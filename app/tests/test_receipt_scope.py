"""Receipt scopes must reach controls, page queries and result validation."""

from dataclasses import replace

import pytest

from app.tests.test_inbound_details import PLAN, receipts, execute
from app.agent_core.inbound_details import compile_inbound_details, reconcile_inbound_details
from app.agent_core.receipt_pagination import compile_receipt_page, retrieve_receipt_details
from app.agent_core.semantic_types import SemanticRuntimeError


def test_receipt_plan_carries_scope_to_sql():
    scoped = replace(PLAN, owner="B", warehouse_codes=("W2",), sku_codes=("S2",))
    import sqlglot
    from sqlglot import exp
    for sql in (compile_inbound_details(scoped), compile_receipt_page(scoped, offset=0)):
        tree = sqlglot.parse_one(sql, read="doris")
        scoped_select = next(node for node in tree.find_all(exp.Select) if any(col.alias_or_name == "matched_count" for col in node.expressions))
        assert scoped_select.args.get("where") is not None
        assert {col.name for col in scoped_select.args["where"].find_all(exp.Column)} == {"cargo_owner", "warehouse_code", "sku_code"}
        assert {lit.this for lit in scoped_select.args["where"].find_all(exp.Literal)} == {"B", "W2", "S2"}


@pytest.mark.parametrize("field,value", [("owner", "DIFFERENT"), ("warehouse_codes", ("DIFFERENT",)), ("sku_codes", ("DIFFERENT",))])
def test_receipt_reconciler_rejects_out_of_scope_rows(receipts, field, value):
    with pytest.raises(SemanticRuntimeError):
        reconcile_inbound_details(execute(receipts), replace(PLAN, **{field: value}))


def test_scoped_pages_reconcile_only_requested_owner_and_rebuild_all(receipts):
    import sqlglot
    def query(sql, **_):
        return [dict(row) for row in receipts.execute(sqlglot.transpile(sql, read="doris", write="sqlite")[0])]
    scoped = replace(PLAN, owner="B", warehouse_codes=("W1",), sku_codes=("S1",))
    result = retrieve_receipt_details(scoped, query=query, page_size=1)
    assert result.row_count == 1 and {r["cargo_owner"] for r in result.rows} == {"B"}
    assert retrieve_receipt_details(PLAN, query=query, page_size=2).row_count == 4
