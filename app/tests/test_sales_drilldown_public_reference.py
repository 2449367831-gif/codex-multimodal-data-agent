"""Public API acceptance must inspect every public row and field independently."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from decimal import Decimal, localcontext
import json

import pytest


@pytest.fixture
def verifier():
    spec = spec_from_file_location("public_drilldown_reference", Path(__file__).resolve().parents[2] / "tests/acceptance/check_sales_drilldown_chat_live.py")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def records():
    reference = [{"rank": 1, "sku_code": "000A", "sku_name": "Synthetic product", "sku_quantity": "9007199254740993.25", "outbound_count": 2,
                  "owner": owner, "outbound_code": "SAME", "warehouse_code": "W", "warehouse_name": name,
                  "customer_code": customer, "ship_at": "2026-09-10 12:00:00", "quantity": quantity}
                 for owner, name, customer, quantity in [("A", "Warehouse A", None, "9007199254740992.25"), ("B", "Warehouse B", "00001", "1")]]
    ranking = {"rank": "1", "sku_code": "000A", "sku_name": "Synthetic product", "quantity": "9007199254740993.25", "outbound_count": "2"}
    detail = [{"sku_code": row["sku_code"], "cargo_owner": row["owner"], "warehouse_code": row["warehouse_code"],
               "warehouse_name": row["warehouse_name"], "customer_code": row["customer_code"], "outbound_code": row["outbound_code"],
               "ship_at": "2026-09-10 12:00:00+08:00", "quantity": row["quantity"]} for row in reference]
    return ranking, detail, reference


def test_complete_public_detail_names_precision_and_owner_count(verifier, records):
    ranking, detail, reference = records
    assert all(verifier.compare_public_detail(ranking, detail, reference).values())
    assert all(verifier.compare_public_ranking([ranking], [reference[0]]).values())


@pytest.mark.parametrize('precision', [6, 28])
def test_detail_reconciliation_is_independent_of_decimal_context(verifier, records, precision):
    ranking, detail, reference = records
    detail[0]['quantity'] = reference[0]['quantity'] = '123456789012345678901234567890.25'
    ranking['quantity'] = '123456789012345678901234567891.25'
    for row in reference:
        row['sku_quantity'] = ranking['quantity']
    with localcontext() as context:
        context.prec = precision
        assert all(verifier.compare_public_detail(ranking, detail, reference).values())
        ranking['quantity'] = '123456789012345678901234567891.2501'
        assert not verifier.compare_public_detail(ranking, detail, reference)['quantity_matches_original_rank']


def test_ranking_summary_total_keeps_every_fractional_digit(verifier):
    rows = [{'quantity': '123456789012345678901234567890.25'}, {'quantity': '1'}]
    with localcontext() as context:
        context.prec = 6
        assert verifier.quantity_total(rows) == Decimal('123456789012345678901234567891.25')


def test_published_acceptance_uses_the_full_requested_question(verifier):
    cases = json.loads((Path(__file__).resolve().parents[2] / 'tests/acceptance/complex-query-cases.json').read_text(encoding='utf-8'))['cases']
    expected = next(case['question'] for case in cases if case['id'] == 'T38')
    assert verifier.acceptance_question() == expected
    assert '点击' in verifier.acceptance_question() and '回钻' in verifier.acceptance_question()


@pytest.mark.parametrize("field,value", [("warehouse_name", "Wrong"), ("sku_code", "OTHER"), ("customer_code", "1"), ("cargo_owner", "B"),
                                       ("quantity", "9007199254740992.26"), ("ship_at", "2026-09-10 00:00:00+08:00"), ("outbound_code", "OTHER")])
def test_wrong_public_field_never_passes(verifier, records, field, value):
    ranking, detail, reference = records
    detail[0][field] = value
    assert not all(verifier.compare_public_detail(ranking, detail, reference).values())


@pytest.mark.parametrize("mode", ["missing", "duplicate", "internal_column", "missing_column"])
def test_missing_duplicate_or_unscreened_detail_fails(verifier, records, mode):
    ranking, detail, reference = records
    if mode == "missing": detail.pop()
    if mode == "duplicate": detail.append(dict(detail[0]))
    if mode == "internal_column": detail[0]["internal_plan"] = "not public"
    if mode == "missing_column": del detail[0]["warehouse_name"]
    assert not all(verifier.compare_public_detail(ranking, detail, reference).values())


@pytest.mark.parametrize("field,value", [("sku_name", "Wrong"), ("quantity", "9007199254740993.26"), ("rank", "2"), ("outbound_count", "1")])
def test_ranking_fields_and_stable_order_are_compared(verifier, records, field, value):
    ranking, _, reference = records
    ranking[field] = value
    assert not all(verifier.compare_public_ranking([ranking], [reference[0]]).values())


@pytest.mark.parametrize("mode", ["valid", "duplicate_label", "wrong_value", "wrong_unit", "missing"])
def test_summary_values_are_not_trusted(verifier, mode):
    table = {"summary": [{"label": "入榜实发数量", "value": "10", "unit": "EA"}, {"label": "入榜出库单数", "value": "1", "unit": "单"}]}
    if mode == "duplicate_label": table["summary"].append(dict(table["summary"][0]))
    if mode == "wrong_value": table["summary"][1]["value"] = "2"
    if mode == "wrong_unit": table["summary"][0]["unit"] = "箱"
    if mode == "missing": table["summary"].pop()
    assert verifier.summary_matches(table, Decimal(10), 1, ranking=True) is (mode == "valid")


@pytest.mark.parametrize("mode", ["valid", "skipped", "changed_id", "changed_summary", "empty", "excessive", "wrong_total"])
def test_every_page_boundary_and_identity_is_checked(verifier, records, mode):
    _, rows, _ = records
    first = {"columns": [{"key": key} for key in verifier.DETAIL_KEYS], "rows": rows[:1], "summary": [],
             "pagination": {"page": 1, "page_count": 2, "total_rows": 2, "start_row": 1, "end_row": 1, "result_id": "rp_" + "A" * 32, "expires_at": 2000000000, "reconciled": True}}
    second = {**first, "rows": rows[1:], "pagination": {**first["pagination"], "page": 2, "start_row": 2, "end_row": 2}}
    if mode == "skipped": second["pagination"]["start_row"] = 3
    if mode == "changed_id": second["pagination"]["result_id"] = "rp_" + "B" * 32
    if mode == "changed_summary": second["summary"] = [{"label": "Unexpected"}]
    if mode == "empty": second["rows"] = []
    if mode == "excessive": first["pagination"]["page_count"] = 100001
    if mode == "wrong_total": first["pagination"]["total_rows"] = second["pagination"]["total_rows"] = 3
    def request(path):
        assert path.endswith("/pages/2")
        return {"business_table": second}
    if mode == "valid":
        assert verifier.complete_public_rows(first, verifier.DETAIL_KEYS, request) == rows
    else:
        with pytest.raises(ValueError): verifier.complete_public_rows(first, verifier.DETAIL_KEYS, request)
