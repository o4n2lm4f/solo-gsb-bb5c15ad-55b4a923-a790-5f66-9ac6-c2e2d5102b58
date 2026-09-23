"""ORM 对象 -> API 响应模式 的组装函数。"""
from __future__ import annotations

from sqlalchemy.orm import Session

from .models import Account, Balance, Voucher, VoucherLine
from .money import minor_to_amount
from . import schemas


def account_out(a: Account) -> schemas.AccountOut:
    return schemas.AccountOut.model_validate(a)


def _line_out(line: VoucherLine) -> schemas.LineOut:
    return schemas.LineOut(
        line_no=line.line_no,
        account_id=line.account_id,
        account_code=line.account.code,
        direction=line.direction,
        amount=minor_to_amount(line.amount_minor),
        memo=line.memo,
    )


def voucher_out(v: Voucher) -> schemas.VoucherOut:
    return schemas.VoucherOut(
        id=v.id,
        number=v.number,
        date=v.voucher_date,
        period_key=v.period_key,
        currency=v.currency,
        memo=v.memo,
        status=v.status,
        request_id=v.request_id,
        created_at=v.created_at,
        lines=[_line_out(ln) for ln in sorted(v.lines, key=lambda x: x.line_no)],
        reverses_id=v.reverses_id,
        reverses_number=v.reverses.number if v.reverses is not None else None,
        reversed_by_id=v.reversed_by_id,
        reversed_by_number=(
            db_reversed_by_number(v) if v.reversed_by_id is not None else None
        ),
    )


def db_reversed_by_number(v: Voucher) -> str | None:
    """反向凭证编号；关系未加载时按 id 惰性取一次。"""
    for candidate in v.reversals:
        if candidate.reverses_id == v.id:
            return candidate.number
    return None


def balance_out(db: Session, b: Balance) -> schemas.BalanceOut:
    account = b.account if b.account is not None else db.get(Account, b.account_id)
    net_d = b.debit_minor - b.credit_minor
    net_c = b.credit_minor - b.debit_minor
    return schemas.BalanceOut(
        account_id=b.account_id,
        account_code=account.code,
        currency=account.currency,
        period_key=b.period_key,
        total_debit=minor_to_amount(b.debit_minor),
        total_credit=minor_to_amount(b.credit_minor),
        net_debit=minor_to_amount(net_d),
        net_credit=minor_to_amount(net_c),
    )
