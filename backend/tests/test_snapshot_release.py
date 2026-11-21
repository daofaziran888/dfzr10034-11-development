"""快照发布对账测试：两个小组从同一基线 T12 并发发布字段变更的完整对账闭环。

叙事：
- 基线 T12：currency 为三字符（USD），net_price 小数位为二位（7.12）；
- 币种字典小组把 currency 由三字符扩成四字符（USD -> USDX）；
- 财务小组同日把 net_price 小数位从二位调至四位（7.12 -> 7.1250）；
- 两枚发布标识各自还原账本、追踪字段，凭据不能互换，重放不抹除对方证据；
- 变更捕获按发布标识生成前后值汇总，主版本推进后仍可重新生成原差异。
"""
import pytest

import app.store as store_mod
from app.services.change_capture import ChangeCaptureService
from app.services.snapshot_release import (
    SnapshotReleaseError,
    SnapshotReleaseService,
)

ASSET = "ORD_LEDGER"
BASELINE = "T12"


def _fresh_service():
    store_mod.store.reset()
    return SnapshotReleaseService()


def _publish_two_groups(svc):
    """两组从同一基线分别发布，返回各自结果。"""
    currency = svc.publish_release(
        ASSET, BASELINE, "currency_group",
        [{
            "field": "currency",
            "before": "USD",
            "after": "USDX",
            "schema_before": "VARCHAR(3)",
            "schema_after": "VARCHAR(4)",
        }],
        receipt_token="TOKEN-CURRENCY-001",
    )
    finance = svc.publish_release(
        ASSET, BASELINE, "finance_group",
        [{
            "field": "net_price",
            "before": "7.12",
            "after": "7.1250",
            "schema_before": "DECIMAL(12,2)",
            "schema_after": "DECIMAL(12,4)",
        }],
        receipt_token="TOKEN-FINANCE-001",
    )
    return currency, finance


class TestBaseline:

    def test_t12_baseline_seeded_with_three_char_currency_and_two_digits(self):
        svc = _fresh_service()
        baseline = store_mod.store.get_baseline(ASSET, BASELINE)
        assert baseline is not None
        assert baseline["fields"]["currency"] == "USD"
        assert baseline["schema"]["currency"] == "VARCHAR(3)"
        assert baseline["fields"]["net_price"] == "7.12"
        assert baseline["schema"]["net_price"] == "DECIMAL(12,2)"

    def test_duplicate_baseline_rejected(self):
        svc = _fresh_service()
        with pytest.raises(SnapshotReleaseError):
            svc.register_baseline(ASSET, BASELINE, {"currency": "USD"})


