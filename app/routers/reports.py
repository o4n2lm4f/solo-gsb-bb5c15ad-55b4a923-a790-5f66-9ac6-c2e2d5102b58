"""只读报表路由：试算平衡表。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_session
from ..schemas import TrialBalanceOut
from ..services import reports

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get(
    "/trial-balance",
    response_model=TrialBalanceOut,
    summary="试算平衡表：按币种汇总截止期间的累计发生额与期末余额 (只读)",
)
def get_trial_balance(
    period: str,
    db: Session = Depends(get_session),
) -> TrialBalanceOut:
    """截止期间 ``period=YYYY-MM`` (含本期及此前各期间)，按币种分组。

    只读接口：不提交事务、不创建期间、不改动任何账务数据。
    无余额科目不列出；每币种返回期末借/贷余额合计与 ``balanced`` 是否平衡。
    """
    return reports.trial_balance(db, period)
