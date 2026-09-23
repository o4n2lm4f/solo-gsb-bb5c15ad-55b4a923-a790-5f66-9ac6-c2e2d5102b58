"""会计期间路由。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_session
from ..schemas import PeriodOut
from ..services import master_data

router = APIRouter(prefix="/periods", tags=["periods"])


def _period_out(p) -> PeriodOut:
    return PeriodOut.model_validate(p)


@router.get("", response_model=list[PeriodOut], summary="期间列表")
def list_periods(db: Session = Depends(get_session)) -> list[PeriodOut]:
    return [_period_out(p) for p in master_data.list_periods(db)]


@router.get("/{key}", response_model=PeriodOut, summary="期间详情 (不存在则自动创建并开账)")
def get_period(key: str, db: Session = Depends(get_session)) -> PeriodOut:
    p = master_data.get_period(db, key)
    db.commit()
    return _period_out(p)


@router.post("/{key}/close", response_model=PeriodOut, summary="结账 (幂等)")
def close_period(key: str, db: Session = Depends(get_session)) -> PeriodOut:
    p = master_data.close_period(db, key)
    db.commit()
    db.refresh(p)
    return _period_out(p)


@router.post("/{key}/reopen", response_model=PeriodOut, summary="重新打开期间")
def reopen_period(key: str, db: Session = Depends(get_session)) -> PeriodOut:
    p = master_data.reopen_period(db, key)
    db.commit()
    db.refresh(p)
    return _period_out(p)
