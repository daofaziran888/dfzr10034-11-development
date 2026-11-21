"""快照发布领域服务：把同一基线上的字段变更写成可引用、不可互换的版本快照。

设计目标（对账场景）：
- 两个对账小组从同一条基线（如 T12）各自发布字段变更，各自拿到独立的发布标识与凭据；
- 每份快照都落下共同的父版本与完整字段副本，任一组都拿不到对方的父版本之外的信息，
  凭据跨组不可互换，重放凭据不会覆盖或抹去对方的证据；
- 同一组在同一基线上重复提交只保留一份「已确认」快照（幂等去重）；
- 变更捕获按发布标识汇总前后值，随快照一起持久化；主版本指针后续推进后，
  仍可凭发布标识重新生成当初的差异。
"""
from __future__ import annotations

import copy
import secrets
import time as _time
from typing import Any

from app.services.base import BaseModuleService
from app.services.change_capture import ChangeCaptureService
from app.store import store

# 允许参与对账的两个小组；凭据、发布标识都按小组隔离。
RECONCILIATION_GROUPS = ("currency_group", "finance_group")
GROUP_LABELS = {
    "currency_group": "币种字典小组",
    "finance_group": "财务精度小组",
}


class SnapshotReleaseError(ValueError):
    """快照发布业务校验失败（路由层翻译成 4xx）。"""


