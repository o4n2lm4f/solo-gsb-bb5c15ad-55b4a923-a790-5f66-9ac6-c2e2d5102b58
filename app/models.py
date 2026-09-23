"""SQLAlchemy ORM 模型。

核心结构
========
- ``Account``  会计科目，带币种 (如 CNY/USD)
- ``Period``   会计期间 (按月)，open / closed
- ``Voucher``  不可变凭证头；``reverses_id`` 指回被冲销凭证
- ``VoucherLine`` 借贷分录行，金额以整数分存储
- ``Balance``  按 (科目, 期间) 物化的借贷累计额

设计约束
========
- 借贷方向以正整数 ``amount_minor`` + ``direction`` ('debit'/'credit') 表示；
- 一张凭证内所有分录币种必须相同，且与科目币种一致；
- 已过账凭证不可修改/删除，撤销只能通过反向凭证。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    # asset 资产 / liability 负债 / equity 净资产 / income 收入 / expense 费用
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp(), nullable=False)

    lines: Mapped[list["VoucherLine"]] = relationship(back_populates="account")
    balances: Mapped[list["Balance"]] = relationship(back_populates="account")
    spending_limits: Mapped[list["SpendingLimit"]] = relationship(back_populates="account")


class Period(Base):
    __tablename__ = "periods"

    # 期间主键即 YYYY-MM
    key: Mapped[str] = mapped_column(String(7), primary_key=True)
    status: Mapped[str] = mapped_column(String(8), nullable=False, default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp(), nullable=False)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (CheckConstraint("status IN ('open','closed')", name="ck_period_status"),)


class Voucher(Base):
    __tablename__ = "vouchers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 人类可读的顺序凭证号 V-YYYYMM-0001 (同期间内顺序递增)
    number: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    voucher_date: Mapped[date] = mapped_column("date", Date, nullable=False)
    period_key: Mapped[str] = mapped_column(String(7), ForeignKey("periods.key"), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    memo: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    # posted 正常 / reversed 已被反向凭证冲销
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="posted")
    request_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp(), nullable=False)

    # 本凭证是对哪张凭证的反向冲销
    reverses_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("vouchers.id"), nullable=True
    )
    # 被冲销凭证上回填冲销它的凭证
    reversed_by_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("vouchers.id"), nullable=True
    )

    lines: Mapped[list["VoucherLine"]] = relationship(
        back_populates="voucher",
        lazy="selectin",
        cascade="all",
    )
    reverses: Mapped[Optional["Voucher"]] = relationship(
        foreign_keys=[reverses_id], remote_side=[id], back_populates="reversals"
    )
    reversals: Mapped[list["Voucher"]] = relationship(
        foreign_keys=[reverses_id], back_populates="reverses"
    )

    __table_args__ = (
        CheckConstraint("status IN ('posted','reversed')", name="ck_voucher_status"),
        CheckConstraint("reverses_id IS NULL OR reverses_id <> id", name="ck_no_self_reverse"),
        Index("ix_voucher_period", "period_key"),
    )


class VoucherLine(Base):
    __tablename__ = "voucher_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    voucher_id: Mapped[int] = mapped_column(
        ForeignKey("vouchers.id", ondelete="RESTRICT"), nullable=False
    )
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    direction: Mapped[str] = mapped_column(String(6), nullable=False)  # debit / credit
    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    memo: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    voucher: Mapped[Voucher] = relationship(back_populates="lines")
    account: Mapped[Account] = relationship(back_populates="lines", lazy="joined")

    __table_args__ = (
        CheckConstraint("direction IN ('debit','credit')", name="ck_line_direction"),
        CheckConstraint("amount_minor >= 0", name="ck_line_amount_nonneg"),
        UniqueConstraint("voucher_id", "line_no", name="uq_voucher_line_no"),
        Index("ix_line_account", "account_id"),
    )


class VoucherNumber(Base):
    """按期间分配的顺序凭证号计数器 (UPSERT 自增)。"""

    __tablename__ = "voucher_numbers"

    period_key: Mapped[str] = mapped_column(String(7), primary_key=True)
    next_seq: Mapped[int] = mapped_column(Integer, nullable=False)


class Balance(Base):
    """按期间物化的科目借贷累计 (整数分)，过账/冲销时在事务内 UPSERT。"""

    __tablename__ = "balances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    period_key: Mapped[str] = mapped_column(String(7), nullable=False)
    debit_minor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    credit_minor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(), nullable=False,
    )

    account: Mapped[Account] = relationship(back_populates="balances")

    __table_args__ = (
        UniqueConstraint("account_id", "period_key", name="uq_balance_account_period"),
        # 期间关闭后余额冻结：关闭期间不应再有余额行变化 (服务层保证)
    )


class SpendingLimit(Base):
    """费用科目的月度支出限额 (整数分)，按 (科目, YYYY-MM) 设置，可随时调整。

    - 只允许挂在 ``type='expense'`` 的费用科目上 (服务层校验)；
    - 过账时该科目当月新增借方费用 + 已过账未冲销借方费用合计不得超过限额；
    - 限额行本身可变 (PUT 覆盖)，不需要不可变触发器。
    """

    __tablename__ = "spending_limits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    period_key: Mapped[str] = mapped_column(String(7), nullable=False)
    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(), nullable=False,
    )

    account: Mapped[Account] = relationship(back_populates="spending_limits")

    __table_args__ = (
        UniqueConstraint("account_id", "period_key", name="uq_spending_limit_account_period"),
        CheckConstraint("amount_minor >= 0", name="ck_spending_limit_nonneg"),
    )
