from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
METRIC_FILES = {
    "sales_order.so07_cloud_warehouse.yaml": "sales_order.so07_cloud_warehouse",
    "sales.so07_cloud_warehouse_outbound.yaml": "sales.so07_cloud_warehouse_outbound",
    "inventory.so07_cloud_warehouse.yaml": "inventory.so07_cloud_warehouse",
    "purchase.so07_cloud_warehouse_inbound.yaml": "purchase.so07_cloud_warehouse_inbound",
}


def load_metric(filename):
    return yaml.safe_load((ROOT / "metrics" / filename).read_text(encoding="utf-8"))


def test_community_cloud_metrics_cover_separate_business_facts():
    metrics = {load_metric(filename)["id"]: load_metric(filename) for filename in METRIC_FILES}
    assert set(metrics) == set(METRIC_FILES.values())
    assert all(metric["status"] == "candidate" for metric in metrics.values())
    assert metrics["sales_order.so07_cloud_warehouse"]["filters"]["order_type"].startswith("h.order_type")
    assert metrics["sales.so07_cloud_warehouse_outbound"]["quantity_field"] == "ship_qty"
    assert metrics["sales.so07_cloud_warehouse_outbound"]["valid_status"] == [700, 900, 701, 901]
    assert metrics["inventory.so07_cloud_warehouse"]["formula"] == "SUM(available_quantity + in_transit_quantity)"
    assert metrics["purchase.so07_cloud_warehouse_inbound"]["filters"]["purchase_type"].endswith("warehous_type = '10'; this is the approved current basis.")


def test_cloud_warehouse_does_not_use_wms_status_as_a_global_filter():
    source = yaml.safe_load(
        (ROOT / "sources" / "ywzt_ods" / "community_cloud_warehouse.yaml").read_text(encoding="utf-8")
    )
    rule = source["semantic_separation"]["cloud_warehouse"]
    assert "wms_type='12'" in rule
    assert "wms_status" in rule
    assert "10, 11 and 12" in rule
    for filename in METRIC_FILES:
        cloud_rule = load_metric(filename)["filters"]["cloud_warehouse"]
        assert "m.status = '0'" in cloud_rule
        assert "wms_status" in cloud_rule


def test_so07_cloud_outbound_never_substitutes_request_or_allocation_quantity():
    metric = load_metric("sales.so07_cloud_warehouse_outbound.yaml")
    text = " ".join(metric["reconciliation"])
    assert "ship_qty" in text
    assert "allocate_qty" in text
    assert "request_qty" in text
    assert "ref_order_type alone is not sufficient" in text


def test_source_map_keeps_order_outbound_inventory_and_receipt_keys_distinct():
    source = yaml.safe_load(
        (ROOT / "sources" / "ywzt_ods" / "community_cloud_warehouse.yaml").read_text(encoding="utf-8")
    )
    assert source["business_keys"]["order"] == ["company_code", "sales_order_code"]
    assert source["business_keys"]["outbound"] == ["company_code", "outbound_code"]
    assert source["business_keys"]["receipt"] == ["cargo_owner_id", "warehous_code"]
    assert source["business_keys"]["inventory"] == ["owner", "wms_desc", "sku_code"]