class TestParallelPublishing:

    def test_two_groups_get_distinct_releases_off_same_parent(self):
        svc = _fresh_service()
        currency, finance = _publish_two_groups(svc)

        assert currency["deduplicated"] is False
        assert finance["deduplicated"] is False
        assert currency["release"]["发布编号"] != finance["release"]["发布编号"]
        # 同一基线 = 共同父版本，且两组都看不到对方的版本作为自己的父版本
        assert currency["release"]["父版本"] == BASELINE
        assert finance["release"]["父版本"] == BASELINE
        assert currency["release"]["对账小组"] == "currency_group"
        assert finance["release"]["对账小组"] == "finance_group"

    def test_neither_release_uses_other_as_parent(self):
        svc = _fresh_service()
        currency, finance = _publish_two_groups(svc)
        currency_id = currency["release"]["发布编号"]
        finance_id = finance["release"]["发布编号"]
        # 财务组发布的父版本不能是币种组的发布，反之亦然
        assert finance["release"]["父版本"] != currency_id
        assert currency["release"]["父版本"] != finance_id

    def test_receipts_are_bound_to_own_release_only(self):
        svc = _fresh_service()
        currency, finance = _publish_two_groups(svc)
        assert currency["receipt_token"] == "TOKEN-CURRENCY-001"
        assert finance["receipt_token"] == "TOKEN-FINANCE-001"
        binding_c = store_mod.store.get_receipt_binding("TOKEN-CURRENCY-001")
        binding_f = store_mod.store.get_receipt_binding("TOKEN-FINANCE-001")
        assert binding_c["group"] == "currency_group"
        assert binding_f["group"] == "finance_group"
        assert binding_c["release_id"] != binding_f["release_id"]

    def test_credentials_cannot_be_cross_used(self):
        svc = _fresh_service()
        currency, finance = _publish_two_groups(svc)
        currency_id = currency["release"]["发布编号"]
        finance_id = finance["release"]["发布编号"]

        # 币种组凭据不能读财务组发布
        with pytest.raises(SnapshotReleaseError):
            svc.read_release(finance_id, "TOKEN-CURRENCY-001")
        # 财务组凭据不能读币种组发布
        with pytest.raises(SnapshotReleaseError):
            svc.read_release(currency_id, "TOKEN-FINANCE-001")

    def test_unknown_receipt_rejected(self):
        svc = _fresh_service()
        with pytest.raises(SnapshotReleaseError):
            svc.replay_receipt("NOT-A-REAL-TOKEN")

    def test_duplicate_publish_is_idempotent_single_confirmed_snapshot(self):
        svc = _fresh_service()
        first, _ = _publish_two_groups(svc)
        first_id = first["release"]["发布编号"]

        # 币种组在同一基线上重复提交（甚至带一张新凭据）
        repeat = svc.publish_release(
            ASSET, BASELINE, "currency_group",
            [{"field": "currency", "before": "USD", "after": "USDX"}],
            receipt_token="TOKEN-CURRENCY-RETRY",
        )
        assert repeat["deduplicated"] is True
        assert repeat["release"]["发布编号"] == first_id
        # 没有新增发布记录，仍是一份已确认快照
        group_releases = [
            r for r in store_mod.store.list_releases(ASSET)
            if r["对账小组"] == "currency_group"
        ]
        assert len(group_releases) == 1
        assert group_releases[0]["status"] == "已确认"
        # 重试带来的新凭据不得被绑定（不能顶替或分裂原凭据）
        assert store_mod.store.get_receipt_binding("TOKEN-CURRENCY-RETRY") is None

    def test_replay_does_not_erase_other_groups_evidence(self):
        svc = _fresh_service()
        currency, finance = _publish_two_groups(svc)

        # 重放币种组回执多次
        for _ in range(3):
            replayed = svc.replay_receipt("TOKEN-CURRENCY-001")
            assert replayed["replayed"] is True
            assert replayed["release"]["发布编号"] == currency["release"]["发布编号"]

        # 财务组证据仍可完整读取
        finance_ledger = svc.restore_ledger("TOKEN-FINANCE-001")
        assert finance_ledger["字段快照"]["net_price"] == "7.1250"
        currency_ledger = svc.restore_ledger("TOKEN-CURRENCY-001")
        assert currency_ledger["字段快照"]["currency"] == "USDX"


class TestLedgerRestoreAndTrace:

    def test_currency_group_finds_when_usdx_became_legal(self):
        svc = _fresh_service()
        _publish_two_groups(svc)
        trace = svc.trace_field(
            "TOKEN-CURRENCY-001", "currency", expected_value="USDX",
        )
        assert trace["取值匹配"] is True
        assert trace["当前取值"] == "USDX"
        # 时间线先父版本 USD，再发布 USDX，能定位“何时变成合法取值”
        timeline = trace["变迁时间线"]
        assert timeline[0]["版本"] == BASELINE
        assert timeline[0]["取值"] == "USD"
        assert timeline[-1]["取值"] == "USDX"
        assert timeline[-1]["schema"] == "VARCHAR(4)"

    def test_finance_group_traces_four_digit_precision(self):
        svc = _fresh_service()
        _publish_two_groups(svc)
        trace = svc.trace_field(
            "TOKEN-FINANCE-001", "net_price", expected_value="7.1250",
        )
        assert trace["取值匹配"] is True
        assert trace["小数精度"] == 4
        parent_node = trace["变迁时间线"][0]
        assert parent_node["取值"] == "7.12"
        assert svc._decimal_precision("7.12") == 2

    def test_restore_ledger_returns_full_snapshot_and_parent(self):
        svc = _fresh_service()
        _publish_two_groups(svc)
        ledger = svc.restore_ledger("TOKEN-CURRENCY-001")
        assert ledger["父版本"] == BASELINE
        # 账本还原保留未变更字段（来自父版本）+ 本小组变更字段
        assert ledger["字段快照"]["order_id"] == "10001"
        assert ledger["字段快照"]["currency"] == "USDX"
        assert ledger["字段快照"]["net_price"] == "7.12"  # 币种组未动精度列

    def test_trace_with_wrong_expected_value_raises(self):
        svc = _fresh_service()
        _publish_two_groups(svc)
        with pytest.raises(SnapshotReleaseError):
            svc.trace_field("TOKEN-FINANCE-001", "net_price", "9.9999")


