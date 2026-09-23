"""Pydantic 请求/响应模式。

金额在 API 边界统一为字符串 (如 ``"1000.00"``)，服务端转整数分，
杜绝二进制浮点误差。
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

AccountType = Literal["asset", "liability", "equity", "income", "expense"]
Direction = Literal["debit", "credit"]

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_-]{0,31}$")


# ---------------------------------------------------------------- 请求模式
class AccountCreate(BaseModel):
    code: str = Field(..., description="科目代码，唯一，如 1001")
    name: str = Field(..., min_length=1, max_length=128)
    currency: str = Field(..., description="ISO 4217 币种代码，如 CNY/USD")
    type: AccountType

    @field_validator("currency")
    @classmethod
    def _currency_upper(cls, v: str) -> str:
        v = v.strip().upper()
        if not _CURRENCY_RE.match(v):
            raise ValueError("币种需为 3 位大写字母代码 (ISO 4217)，如 CNY")
        return v

    @field_validator("code")
    @classmethod
    def _code_check(cls, v: str) -> str:
        v = v.strip()
        if not _CODE_RE.match(v):
            raise ValueError("科目代码仅允许字母数字及 : _ - 且长度 1-32")
        return v

    @field_validator("name")
    @classmethod
    def _name_strip(cls, v: str) -> str:
        return v.strip()


class LineInput(BaseModel):
    account_id: int = Field(..., gt=0)
    direction: Direction
    amount: str = Field(..., description="十进制字符串金额，最多两位小数，如 '1000.00'")
    memo: Optional[str] = Field(None, max_length=512)


class VoucherCreate(BaseModel):
    voucher_date: date = Field(..., alias="date")
    currency: str = Field(..., description="凭证币种，所有分录科目必须与此一致")
    lines: list[LineInput] = Field(..., min_length=2)
    memo: Optional[str] = Field(None, max_length=512)
    request_id: str = Field(..., description="客户端幂等键，重复提交返回同一凭证")

    model_config = ConfigDict(populate_by_name=True)

    @field_validator("currency")
    @classmethod
    def _currency_upper(cls, v: str) -> str:
        v = v.strip().upper()
        if not _CURRENCY_RE.match(v):
            raise ValueError("币种需为 3 位大写字母代码 (ISO 4217)，如 CNY")
        return v

    @field_validator("request_id")
    @classmethod
    def _request_id_check(cls, v: str) -> str:
        v = v.strip()
        if not _REQUEST_ID_RE.match(v):
            raise ValueError("request_id 仅允许字母数字及 . _ -，长度 1-128")
        return v


class ReversalCreate(BaseModel):
    voucher_date: Optional[date] = Field(
        None, alias="date", description="反向凭证日期，默认取服务端当前日期"
    )
    memo: Optional[str] = Field(None, max_length=512)
    request_id: str

    model_config = ConfigDict(populate_by_name=True)

    @field_validator("request_id")
    @classmethod
    def _request_id_check(cls, v: str) -> str:
        v = v.strip()
        if not _REQUEST_ID_RE.match(v):
            raise ValueError("request_id 仅允许字母数字及 . _ -，长度 1-128")
        return v


# ---------------------------------------------------------------- 支出限额
class SpendingLimitCreate(BaseModel):
    amount: str = Field(
        ..., description="限额金额，十进制字符串，最多两位小数；设置 '0.00' 即冻结该月新增借方费用"
    )


# ---------------------------------------------------------------- 响应模式
class AccountOut(BaseModel):
    id: int
    code: str
    name: str
    currency: str
    type: AccountType
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class LineOut(BaseModel):
    line_no: int
    account_id: int
    account_code: str
    direction: Direction
    amount: str
    memo: Optional[str] = None


class VoucherOut(BaseModel):
    id: int
    number: str
    date: date
    period_key: str
    currency: str
    memo: Optional[str]
    status: Literal["posted", "reversed"]
    request_id: str
    created_at: datetime
    lines: list[LineOut]
    reverses_id: Optional[int] = None
    reverses_number: Optional[str] = None
    reversed_by_id: Optional[int] = None
    reversed_by_number: Optional[str] = None


class PeriodOut(BaseModel):
    key: str
    status: Literal["open", "closed"]
    created_at: datetime
    closed_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class BalanceOut(BaseModel):
    account_id: int
    account_code: str
    currency: str
    period_key: str
    total_debit: str
    total_credit: str
    net_debit: str   # 借-贷 (可为负字符串)
    net_credit: str  # 贷-借 (可为负字符串)


class LedgerEntry(BaseModel):
    line_id: int
    voucher_id: int
    voucher_number: str
    date: date
    direction: Direction
    amount: str
    balance_after_debit: str
    balance_after_credit: str
    memo: Optional[str] = None


class LedgerOut(BaseModel):
    account_id: int
    account_code: str
    currency: str
    period_from: Optional[str]
    period_to: Optional[str]
    starting_debit: str = "0.00"
    starting_credit: str = "0.00"
    entries: list[LedgerEntry]
    next_cursor: Optional[int] = None


class VoucherChainNode(BaseModel):
    id: int
    number: str
    date: date
    direction_in_chain: Literal["original", "reversal"]
    status: Literal["posted", "reversed"]
    reverses_id: Optional[int]
    reversed_by_id: Optional[int]
    total_amount: str
    currency: str
    memo: Optional[str]


class VoucherChainOut(BaseModel):
    root_id: int
    nodes: list[VoucherChainNode]


class VoucherListOut(BaseModel):
    items: list[VoucherOut]
    limit: int
    next_cursor: Optional[int] = None


# ---------------------------------------------------------------- 试算平衡表
class TrialBalanceAccountOut(BaseModel):
    account_id: int
    account_code: str
    account_name: str
    account_type: AccountType
    total_debit: str        # 截止期末累计借方发生额
    total_credit: str       # 截止期末累计贷方发生额
    closing_debit: str      # 期末借方余额 (借>贷 时为差额，否则 "0.00")
    closing_credit: str     # 期末贷方余额 (贷>借 时为差额，否则 "0.00")
    balance_direction: Literal["debit", "credit"]  # 期末余额方向


class TrialBalanceCurrencyOut(BaseModel):
    currency: str
    # 该币种全部列示科目的期末借/贷余额合计
    total_debit: str
    total_credit: str
    balanced: bool          # 期末借方合计 == 期末贷方合计
    accounts: list[TrialBalanceAccountOut]


class TrialBalanceOut(BaseModel):
    period: str             # 截止期间 YYYY-MM (含本期及此前各期间)
    currencies: list[TrialBalanceCurrencyOut]


# ---------------------------------------------------------------- 支出限额
class SpendingLimitOut(BaseModel):
    account_id: int
    account_code: str
    account_name: str
    currency: str
    period_key: str
    limit: str              # 月度支出限额
    used_debit: str         # 本月已过账且未被冲销的借方费用合计
    remaining: str          # 剩余可用额度 (限额下调到已用额以下时为负数字符串)
    created_at: datetime
    updated_at: datetime