class SnapshotReleaseService(BaseModuleService):
    MODULE = "snapshot_release"
    ENTITY = "快照发布"
    OPERATION_TERM = "发布"
    LIST_FIELDS = [
        "发布编号", "目标资产", "基线标签", "父版本", "对账小组",
        "发布标识", "凭据", "字段变更", "发布时间",
    ]
    REQUIRED_FIELDS = ["发布编号", "目标资产", "对账小组"]
    STATUS_ORDER = ["已确认", "主版本", "已归档"]
    ACTION_RULES = {"推进主版本": "主版本"}
    NEGATIVE_ACTIONS = []

    # ── 基线 ──────────────────────────────────────────────────

    def register_baseline(
        self,
        asset_code: str,
        baseline_tag: str,
        fields: dict[str, Any],
        schema: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """登记一条版本基线，后续发布均以该基线为共同父版本。"""
        existing = store.get_baseline(asset_code, baseline_tag)
        if existing is not None:
            raise SnapshotReleaseError(
                f"基线 {baseline_tag} 已存在，资产 {asset_code} 不能重复登记基线"
            )
        baseline = {
            "asset_code": asset_code,
            "baseline_tag": baseline_tag,
            "fields": copy.deepcopy(fields),
            "schema": copy.deepcopy(schema or {}),
            "created_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        store.save_baseline(baseline)
        return copy.deepcopy(baseline)

    def _require_baseline(self, asset_code: str, baseline_tag: str) -> dict[str, Any]:
        baseline = store.get_baseline(asset_code, baseline_tag)
        if baseline is None:
            raise SnapshotReleaseError(
                f"资产 {asset_code} 的基线 {baseline_tag} 不存在，无法发布快照"
            )
        return baseline

    # ── 发布 ──────────────────────────────────────────────────

    @staticmethod
    def _normalize_changes(field_changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for item in field_changes:
            field = str(item.get("field") or "").strip()
            if not field:
                raise SnapshotReleaseError("字段变更必须给出 field")
            normalized.append({
                "field": field,
                "before": item.get("before", ""),
                "after": item.get("after", ""),
                "schema_before": item.get("schema_before", ""),
                "schema_after": item.get("schema_after", ""),
            })
        if not normalized:
            raise SnapshotReleaseError("发布快照至少包含一笔字段变更")
        return normalized

    def _find_group_release(
        self, asset_code: str, baseline_tag: str, group: str
    ) -> dict[str, Any] | None:
        for release in store.list_releases(asset_code):
            if (
                release.get("基线标签") == baseline_tag
                and release.get("对账小组") == group
            ):
                return release
        return None

    def publish_release(
        self,
        asset_code: str,
        baseline_tag: str,
        group: str,
        field_changes: list[dict[str, Any]],
        receipt_token: str | None = None,
        source: str = "schema_evolution",
    ) -> dict[str, Any]:
        """在指定基线上发布一组字段变更，返回不可变快照与对账凭据。

        同一小组在同一基线上重复提交：只保留首份「已确认」快照，不覆盖、不新增。
        """
        if group not in RECONCILIATION_GROUPS:
            raise SnapshotReleaseError(
                f"对账小组必须是 {RECONCILIATION_GROUPS} 之一，收到 {group!r}"
            )
        baseline = self._require_baseline(asset_code, baseline_tag)
        changes = self._normalize_changes(field_changes)

        # 幂等去重：同组同基线只允许一份已确认快照。
        existing = self._find_group_release(asset_code, baseline_tag, group)
        if existing is not None:
            return {
                "release": existing,
                "receipt_token": existing["凭据"],
                "deduplicated": True,
                "message": "该小组在此基线上的快照已确认，重复提交未产生新版本",
            }

        # 若调用方自带凭据，必须是未被占用的；否则由服务签发。
        token = (receipt_token or f"RC-{group}-{secrets.token_hex(8)}").strip()
        if not token:
            raise SnapshotReleaseError("凭据不能为空")
        if store.get_receipt_binding(token) is not None:
            raise SnapshotReleaseError("凭据已被其他发布占用，不能复用")

        parent_snapshot = copy.deepcopy(baseline)
        merged_fields = copy.deepcopy(baseline["fields"])
        merged_schema = copy.deepcopy(baseline["schema"])
        before_summary: dict[str, Any] = {}
        after_summary: dict[str, Any] = {}
        for change in changes:
            field = change["field"]
            before_summary[field] = copy.deepcopy(merged_fields.get(field, change["before"]))
            merged_fields[field] = change["after"]
            after_summary[field] = copy.deepcopy(change["after"])
            if change["schema_after"]:
                if field not in merged_schema or change["schema_before"]:
                    change["schema_before"] = change["schema_before"] or merged_schema.get(field, "")
                merged_schema[field] = change["schema_after"]

        seq = store.next_id(self.MODULE)
        release_id = f"REL-{seq:04d}"
        now = _time.strftime("%Y-%m-%d %H:%M:%S")
        release = {
            "id": seq,
            "发布编号": release_id,
            "发布标识": release_id,
            "目标资产": asset_code,
            "基线标签": baseline_tag,
            "父版本": baseline_tag,
            "对账小组": group,
            "小组名称": GROUP_LABELS[group],
            "凭据": token,
            "字段变更": changes,
            "字段快照": merged_fields,
            "Schema快照": merged_schema,
            "父版本快照": parent_snapshot,
            "变更汇总": {"before": before_summary, "after": after_summary},
            "字段变更摘要": ", ".join(
                f"{c['field']}:{c['before']}->{c['after']}" for c in changes
            ),
            "发布时间": now,
            "status": "已确认",
            "pending": False,
            "abnormal": False,
            "created_at": now,
        }
        store.save_release(release)
        store.rows(self.MODULE).append(release)
        store.bind_receipt(token, {
            "release_id": release_id,
            "group": group,
            "asset_code": asset_code,
            "baseline_tag": baseline_tag,
        })

        # 变更捕获：按发布标识落下前后值，并生成差异报告。
        capture = ChangeCaptureService()
        capture.capture_release_changes(release, source=source)

        # 首次发布即把资产主版本指针挂到该资产（后续可再推进）。
        if store.get_head(asset_code) is None:
            store.set_head(asset_code, release_id)

        return {
            "release": release,
            "receipt_token": token,
            "deduplicated": False,
            "message": "快照已发布并确认",
        }

    # ── 凭据校验与读取 ────────────────────────────────────────

    def _authorize(self, receipt_token: str, release_id: str | None = None) -> dict[str, Any]:
        binding = store.get_receipt_binding(receipt_token or "")
        if binding is None:
            raise SnapshotReleaseError("凭据无效或已失效，无法读取发布结果")
        if release_id is not None and binding["release_id"] != release_id:
            raise SnapshotReleaseError(
                "凭据与发布标识不匹配：两组凭据不能互换，拒绝跨发布读取"
            )
        release = store.get_release(binding["release_id"])
        if release is None:
            raise SnapshotReleaseError("凭据指向的发布快照不存在")
        if release["对账小组"] != binding["group"]:
            raise SnapshotReleaseError("凭据绑定小组与快照归属小组不一致")
        return release

    def replay_receipt(self, receipt_token: str) -> dict[str, Any]:
        """重放回执：只回读本凭据对应的快照，不修改任何既有证据。"""
        release = self._authorize(receipt_token)
        return {
            "release": release,
            "receipt_token": receipt_token,
            "replayed": True,
            "message": "回执重放成功，未改动任何快照",
        }

    def read_release(self, release_id: str, receipt_token: str) -> dict[str, Any]:
        """凭凭据读取指定发布；凭据不属于该发布即拒绝。"""
        return self._authorize(receipt_token, release_id)

    def restore_ledger(self, receipt_token: str) -> dict[str, Any]:
        """凭凭据还原账本：返回发布时落下的完整字段副本与父版本。"""
        release = self._authorize(receipt_token)
        return {
            "发布编号": release["发布编号"],
            "发布标识": release["发布标识"],
            "目标资产": release["目标资产"],
            "父版本": release["父版本"],
            "对账小组": release["对账小组"],
            "小组名称": release["小组名称"],
            "字段快照": copy.deepcopy(release["字段快照"]),
            "Schema快照": copy.deepcopy(release["Schema快照"]),
            "字段变更": copy.deepcopy(release["字段变更"]),
            "发布时间": release["发布时间"],
        }

    # ── 字段追踪 ──────────────────────────────────────────────

    @staticmethod
    def _decimal_precision(value: Any) -> int:
        text = str(value)
        return len(text.split(".", 1)[1]) if "." in text else 0

    def trace_field(
        self,
        receipt_token: str,
        field_name: str,
        expected_value: str | None = None,
    ) -> dict[str, Any]:
        """凭凭据追踪某字段的取值变迁，可断言当前快照值与小数精度。"""
        release = self._authorize(receipt_token)
        parent_fields = release["父版本快照"]["fields"]
        timeline: list[dict[str, Any]] = []
        if field_name in parent_fields:
            timeline.append({
                "版本": release["父版本"],
                "字段": field_name,
                "取值": parent_fields[field_name],
                "节点": "父版本基线",
            })
        for change in release["字段变更"]:
            if change["field"] == field_name:
                timeline.append({
                    "版本": release["发布编号"],
                    "字段": field_name,
                    "取值": change["after"],
                    "前值": change["before"],
                    "节点": "发布快照",
                    "发布时间": release["发布时间"],
                    "schema": change.get("schema_after", ""),
                })

        current_value = release["字段快照"].get(field_name)
        result: dict[str, Any] = {
            "发布编号": release["发布编号"],
            "目标资产": release["目标资产"],
            "字段": field_name,
            "当前取值": current_value,
            "变迁时间线": timeline,
            "小数精度": self._decimal_precision(current_value)
            if current_value is not None else None,
        }
        if expected_value is not None:
            result["期望值"] = expected_value
            result["取值匹配"] = str(current_value) == str(expected_value)
            if not result["取值匹配"]:
                raise SnapshotReleaseError(
                    f"字段 {field_name} 当前取值 {current_value!r} 与期望 {expected_value!r} 不符"
                )
        return result

    # ── 跨组对比 ──────────────────────────────────────────────

    def compare_releases(
        self,
        receipt_a: str,
        receipt_b: str,
    ) -> dict[str, Any]:
        """两组各持自己的凭据，从各自发布版本比较字段差异并核对共同父版本。"""
        release_a = self._authorize(receipt_a)
        release_b = self._authorize(receipt_b)
        if release_a["发布编号"] == release_b["发布编号"]:
            raise SnapshotReleaseError("对比必须来自两份不同的发布快照")
        if release_a["目标资产"] != release_b["目标资产"]:
            raise SnapshotReleaseError("两份发布不属于同一资产，无法对账")

        fields = sorted(
            set(release_a["字段快照"]) | set(release_b["字段快照"])
        )
        field_diffs = [
            {
                "字段": field,
                release_a["对账小组"]: release_a["字段快照"].get(field),
                release_b["对账小组"]: release_b["字段快照"].get(field),
                "一致": release_a["字段快照"].get(field)
                == release_b["字段快照"].get(field),
            }
            for field in fields
        ]
        return {
            "资产": release_a["目标资产"],
            "发布甲": {
                "发布编号": release_a["发布编号"],
                "对账小组": release_a["对账小组"],
                "版本时间": release_a["发布时间"],
            },
            "发布乙": {
                "发布编号": release_b["发布编号"],
                "对账小组": release_b["对账小组"],
                "版本时间": release_b["发布时间"],
            },
            "父版本甲": release_a["父版本"],
            "父版本乙": release_b["父版本"],
            "共同父版本": release_a["父版本"]
            if release_a["父版本"] == release_b["父版本"] else None,
            "父版本一致": release_a["父版本"] == release_b["父版本"],
            "字段差异": field_diffs,
        }

    # ── 主版本推进与差异再生成 ────────────────────────────────

    def advance_head(self, release_id: str, receipt_token: str | None = None) -> dict[str, Any]:
        """把某份发布推进为资产主版本；推进不影响其它发布及其父版本证据。"""
        if receipt_token is None:
            release = store.get_release(release_id)
            if release is None:
                raise SnapshotReleaseError(f"发布 {release_id} 不存在")
        else:
            release = self._authorize(receipt_token, release_id)
        store.set_head(release["目标资产"], release_id)
        release["status"] = "主版本"
        return {
            "目标资产": release["目标资产"],
            "主版本": release_id,
            "推进时间": _time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    def regenerate_release_diff(self, release_id: str, receipt_token: str) -> dict[str, Any]:
        """凭凭据依据快照内的父版本副本重新生成当初的前后值差异。"""
        release = self._authorize(receipt_token, release_id)
        parent_fields = release["父版本快照"]["fields"]
        current_fields = release["字段快照"]
        rebuilt_before: dict[str, Any] = {}
        rebuilt_after: dict[str, Any] = {}
        field_diffs: list[dict[str, Any]] = []
        for change in release["字段变更"]:
            field = change["field"]
            rebuilt_before[field] = parent_fields.get(field, change["before"])
            rebuilt_after[field] = current_fields.get(field, change["after"])
            field_diffs.append({
                "字段": field,
                "前值": rebuilt_before[field],
                "后值": rebuilt_after[field],
                "schema前值": change.get("schema_before", ""),
                "schema后值": change.get("schema_after", ""),
            })
        stored = release["变更汇总"]
        return {
            "发布编号": release_id,
            "父版本": release["父版本"],
            "变更前值汇总": rebuilt_before,
            "变更后值汇总": rebuilt_after,
            "字段差异": field_diffs,
            "与原快照一致": (
                rebuilt_before == stored["before"]
                and rebuilt_after == stored["after"]
            ),
            "当前主版本": store.get_head(release["目标资产"]),
        }

    # ── 工作台动作入口 ────────────────────────────────────────

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        if action == "推进主版本":
            result = self.advance_head(entry["发布编号"])
            return entry, f"主版本已推进到 {result['主版本']}"
        return super().run_action(entry_id, action)
