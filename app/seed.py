"""科研经费演示账套种子数据 (幂等：request_id / 科目 code 唯一)。

科目表 (单位：币种见 currency)
  1002 银行存款-人民币  CNY asset
  1002USD 银行存款-美元 USD asset
  1122 科研应收款        CNY asset
  2202 应付设备款        CNY liability
  4001 科研拨款收入      CNY income
  5001 科研设备费        CNY expense
  5002 科研材料费        CNY expense
  5003 差旅及学术交流费  CNY expense
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select

from .database import SessionLocal, init_db
from .models import Account
from .schemas import AccountCreate, LineInput, VoucherCreate
from .services import master_data
from .services.voucher import get_by_request_id, post_voucher

ACCOUNTS = [
    ("1002", "银行存款-人民币", "CNY", "asset"),
    ("1002-USD", "银行存款-美元", "USD", "asset"),
    ("1122", "科研应收款", "CNY", "asset"),
    ("2202", "应付设备款", "CNY", "liability"),
    ("4001", "科研拨款收入", "CNY", "income"),
    ("5001", "科研设备费", "CNY", "expense"),
    ("5002", "科研材料费", "CNY", "expense"),
    ("5003", "差旅及学术交流费", "CNY", "expense"),
]

VOUCHERS = [
    {
        "request_id": "seed-grant-2026-0001",
        "date": date(2026, 9, 10),
        "currency": "CNY",
        "memo": "国家自然科学基金项目拨款到账",
        "lines": [
            ("1002", "debit", "1000000.00", "基金拨款到账"),
            ("4001", "credit", "1000000.00", "科研拨款收入"),
        ],
    },
    {
        "request_id": "seed-equipment-2026-0002",
        "date": date(2026, 9, 15),
        "currency": "CNY",
        "memo": "购置质谱仪 (暂记应付，后支付)",
        "lines": [
            ("5001", "debit", "350000.00", "质谱仪一台"),
            ("2202", "credit", "350000.00", "应付设备商"),
        ],
    },
    {
        "request_id": "seed-equipment-pay-2026-0003",
        "date": date(2026, 9, 18),
        "currency": "CNY",
        "memo": "支付设备款",
        "lines": [
            ("2202", "debit", "350000.00", "结清应付设备款"),
            ("1002", "credit", "350000.00", "银行转账"),
        ],
    },
]


def seed_demo() -> None:
    init_db()
    db = SessionLocal()
    try:
        code_to_id: dict[str, int] = {
            a.code: a.id for a in db.scalars(select(Account)).all()
        }
        for code, name, currency, atype in ACCOUNTS:
            if code not in code_to_id:
                account = master_data.create_account(
                    db,
                    AccountCreate(code=code, name=name, currency=currency, type=atype),
                )
                db.commit()
                code_to_id[code] = account.id

        for spec in VOUCHERS:
            if get_by_request_id(db, spec["request_id"]) is not None:
                continue
            payload = VoucherCreate(
                date=spec["date"],
                currency=spec["currency"],
                memo=spec["memo"],
                request_id=spec["request_id"],
                lines=[
                    LineInput(
                        account_id=code_to_id[code],
                        direction=direction,
                        amount=amount,
                        memo=memo,
                    )
                    for code, direction, amount, memo in spec["lines"]
                ],
            )
            voucher, created = post_voucher(db, payload)
            print(f"  {'created' if created else 'exists '}  {voucher.number}  {voucher.memo}")
        print("演示账套就绪。")
    finally:
        db.close()


if __name__ == "__main__":
    seed_demo()
