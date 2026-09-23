"""费用科目月度支出限额服务：设置/调整、查询、过账时强校验。

限额语义
========
- 只有 ``type='expense'`` 的费用科目允许设置限额；
- 每个 (费用科目, YYYY-MM) 一条限额，PUT 覆盖、可随时调整；
- 过账时，把**本次凭证内该费用科目的借方金额**加上该科目本月**已过账、
  且未被冲销**的借方费用合计，与限额比较，超限则整个凭证拒绝；
- 已用额直接从 ``voucher_lines`` 实时聚合：被冲销凭证 (``status='reversed'``)
  与反向凭证 (``reverses_id IS NOT NULL``) 的行都不计入，因此同月冲销会
  自动释放原已用额度；
- 限额校验由过账流程在同一个 ``BEGIN IMMEDIATE`` 写事务内调用。SQLite 写事务
  在事务起点排队串行执行，因此并发过账/调限额时，每个事务看到的已用额都
  包含此前已提交事务，二者不可能共同突破上限。
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ..errors import not_found, unprocessable
from ..models import Account, SpendingLimit, Voucher, VoucherLine
from ..money import amount_to_minor, minor_to_amount
from ..schemas import SpendingLimitOut
from .master_data import PERIOD_KEY_RE


def _validate_period(period_key: str) -> None:
    if not PERIOD_KEY_RE.match(period_key):
        raise unprocessable(f"非法期间 {period_key!r}，格式应为 YYYY-MM", period_key=period_key)


def used_expense_debit(db: Session, account_id: int, period_key: str) -> int:
    """该费用科目在指定期间已过账且未被冲销的借方费用合计 (整数分)。

    排除两类行：
    - 被冲销的原始凭证 (``status='reversed'``)；
    - 反向凭证本身 (``reverses_id IS NOT NULL``，冲销跨期时其分录落在别的
      期间，同月冲销时其费用行在贷方，两种情况下都不应占用限额)。
    """
    used = db.scalar(
        select(func.coalesce(func.sum(VoucherLine.amount_minor), 0))
        .join(Voucher, Voucher.id == VoucherLine.voucher_id)
        .where(
            VoucherLine.account_id == account_id,
            VoucherLine.direction == "debit",
            Voucher.period_key == period_key,
            Voucher.status == "posted",
            Voucher.reverses_id.is_(None),
        )
    )
    return int(used or 0)


def assert_within_limits(
    db: Session,
    period_key: str,
    expense_debits: dict[int, int],
) -> None:
    """过账事务内的限额校验。``expense_debits`` 为本次凭证 {费用科目: 借方金额}。

    超限抛 422 ``spending_limit_exceeded``；无限额的费用科目不做限制。
    调用方必须已在写事务内 (BEGIN IMMEDIATE)，从而与其他过账/调限额串行。
    """
    if not expense_debits:
        return
    limits = list(db.scalars(
        select(SpendingLimit).where(
            SpendingLimit.period_key == period_key,
            SpendingLimit.account_id.in_(expense_debits.keys()),
        )
    ))
    for limit in limits:
        new_debit = expense_debits.get(limit.account_id, 0)
        if new_debit == 0:
            continue
        used = used_expense_debit(db, limit.account_id, period_key)
        if used + new_debit > limit.amount_minor:
            account = db.get(Account, limit.account_id)
            raise unprocessable(
                f"费用科目 {account.code} 在 {period_key} 的支出限额不足",
                reason="spending_limit_exceeded",
                account_id=limit.account_id,
                account_code=account.code,
                period_key=period_key,
                limit_minor=limit.amount_minor,
                used_minor=used,
                new_debit_minor=new_debit,
                total_minor=used + new_debit,
                available_minor=limit.amount_minor - used,
            )


def limit_out(db: Session, limit: SpendingLimit) -> SpendingLimitOut:
    account = limit.account if limit.account is not None else db.get(Account, limit.account_id)
    used = used_expense_debit(db, limit.account_id, limit.period_key)
    return SpendingLimitOut(
        account_id=limit.account_id,
        account_code=account.code,
        account_name=account.name,
        currency=account.currency,
        period_key=limit.period_key,
        limit=minor_to_amount(limit.amount_minor),
        used_debit=minor_to_amount(used),
        remaining=minor_to_amount(limit.amount_minor - used),
        created_at=limit.created_at,
        updated_at=limit.updated_at,
    )


# ================================================================ 设置/调整
def set_limit(db: Session, account_id: int, period_key: str, amount_text: str) -> SpendingLimit:
    """设置或覆盖某费用科目某月份的支出限额 (UPSERT)，返回限额行。"""
    _validate_period(period_key)
    account = db.get(Account, account_id)
    if account is None:
        raise not_found(f"科目 {account_id} 不存在", account_id=account_id)
    if account.type != "expense":
        raise unprocessable(
            f"科目 {account.code} 类型为 {account.type}，只有费用科目 (expense) 可设置支出限额",
            reason="not_expense_account",
            account_id=account_id,
            account_type=account.type,
        )
    try:
        amount_minor = amount_to_minor(amount_text)
    except ValueError as exc:
        raise unprocessable(str(exc), reason="invalid_amount")

    db.execute(
        sqlite_insert(SpendingLimit)
        .values(account_id=account_id, period_key=period_key, amount_minor=amount_minor)
        .on_conflict_do_update(
            index_elements=[SpendingLimit.account_id, SpendingLimit.period_key],
            set_={"amount_minor": amount_minor},
        )
    )
    limit = db.scalar(select(SpendingLimit).where(
        SpendingLimit.account_id == account_id,
        SpendingLimit.period_key == period_key,
    ))
    return limit


def get_limit_out(db: Session, account_id: int, period_key: str) -> SpendingLimitOut:
    _validate_period(period_key)
    account = db.get(Account, account_id)
    if account is None:
        raise not_found(f"科目 {account_id} 不存在", account_id=account_id)
    limit = db.scalar(select(SpendingLimit).where(
        SpendingLimit.account_id == account_id,
        SpendingLimit.period_key == period_key,
    ))
    if limit is None:
        raise not_found(
            f"科目 {account.code} 在 {period_key} 未设置支出限额",
            account_id=account_id, period_key=period_key,
        )
    return limit_out(db, limit)


def list_limits_out(db: Session, account_id: int) -> list[SpendingLimitOut]:
    account = db.get(Account, account_id)
    if account is None:
        raise not_found(f"科目 {account_id} 不存在", account_id=account_id)
    rows = list(db.scalars(
        select(SpendingLimit)
        .where(SpendingLimit.account_id == account_id)
        .order_by(SpendingLimit.period_key)
    ))
    return [limit_out(db, row) for row in rows]