class TestCompareReleases:

    def test_compare_from_each_versions_shows_field_diffs_and_common_parent(self):
        svc = _fresh_service()
        _publish_two_groups(svc)
        result = svc.compare_releases("TOKEN-CURRENCY-001", "TOKEN-FINANCE-001")
        assert result["父版本一致"] is True
        assert result["共同父版本"] == BASELINE
        diffs = {d["字段"]: d for d in result["字段差异"]}
        # 币种列与精度列都要参与对账
        assert diffs["currency"]["一致"] is False
        assert diffs["currency"]["currency_group"] == "USDX"
        assert diffs["currency"]["finance_group"] == "USD"
        assert diffs["net_price"]["一致"] is False
        assert diffs["net_price"]["finance_group"] == "7.1250"
        assert diffs["net_price"]["currency_group"] == "7.12"
        # 未变更字段保持一致
        assert diffs["order_id"]["一致"] is True


class TestChangeCaptureIntegration:

    def test_change_capture_summary_by_release_id(self):
        svc = _fresh_service()
        currency, finance = _publish_two_groups(svc)
        capture = ChangeCaptureService()

        summary_c = capture.read_release_changes(currency["release"]["发布编号"])
        assert summary_c["count"] == 1
        assert summary_c["before"]["currency"] == "USD"
        assert summary_c["after"]["currency"] == "USDX"
        assert summary_c["parent_version"] == BASELINE

        summary_f = capture.read_release_changes(finance["release"]["发布编号"])
        assert summary_f["before"]["net_price"] == "7.12"
        assert summary_f["after"]["net_price"] == "7.1250"

        # 两笔发布的变更捕获互不串线
        assert summary_c["group"] == "currency_group"
        assert summary_f["group"] == "finance_group"

    def test_diff_report_rows_linked_to_release(self):
        svc = _fresh_service()
        currency, _ = _publish_two_groups(svc)
        release_id = currency["release"]["发布编号"]
        diffs = [
            r for r in store_mod.store.rows("diff_report")
            if r.get("release_id") == release_id
        ]
        assert len(diffs) == 1
        assert diffs[0]["源值"] == "USD"
        assert diffs[0]["目标值"] == "USDX"
        assert diffs[0]["差异字段"] == "currency"


class TestHeadAdvanceAndDiffRegeneration:

    def test_regenerate_original_diff_after_head_advances(self):
        svc = _fresh_service()
        currency, finance = _publish_two_groups(svc)
        currency_id = currency["release"]["发布编号"]
        finance_id = finance["release"]["发布编号"]

        # 后来把主版本推进到财务组发布
        svc.advance_head(finance_id, "TOKEN-FINANCE-001")
        assert store_mod.store.get_head(ASSET) == finance_id

        # 币种组凭据仍可重新生成当初相对父版本 T12 的差异
        rebuilt = svc.regenerate_release_diff(currency_id, "TOKEN-CURRENCY-001")
        assert rebuilt["父版本"] == BASELINE
        assert rebuilt["变更前值汇总"]["currency"] == "USD"
        assert rebuilt["变更后值汇总"]["currency"] == "USDX"
        assert rebuilt["与原快照一致"] is True
        # 主版本已经推进，不影响原差异
        assert rebuilt["当前主版本"] == finance_id

    def test_advance_head_cannot_be_driven_by_other_groups_receipt(self):
        svc = _fresh_service()
        currency, finance = _publish_two_groups(svc)
        currency_id = currency["release"]["发布编号"]
        # 财务组凭据无权推进币种组发布
        with pytest.raises(SnapshotReleaseError):
            svc.advance_head(currency_id, "TOKEN-FINANCE-001")

    def test_other_release_still_readable_after_head_advance(self):
        svc = _fresh_service()
        currency, finance = _publish_two_groups(svc)
        svc.advance_head(finance["release"]["发布编号"], "TOKEN-FINANCE-001")
        # 主版本推进后，另一组已引用的发布结果仍需可读
        readable = svc.read_release(currency["release"]["发布编号"], "TOKEN-CURRENCY-001")
        assert readable["字段快照"]["currency"] == "USDX"
        assert readable["父版本"] == BASELINE


class TestWorkbenchSurface:

    def test_release_rows_listable_through_module_table(self):
        svc = _fresh_service()
        _publish_two_groups(svc)
        rows = store_mod.store.rows("snapshot_release")
        assert len(rows) == 2
        items, total = svc.list_entries(filters={"对账小组": "finance_group"})
        assert total == 1
        assert items[0]["对账小组"] == "finance_group"
        # 工作台动作：推进主版本
        target = items[0]
        entry, message = svc.run_action(target["id"], "推进主版本")
        assert entry is not None
        assert entry["status"] == "主版本"
        assert store_mod.store.get_head(ASSET) == target["发布编号"]
