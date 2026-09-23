"""应用配置。

金额以「最小货币单位的整数」(分) 存储，避免浮点误差；
API 层使用字符串形式的十进制金额 (最多两位小数)。
"""
from __future__ import annotations

import os
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", "/data")).resolve()
DB_URL = os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR / 'ledger.db'}")

# 服务标题
APP_NAME = "科研经费双重记账 API"
APP_VERSION = "1.0.0"

# 金额允许的最大整数分 (1000 亿，约合 10 亿元)，防止溢出/脏数据
MAX_AMOUNT_MINOR = 100_000_000_000

# SQLite 写锁等待毫秒数 (配合 BEGIN IMMEDIATE 串行化写入)
SQLITE_BUSY_TIMEOUT_MS = 10_000


def ensure_data_dir() -> None:
    """确保数据目录存在 (SQLite 文件所在目录)。"""
    if DB_URL.startswith("sqlite:///"):
        path = DB_URL.removeprefix("sqlite:///")
        if path not in (":memory:",):
            Path(path).parent.mkdir(parents=True, exist_ok=True)
