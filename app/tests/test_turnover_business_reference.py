"""The supplied W37 workbook is a regression reference, not a live contract."""

from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def test_w37_reference_keeps_the_business_control_totals_and_candidate_status() -> None:
    metric = yaml.safe_load(
        (ROOT / "metrics/inventory.turnover_business_reference.v1.yaml").read_text(
            encoding="utf-8"
        )
    )

    assert metric["status"] == "candidate"
    assert metric["approval"]["state"] == "pending_business_approval"
    assert metric["time_window"]["source_window_days"] == 7
    assert "workbook_control_total" in metric["filters"]
    assert "2361818" in metric["filters"]["workbook_control_total"]
    assert "34048494" in metric["filters"]["workbook_control_total"]
    assert metric["filters"]["movement_unit"] == "seven_day_sales_total_ea"
    assert "seven_day_sales_total_ea" in metric["formula"]
    assert "inventory snapshot reconciliation" in metric["notes"]


def test_w37_reference_has_nine_centers_and_does_not_publish_an_active_alias() -> None:
    profile = yaml.safe_load(
        (ROOT / "profiles/hjxh_business_reference_centers.yaml").read_text(
            encoding="utf-8"
        )
    )
    active = yaml.safe_load(
        (ROOT / "metrics/inventory.turnover_7d_realtime.v2.yaml").read_text(
            encoding="utf-8"
        )
    )

    assert profile["status"] == "candidate"
    assert profile["scope"]["expected_center_count"] == 9
    assert any("广州顺德" in item["name"] for item in profile["scope"]["centers"])
    assert "近14天周转图片" not in active["aliases"]


def test_w37_reference_requires_a_replayable_inventory_snapshot() -> None:
    reference = (ROOT / "knowledge/turnover_business_reference_w37.md").read_text(
        encoding="utf-8"
    )

    assert "不能用实时库存替代业务文件快照" in reference
    assert "33,767,318 EA" in reference
    assert "34,048,494 EA" in reference
    assert "保持候选状态" in reference


def test_w37_reference_distinguishes_business_seven_day_source_from_live_fourteen_day_report() -> None:
    metric = yaml.safe_load(
        (ROOT / "metrics/inventory.turnover_business_reference.v1.yaml").read_text(
            encoding="utf-8"
        )
    )
    reference = (ROOT / "knowledge/turnover_business_reference_w37.md").read_text(
        encoding="utf-8"
    )

    assert metric["status"] == "candidate"
    assert metric["time_window"]["source_window_days"] == 7
    assert "真正14个完整自然日" in reference
    assert "不能默认复现此表" in reference
    assert "不能再次除以 7" in reference
