"""FastAPI 应用入口。

启动: uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .config import APP_NAME, APP_VERSION
from .database import init_db
from .errors import register_exception_handlers
from .routers import accounts, periods, reports, vouchers


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if os.getenv("SEED_DEMO", "").lower() in ("1", "true", "yes"):
        from .seed import seed_demo
        seed_demo()
    yield


app = FastAPI(
    title=APP_NAME,
    version=APP_VERSION,
    description=(
        "科研经费双重记账服务：带币种科目、多分录凭证、事务内借贷平衡校验、"
        "结账期间冻结、不可变凭证、反向凭证冲销、request_id 幂等、"
        "费用科目月度支出限额、按科目余额与凭证链查询，以及只读试算平衡表。金额以整数分存储。"
    ),
    lifespan=lifespan,
)

register_exception_handlers(app)
app.include_router(accounts.router)
app.include_router(periods.router)
app.include_router(vouchers.router)
app.include_router(reports.router)


@app.get("/", tags=["meta"], summary="服务信息")
def root() -> dict:
    return {"name": APP_NAME, "version": APP_VERSION, "docs": "/docs"}


@app.get("/health", tags=["meta"], summary="健康检查")
def health() -> dict:
    return {"status": "ok"}
