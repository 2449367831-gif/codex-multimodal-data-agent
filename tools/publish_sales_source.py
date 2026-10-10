"""Prepare only the requester-approved sales and turnover semantic changes."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.publish_inventory_source import prepare as prepare_contracts


def prepare(root, release_id):
    return prepare_contracts(root, release_id,
        metric_names=("sales.business_quantity.yaml", "inventory.turnover_7d_realtime.v5.yaml",
                      "inventory.turnover_finished_goods_realtime.v3.yaml", "inventory.turnover_finished_goods_snapshot.v2.yaml"),
        supporting_files=("schemas/metric-contract.schema.json", "sources/ywzt_ods/shipment.yaml", "rules/sales-default.yaml", "knowledge/sales_user_sql_20261010.sql"),
        profile_overrides={
            "filters": {"outbound": {"source_metric":"sales.business_quantity@1.0.0",
                "window":"Centers 7 days, subwarehouses 14 days for the approved image; other queries use selected half-open window.",
                "formula":"Ordinary ship_qty plus community-platform request_qty at created minus one day.",
                "aggregation":"Independent facts; contributing header/master code uniqueness required."}},
            "freshness": {"outbound":"Catalog ingestion of shipment and order header/detail plus store master."},
        })


if __name__ == '__main__':
    print(prepare(ROOT, sys.argv[1]))
