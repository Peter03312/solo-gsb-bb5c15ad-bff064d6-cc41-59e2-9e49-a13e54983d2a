"""HTTP API 路由。"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query

from . import service
from .database import get_conn
from .errors import ApiError
from .models import (
    CertCorrectRequest,
    CertIssueRequest,
    CertRevokeRequest,
    ReadingSubmitRequest,
    UtcDatetime,
)

router = APIRouter(prefix="/api/v1", tags=["traceability"])


@router.get("/health")
def health() -> dict:
    with get_conn() as conn:
        return {"status": "ok", "version": service.current_version(conn)}


@router.get("/status")
def status() -> dict:
    """服务总览：当前单调版本与结论枚举。"""
    with get_conn() as conn:
        return {
            "current_version": service.current_version(conn),
            "conclusion_statuses": list(service.CONCLUSIONS),
        }


# ---------------- 证书：签发 / 纠正 / 撤销

@router.post("/certificates")
def issue_certificate(req: CertIssueRequest) -> dict:
    return service.issue_certificate(req.model_dump())


@router.post("/certificates/{certificate_no}/correct")
def correct_certificate(certificate_no: str, req: CertCorrectRequest) -> dict:
    data = req.model_dump()
    if data["certificate_no"] != certificate_no:
        raise ApiError(422, "path_body_mismatch",
                       "certificate_no in path and body must match")
    return service.correct_certificate(data)


@router.post("/certificates/{certificate_no}/revoke")
def revoke_certificate(certificate_no: str, req: CertRevokeRequest) -> dict:
    data = req.model_dump()
    if data["certificate_no"] != certificate_no:
        raise ApiError(422, "path_body_mismatch",
                       "certificate_no in path and body must match")
    return service.revoke_certificate(data)


# ---------------- 证书查询

@router.get("/certificates")
def list_certificates(
    at_version: Optional[int] = Query(default=None, ge=1),
    device_no: Optional[str] = Query(default=None, max_length=128),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    with get_conn() as conn:
        return service.list_certificates(conn, at_version, device_no, limit, offset)


@router.get("/certificates/{certificate_no}")
def get_certificate(
    certificate_no: str,
    at_version: Optional[int] = Query(default=None, ge=1),
) -> dict:
    with get_conn() as conn:
        out = service.get_certificate(conn, certificate_no, at_version)
        if out is None:
            raise ApiError(404, "unknown_certificate",
                           f"certificate {certificate_no!r} not found")
        return out


@router.get("/certificates/{certificate_no}/history")
def certificate_history(certificate_no: str) -> dict:
    with get_conn() as conn:
        return service.certificate_history(conn, certificate_no)


# ---------------- 读数：提交与查询

@router.post("/readings")
def submit_readings(req: ReadingSubmitRequest) -> dict:
    return service.submit_readings(req.model_dump())


@router.get("/readings")
def list_readings(
    at_version: Optional[int] = Query(default=None, ge=1),
    device_no: Optional[str] = Query(default=None, max_length=128),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    with get_conn() as conn:
        return service.list_readings(conn, at_version, device_no, status_filter, limit, offset)


@router.get("/readings/{reading_no}")
def get_reading(
    reading_no: str,
    at_version: Optional[int] = Query(default=None, ge=1),
) -> dict:
    with get_conn() as conn:
        out = service.get_reading(conn, reading_no, at_version)
        if out is None:
            raise ApiError(404, "unknown_reading", f"reading {reading_no!r} not found")
        return out


# ---------------- 版本与请求追溯

@router.get("/versions")
def list_versions(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    with get_conn() as conn:
        return service.list_versions(conn, limit, offset)


@router.get("/requests/{request_id}")
def get_request(request_id: str) -> dict:
    with get_conn() as conn:
        out = service.get_request(conn, request_id)
        if out is None:
            raise ApiError(404, "unknown_request",
                           f"request_id {request_id!r} not found")
        return out


# ---------------- 审计取证追溯包

@router.get("/audit/packages")
def get_audit_package(
    device_no: str = Query(min_length=1, max_length=128),
    measured_from: UtcDatetime = Query(
        description="测量时间区间起点（含），必须带时区，如 2026-01-01T00:00:00Z"
    ),
    measured_to: UtcDatetime = Query(
        description="测量时间区间终点（不含），必须带时区"
    ),
    at_version: int = Query(
        ge=1,
        description="取证版本：只收录该版本前（含）已提交的读数，结论按该版本视角评估",
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """生成审计取证追溯包（只读）。

    - 左闭右开测量时间区间 [measured_from, measured_to)，多时区输入归一化为 UTC；
    - 同条件同版本：内容与 package_digest 恒定，与生成时刻、之后的提交无关；
    - 大量条目以 limit/offset 分段，按 (measured_at, reading_no) 稳定排序，
      包摘要始终覆盖全部条目，分段另有 segment_digest 便于拼接校验；
    - 无匹配读数返回空包（200，total_count=0）；版本越界 / 无效区间返回 422。
    """
    return service.build_audit_package(
        device_no, measured_from, measured_to, at_version, limit, offset
    )
