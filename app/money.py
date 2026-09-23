"""金额工具：API 使用十进制字符串，库存整数分。"""
from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from .config import MAX_AMOUNT_MINOR

# 形如 12 / 12.3 / 12.34，允许前导加号、不允许千分位；金额必须为非负数
_AMOUNT_RE = re.compile(r"^\d{1,15}(\.\d{1,2})?$")


def amount_to_minor(text: str) -> int:
    """``"12.34"`` -> 1234。非法或超过两位小数抛 ValueError。"""
    if not isinstance(text, str) or not _AMOUNT_RE.match(text.strip()):
        raise ValueError(f"非法金额 {text!r}：需为非负数字、最多两位小数")
    try:
        value = Decimal(text.strip())
    except InvalidOperation as exc:  # pragma: no cover - 正则已兜底
        raise ValueError(f"非法金额 {text!r}") from exc
    minor = int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if minor > MAX_AMOUNT_MINOR:
        raise ValueError(f"金额 {text} 超出上限")
    return minor


def minor_to_amount(minor: int) -> str:
    """1234 -> ``"12.34"``。"""
    sign = "-" if minor < 0 else ""
    minor = abs(minor)
    return f"{sign}{minor // 100}.{minor % 100:02d}"


def period_key_from_date(d) -> str:  # noqa: ANN001
    """date -> ``"YYYY-MM"``。"""
    return f"{d.year:04d}-{d.month:02d}"
