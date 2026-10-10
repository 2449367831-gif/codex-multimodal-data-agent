"""An internally consistent public table can still be wrong about the source."""

from copy import deepcopy
from datetime import date, timedelta

import pytest
import sqlglot

from app.tests.test_customer_channel_aggregation import channels
from app.tests.test_sales_aggregation import facts
from app.tests.test_sales_analysis import run
from tests.acceptance.check_sales_composition_live import CASES, independent_reference_sql, verify_reference


@pytest.mark.parametrize("case", CASES[1:])
def test_independent_source_accepts_each_composed_fixture(case, channels):
    _, table = run(channels, case[1])
    end = date(2026, 9, 10)
    sql = independent_reference_sql(case, end - timedelta(days=case[3] or 1), end)
    reference = [dict(row) for row in channels.execute(sqlglot.transpile(sql, read="doris", write="sqlite")[0])]
    verify_reference(case, table["rows"], table, reference)


@pytest.mark.parametrize("corruption", ["scaled_quantities", "missing_group", "changed_sku_count", "wrong_owner", "wrong_unit", "incomplete_reference"])
def test_independent_source_rejects_self_consistent_wrong_results(channels, corruption):
    case = CASES[1]
    _, table = run(channels, case[1])
    sql = independent_reference_sql(case, date(2026, 9, 9), date(2026, 9, 10))
    reference = [dict(row) for row in channels.execute(sqlglot.transpile(sql, read="doris", write="sqlite")[0])]
    table = deepcopy(table)
    if corruption == "scaled_quantities":
        from decimal import Decimal
        for row in table["rows"]:
            row["quantity"] = str(Decimal(row["quantity"]) * 2)
        for item in table["summary"]:
            if item["label"] == "实际销售出库":
                item["value"] = str(Decimal(item["value"]) * 2)
    elif corruption == "missing_group":
        table["rows"].pop()
    elif corruption == "changed_sku_count":
        table["rows"][0]["sku_count"] = "99"
    elif corruption == "wrong_owner":
        table["rows"][0]["cargo_owner"] = "DIFFERENT"
    elif corruption == "wrong_unit":
        next(column for column in table["columns"] if column["key"] == "quantity")["unit"] = "箱"
    else:
        reference.pop()
    with pytest.raises(AssertionError):
        verify_reference(case, table["rows"], table, reference)
