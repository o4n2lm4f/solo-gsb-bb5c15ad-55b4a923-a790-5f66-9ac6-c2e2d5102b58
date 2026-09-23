"""凭证路由：提交、冲销、查询、凭证链。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from ..database import get_session
from ..schemas import (
    ReversalCreate,
    VoucherBatchCreate,
    VoucherBatchOut,
    VoucherChainOut,
    VoucherCreate,
    VoucherListOut,
    VoucherOut,
)
from ..serializers import voucher_out
from ..services import voucher as voucher_service

router = APIRouter(prefix="/vouchers", tags=["vouchers"])


@router.post("", response_model=VoucherOut, summary="提交凭证 (事务内校验，request_id 幂等)")
def post_voucher(
    payload: VoucherCreate,
    response: Response,
    db: Session = Depends(get_session),
) -> VoucherOut:
    voucher, created = voucher_service.post_voucher(db, payload)
    response.status_code = 201 if created else 200
    response.headers["X-Idempotent-Replay"] = "false" if created else "true"
    return voucher_out(voucher)


@router.post(
    "/batch",
    response_model=VoucherBatchOut,
    summary="批量提交普通凭证 (1-20 张，单事务顺序过账，逐项幂等回放)",
)
def post_vouchers_batch(
    payload: VoucherBatchCreate,
    response: Response,
    db: Session = Depends(get_session),
) -> VoucherBatchOut:
    results = voucher_service.post_vouchers_batch(db, payload.items)
    any_created = any(created for _v, created in results)
    response.status_code = 201 if any_created else 200
    response.headers["X-Idempotent-Replay"] = "false" if any_created else "true"
    items = [
        {"idempotent_replay": not created, "voucher": voucher_out(voucher)}
        for voucher, created in results
    ]
    return VoucherBatchOut(
        items=items,
        created_count=sum(1 for _v, created in results if created),
        replay_count=sum(1 for _v, created in results if not created),
    )


@router.get("", response_model=VoucherListOut, summary="凭证列表 (可按科目/期间过滤，游标分页)")
def list_vouchers(
    account_id: int | None = None,
    period: str | None = None,
    cursor: int | None = None,
    limit: int = 100,
    db: Session = Depends(get_session),
) -> VoucherListOut:
    items, next_cursor = voucher_service.list_vouchers(
        db, account_id=account_id, period_key=period, cursor=cursor, limit=limit
    )
    return VoucherListOut(
        items=[voucher_out(v) for v in items],
        limit=min(max(1, limit), voucher_service.MAX_PAGE_LIMIT),
        next_cursor=next_cursor,
    )


# 注意：静态路径必须声明在 /{voucher_id} 之前，否则 "by-request" 会被当成 id
@router.get(
    "/by-request/{request_id}",
    response_model=VoucherOut,
    summary="按 request_id 幂等查询",
)
def get_by_request_id(request_id: str, db: Session = Depends(get_session)) -> VoucherOut:
    voucher = voucher_service.get_by_request_id(db, request_id)
    if voucher is None:
        from ..errors import not_found
        raise not_found("未找到该 request_id 对应的凭证", request_id=request_id)
    return voucher_out(voucher)


@router.get("/{voucher_id}", response_model=VoucherOut, summary="凭证详情")
def get_voucher(voucher_id: int, db: Session = Depends(get_session)) -> VoucherOut:
    return voucher_out(voucher_service.get_voucher(db, voucher_id))


@router.get(
    "/{voucher_id}/chain",
    response_model=VoucherChainOut,
    summary="凭证链：原始凭证与反向凭证的关联链",
)
def voucher_chain(voucher_id: int, db: Session = Depends(get_session)) -> VoucherChainOut:
    return voucher_service.voucher_chain(db, voucher_id)


@router.post(
    "/{voucher_id}/reverse",
    response_model=VoucherOut,
    summary="提交反向凭证冲销 (唯一允许的撤销方式)",
)
def reverse_voucher(
    voucher_id: int,
    payload: ReversalCreate,
    response: Response,
    db: Session = Depends(get_session),
) -> VoucherOut:
    voucher, created = voucher_service.reverse_voucher(db, voucher_id, payload)
    response.status_code = 201 if created else 200
    response.headers["X-Idempotent-Replay"] = "false" if created else "true"
    return voucher_out(voucher)
