"""数据库引擎、会话与建表逻辑。

SQLite 并发策略
---------------
SQLite 同一时刻只允许一个写事务。SQLAlchemy 的惰性 BEGIN 与
``BEGIN IMMEDIATE`` 配合时需要小心：

1.  ``isolation_level=None`` 让 pysqlite 进入 autocommit 模式，事务边界
    完全由 SQLAlchemy 控制，避免驱动在 DDL 前偷发 BEGIN/COMMIT。
2.  ``connect`` 事件中开启 ``foreign_keys`` 并设置 busy timeout。
3.  ``begin`` 事件把每个写事务升级为 ``BEGIN IMMEDIATED``——进入事务即拿
    到保留锁，写冲突在事务起点排队而不是在 commit 时抛出
    'database is locked'。
"""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import DB_URL, SQLITE_BUSY_TIMEOUT_MS, ensure_data_dir


class Base(DeclarativeBase):
    pass


ensure_data_dir()

# isolation_level=None -> pysqlite autocommit (SQLAlchemy 自行管理事务)
_connect_args: dict = {"timeout": SQLITE_BUSY_TIMEOUT_MS / 1000}
_engine_kwargs: dict = {
    "connect_args": _connect_args,
    "future": True,
}
if DB_URL.startswith("sqlite"):
    _engine_kwargs["isolation_level"] = None

engine: Engine = create_engine(DB_URL, **_engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


if DB_URL.startswith("sqlite"):

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        cur.execute("PRAGMA foreign_keys=ON")
        # WAL 允许读写并发；持久化文件数据库时开启，内存库不支持
        if not DB_URL.endswith(":memory:"):
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

    @event.listens_for(engine, "begin")
    def _begin_immediate(conn):  # noqa: ANN001
        """每个事务立即取保留锁，把写冲突转化为起点处的排队。"""
        cur = conn.connection.cursor()
        cur.execute("BEGIN IMMEDIATE")
        cur.close()


def get_session() -> Iterator[Session]:
    """FastAPI 依赖：请求级会话。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """建表并安装不可变性触发器 (幂等)。"""
    from . import models  # noqa: F401  确保模型已注册

    Base.metadata.create_all(engine)
    _install_triggers()


def _install_triggers() -> None:
    """在 SQLite 层加固凭证不可变性。

    已过账凭证只允许因「反向凭证登记」而更新 ``reversed_by_id`` /
    ``status``，其余任何 UPDATE 以及全部 DELETE 都被拒绝。
    """
    if not DB_URL.startswith("sqlite"):
        return

    stmts = [
        """
        CREATE TRIGGER IF NOT EXISTS trg_voucher_no_update
        BEFORE UPDATE ON vouchers
        WHEN
            NEW.number      IS NOT OLD.number
         OR NEW.date        IS NOT OLD.date
         OR NEW.currency    IS NOT OLD.currency
         OR NEW.period_key  IS NOT OLD.period_key
         OR NEW.request_id  IS NOT OLD.request_id
         OR NEW.created_at  IS NOT OLD.created_at
         OR NEW.memo        IS NOT OLD.memo
         OR (OLD.reverses_id IS NOT NEW.reverses_id)
        BEGIN
            SELECT RAISE(ABORT, 'voucher is immutable after posting');
        END;
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_voucher_status_transition
        BEFORE UPDATE ON vouchers
        WHEN NEW.status NOT IN ('posted','reversed')
           OR (OLD.status = 'reversed' AND NEW.status <> 'reversed')
           OR (OLD.reversed_by_id IS NOT NULL AND NEW.reversed_by_id IS NULL)
        BEGIN
            SELECT RAISE(ABORT, 'illegal voucher status transition');
        END;
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_voucher_line_immutable
        BEFORE UPDATE ON voucher_lines
        BEGIN
            SELECT RAISE(ABORT, 'voucher lines are immutable');
        END;
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_voucher_line_no_delete
        BEFORE DELETE ON voucher_lines
        BEGIN
            SELECT RAISE(ABORT, 'voucher lines are immutable');
        END;
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_voucher_no_delete
        BEFORE DELETE ON vouchers
        BEGIN
            SELECT RAISE(ABORT, 'vouchers cannot be deleted; post a reversal voucher');
        END;
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_balance_no_delete
        BEFORE DELETE ON balances
        BEGIN
            SELECT RAISE(ABORT, 'balances cannot be deleted');
        END;
        """,
    ]
    with engine.begin() as conn:
        for sql in stmts:
            conn.exec_driver_sql(sql)
