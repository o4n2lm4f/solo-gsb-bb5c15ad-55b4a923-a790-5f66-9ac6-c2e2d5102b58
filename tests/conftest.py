"""pytest 夹具：每个测试一个临时文件数据库，建表后通过 TestClient 调用。

注意：必须在导入 ``app.*`` 之前设置 DATABASE_URL，故本文件顶部先配环境变量。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

_tmpdir = tempfile.mkdtemp(prefix="ledger-test-")
_db_path = Path(_tmpdir) / "test.db"

import os  # noqa: E402

os.environ["DATABASE_URL"] = f"sqlite:///{_db_path}"
os.environ["DATA_DIR"] = _tmpdir

from fastapi.testclient import TestClient  # noqa: E402

from app.database import SessionLocal, engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Account,
    Balance,
    Period,
    Voucher,
    VoucherLine,
    VoucherNumber,
)


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def clean_db():
    """每个用例前清空全部业务表 (触发器不拦截 DELETE 配置表以外的表)。"""
    init_db()
    # 上一个用例可能留下未释放的事务/保留锁，先确保连接全部归还并关闭
    engine.dispose()
    with engine.begin() as conn:
        # 先临时摘除触发器以清空数据，再重建
        for trig in (
            "trg_voucher_no_update",
            "trg_voucher_status_transition",
            "trg_voucher_line_immutable",
            "trg_voucher_line_no_delete",
            "trg_voucher_no_delete",
            "trg_balance_no_delete",
        ):
            conn.exec_driver_sql(f"DROP TRIGGER IF EXISTS {trig}")
        for table in (
            "voucher_lines",
            "vouchers",
            "voucher_numbers",
            "balances",
            "spending_limits",
            "periods",
            "accounts",
        ):
            conn.exec_driver_sql(f"DELETE FROM {table}")
    init_db()
    yield
    SessionLocal().close()
