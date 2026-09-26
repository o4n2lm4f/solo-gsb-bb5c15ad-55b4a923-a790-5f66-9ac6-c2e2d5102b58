"""凭证领域服务：过账、批量过账、冲销、查询。

过账在单个 ``BEGIN IMMEDIATE`` 事务内完成：

1. request_id 幂等检查 -> 命中则原样回放；
2. 期间存在且未结账；
3. 逐行加载科目并校验币种一致；
4. 借贷总额相等；
5. 费用科目月度支出限额校验 (本次借方费用 + 本月已用额 <= 限额)；
6. 分配期间内顺序凭证号；
7. 写入不可变凭证与分录；
8. UPSERT 每个 (科目, 期间) 的余额；
9. commit。任一步失败整体回滚。

批量过账 (``post_vouchers_batch``) 把上述 2-8 步按请求顺序逐张执行，
所有新凭证共用同一个事务、只在最后 commit 一次：前序新凭证 flush 后
即计入后序凭证的限额已用额聚合；任一项失败整批回滚。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ..errors import conflict, not_found, unprocessable
from ..models import Account, Balance, Voucher, VoucherLine
from ..money import amount_to_minor
from ..schemas import LedgerEntry, LedgerOut, VoucherChainNode, VoucherChainOut, VoucherCreate
from .master_data import assert_period_open
from .spending_limits import assert_within_limits

DEFAULT_PAGE_LIMIT = 100
MAX_PAGE_LIMIT = 500


# ================================================================ 内部工具
def _next_voucher_number(db: Session, period_key: str) -> str:
    """分配期间内顺序凭证号 ``V-YYYYMM-NNNN``。

    显式的编号表 + upsert 自增；外层事务已是 IMMEDIATE，编号分配天然串行。
    """
    from ..models import VoucherNumber  # 局部导入避免循环

    db.execute(
        sqlite_insert(VoucherNumber)
        .values(period_key=period_key, next_seq=1)
        .on_conflict_do_update(
            index_elements=[VoucherNumber.period_key],
            set_={"next_seq": VoucherNumber.next_seq + 1},
        )
    )
    seq = db.scalar(select(VoucherNumber.next_seq).where(VoucherNumber.period_key == period_key))
    return f"V-{period_key}-{seq:04d}"


def _apply_balances(db: Session, period_key: str, account_amounts: dict[int, tuple[int, int]]) -> None:
    """把本次过账的 (借, 贷) 增量合并进余额表。"""
    for account_id, (debit, credit) in account_amounts.items():
        if debit == 0 and credit == 0:
            continue
        stmt = (
            sqlite_insert(Balance)
            .values(
                account_id=account_id,
                period_key=period_key,
                debit_minor=debit,
                credit_minor=credit,
            )
            .on_conflict_do_update(
                index_elements=[Balance.account_id, Balance.period_key],
                set_={
                    "debit_minor": Balance.debit_minor + debit,
                    "credit_minor": Balance.credit_minor + credit,
                },
            )
        )
        db.execute(stmt)


def _parse_lines(db: Session, payload) -> dict:  # noqa: ANN001
    """校验并解析分录，返回中间结构。"""
    if len(payload.lines) < 2:
        raise unprocessable("凭证至少包含两条分录", reason="too_few_lines")

    parsed: list[dict] = []
    total_debit = 0
    total_credit = 0
    seen_accounts: set[int] = set()

    for idx, line in enumerate(payload.lines, start=1):
        try:
            amount_minor = amount_to_minor(line.amount)
        except ValueError as exc:
            raise unprocessable(str(exc), line_no=idx)

        account = db.get(Account, line.account_id)
        if account is None:
            raise unprocessable(
                f"第 {idx} 行科目 {line.account_id} 不存在",
                line_no=idx,
                account_id=line.account_id,
            )
        if account.currency != payload.currency:
            raise unprocessable(
                f"第 {idx} 行科目 {account.code} 币种 {account.currency} "
                f"与凭证币种 {payload.currency} 不一致",
                line_no=idx,
                account_id=account.id,
                account_currency=account.currency,
                voucher_currency=payload.currency,
            )
        seen_accounts.add(account.id)
        parsed.append({
            "account": account,
            "direction": line.direction,
            "amount_minor": amount_minor,
            "memo": line.memo,
        })
        if line.direction == "debit":
            total_debit += amount_minor
        else:
            total_credit += amount_minor

    if total_debit == 0 or total_credit == 0:
        raise unprocessable(
            "凭证必须同时包含借方与贷方分录",
            reason="missing_debit_or_credit",
        )
    if total_debit != total_credit:
        raise unprocessable(
            "借贷不平衡",
            reason="debits_not_equal_credits",
            total_debit=total_debit,
            total_credit=total_credit,
        )

    return {
        "parsed": parsed,
        "total_debit": total_debit,
        "total_credit": total_credit,
    }


def get_by_request_id(db: Session, request_id: str) -> Optional[Voucher]:
    return db.scalar(select(Voucher).where(Voucher.request_id == request_id))


# ================================================================ 过账
def _post_new_voucher(db: Session, payload: VoucherCreate) -> Voucher:
    """校验并写入一张新凭证 (不 commit，事务边界由调用方决定)。

    期间/币种/借贷/限额校验与单笔过账完全一致；分录写入后显式 flush，
    使同一事务内后续 (批量) 凭证的限额已用额聚合能看到本次分录。
    """
    # 所有校验与写入在一个 IMMEDIATE 事务内
    period_key, _period = assert_period_open(db, payload.voucher_date)
    result = _parse_lines(db, payload)

    # 月度支出限额：本次借方费用 + 本月已过账未冲销借方费用 > 限额则整体拒绝。
    # 此时尚未分配凭证号/写入任何数据，超限直接抛错即回滚。
    expense_debits: dict[int, int] = {}
    for item in result["parsed"]:
        if item["direction"] == "debit" and item["account"].type == "expense":
            expense_debits[item["account"].id] = (
                expense_debits.get(item["account"].id, 0) + item["amount_minor"]
            )
    assert_within_limits(db, period_key, expense_debits)

    number = _next_voucher_number(db, period_key)
    voucher = Voucher(
        number=number,
        voucher_date=payload.voucher_date,
        period_key=period_key,
        currency=payload.currency,
        memo=payload.memo,
        status="posted",
        request_id=payload.request_id,
    )
    db.add(voucher)
    db.flush()  # 取 voucher.id

    account_amounts: dict[int, list[int]] = {}
    for idx, item in enumerate(result["parsed"], start=1):
        db.add(VoucherLine(
            voucher_id=voucher.id,
            account_id=item["account"].id,
            line_no=idx,
            direction=item["direction"],
            amount_minor=item["amount_minor"],
            memo=item["memo"],
        ))
        slot = account_amounts.setdefault(item["account"].id, [0, 0])
        if item["direction"] == "debit":
            slot[0] += item["amount_minor"]
        else:
            slot[1] += item["amount_minor"]

    _apply_balances(db, period_key, {k: (v[0], v[1]) for k, v in account_amounts.items()})
    db.flush()  # 批量过账时，让后序凭证的限额聚合看到本次分录
    return voucher


def post_voucher(db: Session, payload: VoucherCreate) -> tuple[Voucher, bool]:
    """提交并过账凭证。返回 (凭证, 是否新建)；重复 request_id 幂等回放。"""
    existing = get_by_request_id(db, payload.request_id)
    if existing is not None:
        return existing, False
    voucher = _post_new_voucher(db, payload)
    db.commit()
    db.refresh(voucher)
    return voucher, True


# ================================================================ 批量过账
def post_vouchers_batch(
    db: Session, payloads: list[VoucherCreate]
) -> list[tuple[Voucher, bool]]:
    """按请求顺序过账一批普通凭证，全部新凭证共享一个事务。

    - 批内 request_id 重复 -> 422，整批拒绝；
    - request_id 已存在 -> 按单笔接口同样的幂等语义原样回放，不重复占用限额；
    - 新凭证逐张过账并 flush，后序凭证的限额校验计入前序已占用额度；
    - 只在最后 commit 一次：任一项失败抛错，整批随会话关闭回滚，
      凭证、凭证号与余额都不留痕。
    """
    seen: set[str] = set()
    for idx, payload in enumerate(payloads, start=1):
        if payload.request_id in seen:
            raise unprocessable(
                f"批内第 {idx} 项 request_id 与之前项重复: {payload.request_id!r}",
                reason="duplicate_request_id_in_batch",
                index=idx,
                request_id=payload.request_id,
            )
        seen.add(payload.request_id)

    results: list[tuple[Voucher, bool]] = []
    for payload in payloads:
        existing = get_by_request_id(db, payload.request_id)
        if existing is not None:
            results.append((existing, False))
            continue
        results.append((_post_new_voucher(db, payload), True))
    db.commit()
    for voucher, _created in results:
        db.refresh(voucher)
    return results


# ================================================================ 反向凭证
def reverse_voucher(db: Session, original_id: int, payload) -> tuple[Voucher, bool]:  # noqa: ANN001
    """对已过账凭证提交反向凭证 (借贷方向全部对调)，并保留双向关联。"""
    original = db.get(Voucher, original_id)
    if original is None:
        raise not_found(f"凭证 {original_id} 不存在", voucher_id=original_id)

    existing = get_by_request_id(db, payload.request_id)
    if existing is not None:
        if existing.reverses_id != original.id:
            raise conflict(
                "request_id 已用于其他凭证",
                request_id=payload.request_id,
            )
        return existing, False

    if original.reversed_by_id is not None:
        raise conflict(
            "该凭证已被反向凭证冲销，不可重复冲销",
            voucher_id=original.id,
            reversed_by_id=original.reversed_by_id,
        )
    if original.reverses_id is not None:
        raise conflict("不能对反向凭证再次冲销；请对原始凭证重新发起冲销", voucher_id=original.id)

    reversal_date = payload.voucher_date or datetime.now(timezone.utc).date()
    period_key, _period = assert_period_open(db, reversal_date)

    # 反向凭证不做支出限额校验：费用科目的反向分录是贷方，不增加借方费用；
    # 同月冲销时，被冲销原凭证在已用额聚合中按 status='reversed' 被排除，
    # 额度随之释放 (跨期冲销不影响原期间，原期间已用额同样剔除被冲销行)。

    number = _next_voucher_number(db, period_key)
    reversal = Voucher(
        number=number,
        voucher_date=reversal_date,
        period_key=period_key,
        currency=original.currency,
        memo=payload.memo or f"冲销凭证 {original.number}",
        status="posted",
        request_id=payload.request_id,
        reverses_id=original.id,
    )
    db.add(reversal)
    db.flush()

    account_amounts: dict[int, list[int]] = {}
    for line in sorted(original.lines, key=lambda x: x.line_no):
        flipped = "credit" if line.direction == "debit" else "debit"
        db.add(VoucherLine(
            voucher_id=reversal.id,
            account_id=line.account_id,
            line_no=line.line_no,
            direction=flipped,
            amount_minor=line.amount_minor,
            memo=line.memo or f"冲销 {original.number}",
        ))
        slot = account_amounts.setdefault(line.account_id, [0, 0])
        if flipped == "debit":
            slot[0] += line.amount_minor
        else:
            slot[1] += line.amount_minor

    _apply_balances(db, period_key, {k: (v[0], v[1]) for k, v in account_amounts.items()})

    # 回填原凭证：仅这两个字段允许被触发器更新
    original.status = "reversed"
    original.reversed_by_id = reversal.id

    db.commit()
    db.refresh(reversal)
    return reversal, True


# ================================================================ 查询
def get_voucher(db: Session, voucher_id: int) -> Voucher:
    voucher = db.get(Voucher, voucher_id)
    if voucher is None:
        raise not_found(f"凭证 {voucher_id} 不存在", voucher_id=voucher_id)
    return voucher


def list_vouchers(
    db: Session,
    *,
    account_id: Optional[int] = None,
    period_key: Optional[str] = None,
    cursor: Optional[int] = None,
    limit: int = DEFAULT_PAGE_LIMIT,
) -> tuple[list[Voucher], Optional[int]]:
    limit = min(max(1, limit), MAX_PAGE_LIMIT)
    stmt = select(Voucher)
    if account_id is not None:
        stmt = stmt.join(VoucherLine, VoucherLine.voucher_id == Voucher.id).where(
            VoucherLine.account_id == account_id
        )
    if period_key:
        stmt = stmt.where(Voucher.period_key == period_key)
    if cursor is not None:
        stmt = stmt.where(Voucher.id < cursor)
    stmt = stmt.order_by(Voucher.id.desc()).limit(limit + 1)

    rows = list(db.scalars(stmt).unique())
    next_cursor = rows[-1].id if len(rows) > limit else None
    return rows[:limit], next_cursor


def voucher_chain(db: Session, voucher_id: int) -> VoucherChainOut:
    """返回原始凭证 -> 反向凭证 的链。从任意节点出发都会回溯到根。"""
    start = get_voucher(db, voucher_id)

    # 回溯到原始凭证 (链只有一层：original <-> reversal)
    root = start
    while root.reverses_id is not None:
        root = db.get(Voucher, root.reverses_id)

    nodes: list[Voucher] = [root]
    nodes.extend(
        db.scalars(
            select(Voucher).where(Voucher.reverses_id == root.id).order_by(Voucher.id)
        ).all()
    )

    def total_amount(v: Voucher) -> int:
        return sum(
            ln.amount_minor for ln in v.lines if ln.direction == "debit"
        )

    out_nodes = [
        VoucherChainNode(
            id=v.id,
            number=v.number,
            date=v.voucher_date,
            direction_in_chain="original" if v.id == root.id else "reversal",
            status=v.status,
            reverses_id=v.reverses_id,
            reversed_by_id=v.reversed_by_id,
            total_amount=_fmt(total_amount(v)),
            currency=v.currency,
            memo=v.memo,
        )
        for v in nodes
    ]
    return VoucherChainOut(root_id=root.id, nodes=out_nodes)


def _fmt(minor: int) -> str:
    from ..money import minor_to_amount
    return minor_to_amount(minor)


# ================================================================ 科目余额
def account_balances(db: Session, account_id: int, period_key: Optional[str] = None) -> list[Balance]:
    account = db.get(Account, account_id)
    if account is None:
        raise not_found(f"科目 {account_id} 不存在", account_id=account_id)
    stmt = select(Balance).where(Balance.account_id == account_id)
    if period_key:
        stmt = stmt.where(Balance.period_key == period_key)
    return list(db.scalars(stmt.order_by(Balance.period_key)))


def account_ledger(
    db: Session,
    account_id: int,
    *,
    period_from: Optional[str] = None,
    period_to: Optional[str] = None,
    cursor: Optional[int] = None,
    limit: int = DEFAULT_PAGE_LIMIT,
) -> LedgerOut:
    """科目分类账：按时间顺序的分录流与逐笔累计余额。"""
    account = db.get(Account, account_id)
    if account is None:
        raise not_found(f"科目 {account_id} 不存在", account_id=account_id)
    limit = min(max(1, limit), MAX_PAGE_LIMIT)

    stmt = (
        select(VoucherLine)
        .join(Voucher, Voucher.id == VoucherLine.voucher_id)
        .where(VoucherLine.account_id == account_id)
    )
    if period_from:
        stmt = stmt.where(Voucher.period_key >= period_from)
    if period_to:
        stmt = stmt.where(Voucher.period_key <= period_to)
    if cursor is not None:
        stmt = stmt.where(VoucherLine.id > cursor)
    stmt = stmt.order_by(VoucherLine.id.asc()).limit(limit + 1)

    rows = list(db.scalars(stmt))
    page = rows[:limit]
    next_cursor = rows[-1].id if len(rows) > limit else None

    # 期初：本页第一行之前 (且早于 period_from 边界) 的累计
    start_d = start_c = 0
    if page:
        first_id = page[0].id
        prior = db.execute(
            select(
                func.coalesce(func.sum(
                    func.iif(VoucherLine.direction == "debit", VoucherLine.amount_minor, 0)
                ), 0),
                func.coalesce(func.sum(
                    func.iif(VoucherLine.direction == "credit", VoucherLine.amount_minor, 0)
                ), 0),
            )
            .join(Voucher, Voucher.id == VoucherLine.voucher_id)
            .where(
                VoucherLine.account_id == account_id,
                VoucherLine.id < first_id,
                *(
                    [Voucher.period_key >= period_from] if period_from else []
                ),
                *(
                    [Voucher.period_key <= period_to] if period_to else []
                ),
            )
        ).one()
        start_d, start_c = int(prior[0]), int(prior[1])

    run_d, run_c = start_d, start_c
    entries: list[LedgerEntry] = []
    for line in page:
        if line.direction == "debit":
            run_d += line.amount_minor
        else:
            run_c += line.amount_minor
        entries.append(LedgerEntry(
            line_id=line.id,
            voucher_id=line.voucher_id,
            voucher_number=line.voucher.number,
            date=line.voucher.voucher_date,
            direction=line.direction,
            amount=_fmt(line.amount_minor),
            balance_after_debit=_fmt(run_d),
            balance_after_credit=_fmt(run_c),
            memo=line.memo,
        ))

    return LedgerOut(
        account_id=account.id,
        account_code=account.code,
        currency=account.currency,
        period_from=period_from,
        period_to=period_to,
        starting_debit=_fmt(start_d),
        starting_credit=_fmt(start_c),
        entries=entries,
        next_cursor=next_cursor,
    )
