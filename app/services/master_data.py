"""科目与会计期间服务。"""
from __future__ import annotations

from datetime import date as date_cls, datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..errors import conflict, not_found, unprocessable
from ..models import Account, Period
from ..money import period_key_from_date
from .. import schemas

PERIOD_KEY_RE = __import__("re").compile(r"^\d{4}-(0[1-9]|1[0-2])$")


# ---------------------------------------------------------------- 科目
def create_account(db: Session, payload: schemas.AccountCreate) -> Account:
    account = Account(
        code=payload.code,
        name=payload.name,
        currency=payload.currency,
        type=payload.type,
    )
    db.add(account)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise conflict(f"科目代码 {payload.code} 已存在", code_value=payload.code)
    return account


def get_account(db: Session, account_id: int) -> Account:
    account = db.get(Account, account_id)
    if account is None:
        raise not_found(f"科目 {account_id} 不存在", account_id=account_id)
    return account


def list_accounts(db: Session) -> list[Account]:
    return list(db.scalars(select(Account).order_by(Account.code)))


# ---------------------------------------------------------------- 期间
def ensure_period(db: Session, key: str) -> Period:
    """返回期间；不存在则以 open 状态创建 (凭证入账时自动开账)。"""
    period = db.get(Period, key)
    if period is None:
        period = Period(key=key, status="open")
        db.add(period)
        db.flush()
    return period


def get_period(db: Session, key: str) -> Period:
    if not PERIOD_KEY_RE.match(key):
        raise unprocessable(f"非法期间 {key!r}，格式应为 YYYY-MM", period_key=key)
    return ensure_period(db, key)


def list_periods(db: Session) -> list[Period]:
    return list(db.scalars(select(Period).order_by(Period.key)))


def close_period(db: Session, key: str) -> Period:
    if not PERIOD_KEY_RE.match(key):
        raise unprocessable(f"非法期间 {key!r}，格式应为 YYYY-MM", period_key=key)
    period = ensure_period(db, key)
    if period.status != "closed":
        period.status = "closed"
        period.closed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.flush()
    return period


def reopen_period(db: Session, key: str) -> Period:
    """打开已关闭期间 (管理员纠错用)。"""
    period = db.get(Period, key)
    if period is None:
        raise not_found(f"期间 {key} 不存在", period_key=key)
    period.status = "open"
    period.closed_at = None
    db.flush()
    return period


def assert_period_open(db: Session, voucher_date: date_cls) -> tuple[str, Period]:
    """入账前校验期间存在且未结账。返回 (period_key, period)。"""
    key = period_key_from_date(voucher_date)
    period = ensure_period(db, key)
    if period.status == "closed":
        raise conflict(
            f"会计期间 {key} 已结账，不可入账；请提交至未关闭期间",
            period_key=key,
        )
    return key, period
