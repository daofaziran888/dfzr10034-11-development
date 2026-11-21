"""快照发布专用接口：基线登记、发布、凭据回读、账本还原、字段追踪与版本对比。

通用列表/动作仍走 resource 路由（模块 key = snapshot_release）；
这里只承载带业务语义、需要凭据校验的对账动作。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services.change_capture import ChangeCaptureService
from app.services.snapshot_release import (
    SnapshotReleaseError,
    SnapshotReleaseService,
)

router = APIRouter(prefix="/api/snapshot-releases", tags=["快照发布"])

service = SnapshotReleaseService()


class BaselinePayload(BaseModel):
    asset_code: str
    baseline_tag: str
    fields: dict[str, Any] = Field(default_factory=dict)
    schema_: dict[str, str] = Field(default_factory=dict, alias="schema")

    model_config = {"populate_by_name": True}


class PublishPayload(BaseModel):
    asset_code: str
    baseline_tag: str
    group: str
    field_changes: list[dict[str, Any]]
    receipt_token: str | None = None
    source: str = "snapshot_release"


class ReceiptPayload(BaseModel):
    receipt_token: str


class ReadPayload(ReceiptPayload):
    release_id: str


class TracePayload(ReceiptPayload):
    field_name: str
    expected_value: str | None = None


class ComparePayload(BaseModel):
    receipt_a: str
    receipt_b: str


class AdvancePayload(ReadPayload):
    pass


def _error(exc: SnapshotReleaseError) -> dict[str, Any]:
    return {"ok": False, "message": str(exc)}


@router.post("/baselines")
def register_baseline(payload: BaselinePayload) -> dict[str, Any]:
    try:
        baseline = service.register_baseline(
            payload.asset_code, payload.baseline_tag,
            payload.fields, payload.schema_,
        )
    except SnapshotReleaseError as exc:
        return _error(exc)
    return {"ok": True, "message": "基线已登记", "baseline": baseline}


@router.post("/publish")
def publish(payload: PublishPayload) -> dict[str, Any]:
    try:
        result = service.publish_release(
            payload.asset_code, payload.baseline_tag, payload.group,
            payload.field_changes, payload.receipt_token, payload.source,
        )
    except SnapshotReleaseError as exc:
        return _error(exc)
    release = result["release"]
    return {
        "ok": True,
        "message": result["message"],
        "deduplicated": result["deduplicated"],
        "receipt_token": result["receipt_token"],
        "release": _public_release(release),
    }


@router.post("/replay")
def replay(payload: ReceiptPayload) -> dict[str, Any]:
    try:
        result = service.replay_receipt(payload.receipt_token)
    except SnapshotReleaseError as exc:
        return _error(exc)
    return {"ok": True, **result, "release": _public_release(result["release"])}


@router.post("/read")
def read_release(payload: ReadPayload) -> dict[str, Any]:
    try:
        release = service.read_release(payload.release_id, payload.receipt_token)
    except SnapshotReleaseError as exc:
        return _error(exc)
    return {"ok": True, "release": _public_release(release)}


@router.post("/restore-ledger")
def restore_ledger(payload: ReceiptPayload) -> dict[str, Any]:
    try:
        ledger = service.restore_ledger(payload.receipt_token)
    except SnapshotReleaseError as exc:
        return _error(exc)
    return {"ok": True, "ledger": ledger}


@router.post("/trace-field")
def trace_field(payload: TracePayload) -> dict[str, Any]:
    try:
        trace = service.trace_field(
            payload.receipt_token, payload.field_name, payload.expected_value,
        )
    except SnapshotReleaseError as exc:
        return _error(exc)
    return {"ok": True, "trace": trace}


@router.post("/compare")
def compare(payload: ComparePayload) -> dict[str, Any]:
    try:
        result = service.compare_releases(payload.receipt_a, payload.receipt_b)
    except SnapshotReleaseError as exc:
        return _error(exc)
    return {"ok": True, "comparison": result}


@router.post("/advance-head")
def advance_head(payload: AdvancePayload) -> dict[str, Any]:
    try:
        result = service.advance_head(payload.release_id, payload.receipt_token)
    except SnapshotReleaseError as exc:
        return _error(exc)
    return {"ok": True, **result}


@router.post("/regenerate-diff")
def regenerate_diff(payload: ReadPayload) -> dict[str, Any]:
    try:
        result = service.regenerate_release_diff(
            payload.release_id, payload.receipt_token,
        )
    except SnapshotReleaseError as exc:
        return _error(exc)
    return {"ok": True, "diff": result}


@router.get("/changes/{release_id}")
def read_release_changes(release_id: str) -> dict[str, Any]:
    """变更捕获：读取发布标识生成的前后值汇总。"""
    summary = ChangeCaptureService().read_release_changes(release_id)
    if summary["count"] == 0:
        return {"ok": False, "message": f"发布 {release_id} 没有对应的变更捕获记录"}
    return {"ok": True, "summary": summary}


def _public_release(release: dict[str, Any]) -> dict[str, Any]:
    """对外裁剪掉父版本完整副本等内部结构，保留对账所需字段。"""
    return {
        key: release[key]
        for key in (
            "发布编号", "发布标识", "目标资产", "基线标签", "父版本",
            "对账小组", "小组名称", "凭据", "字段变更", "字段快照",
            "Schema快照", "变更汇总", "字段变更摘要", "发布时间", "status",
        )
        if key in release
    }
