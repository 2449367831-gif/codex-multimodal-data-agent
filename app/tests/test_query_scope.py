from datetime import date, datetime
import unittest
from zoneinfo import ZoneInfo

import sqlglot
from sqlglot import exp

from app.query_scope import QueryScope, ScopeResolver, apply_scope
from app.semantic_runtime import SemanticQueryRuntime


class QueryScopeTests(unittest.TestCase):
    def setUp(self):
        self.runtime = SemanticQueryRuntime(backend_url="http://127.0.0.1:9", clock=lambda: datetime(2026, 9, 7, 12, tzinfo=ZoneInfo("Asia/Shanghai")))

    def test_named_warehouse_and_product_are_preserved(self):
        for question, code in [
            ("昨天天津仓销售出库top20的sku", "TJWWC-777"),
            ("上月成都仓库存周转", "WMS000005"),
            ("当前成都仓成品库存", "WMS000005"),
        ]:
            with self.subTest(question=question):
                request = self.runtime.classify(question)
                self.assertIsNotNone(request)
                self.assertEqual((code,), request.scope.warehouse_codes)
        request = self.runtime.classify("昨天烤肠销售出库top20的sku")
        self.assertIn("烤肠", request.scope.product_terms)

    def test_dates_do_not_expand_or_default(self):
        for question, start, end in [
            ("查2026年8月3日库存周转", date(2026, 8, 3), date(2026, 8, 4)),
            ("2026年1-8月入库数量", date(2026, 1, 1), date(2026, 9, 1)),
            ("8月3日至8月5日实际销售出库", date(2026, 8, 3), date(2026, 8, 6)),
            ("上周实际销售出库", date(2026, 8, 31), date(2026, 9, 7)),
        ]:
            with self.subTest(question=question):
                request = self.runtime.classify(question)
                self.assertIsNotNone(request)
                self.assertEqual((start, end), (request.start, request.end))

    def test_new_question_has_all_scope_not_previous_scope(self):
        self.runtime.classify("皇家小虎昨天天津仓烤肠销售出库top20的sku")
        request = self.runtime.classify("昨天销售出库top20的sku")
        self.assertEqual("ALL", request.scope.owner)
        self.assertEqual((), request.scope.warehouse_codes)
        self.assertEqual((), request.scope.product_terms)

    def test_unrecognized_conditions_do_not_reach_fixed_queries(self):
        for question in ["昨天武汉仓销售出库top20的sku", "昨天鸡翅销售出库top20的sku", "昨天抖音渠道销售出库top20的sku", "看下dms的1-8月湖南积分兑换空气炸锅有多少", "2026年13月入库数量"]:
            with self.subTest(question=question):
                self.assertIsNone(self.runtime.classify(question))

    def test_ast_scopes_facts_without_interpolating_values(self):
        sql = "SELECT SUM(d.ship_qty) FROM ywzt_ods.scm_sales_shipment_detail d WHERE TRIM(d.company_code) = 'HJXH'"
        compiled = apply_scope(sql, QueryScope(sku_codes=("A' OR 1=1 --",)), legacy_owner="HJXH")
        tree = sqlglot.parse_one(compiled, read="doris")
        self.assertEqual(1, len(list(tree.find_all(exp.In))))
        self.assertIn("A' OR 1=1 --", [n.this for n in tree.find_all(exp.Literal)])
        self.assertNotIn("HJXH", [n.this for n in tree.find_all(exp.Literal)])

    def test_sku_case_is_not_changed(self):
        scope, _, _ = ScopeResolver().resolve("昨天SKU AbC123实际销售出库")
        self.assertEqual(("AbC123",), scope.sku_codes)

    def test_warehouse_suffix_is_consumed_with_the_alias_not_partially(self):
        for name in ("长沙黄兴", "长沙黄兴仓", "长沙黄兴仓库", "黄兴仓库"):
            with self.subTest(name=name):
                scope, shape, unsupported = ScopeResolver().resolve(f"查询{name}当前烤肠库存")
                self.assertFalse(unsupported)
                self.assertEqual(("10114790",), scope.warehouse_codes)
                self.assertNotIn("库当前", shape)
                self.assertIsNotNone(self.runtime.classify(f"查询{name}当前烤肠库存"))
        for extra in ("武汉仓", "库房二", "排除客户甲"):
            with self.subTest(extra=extra):
                self.assertIsNone(self.runtime.classify(f"查询长沙黄兴仓库{extra}当前烤肠库存"))

    def test_presentation_grammar_does_not_become_an_unsupported_scope(self):
        for question in (
            "近14天各仓库周转天数",
            "昨天每个仓库的实际出库分别是多少",
            "查9月1号至9月14号每个仓每个SKU的实际出货",
        ):
            with self.subTest(question=question):
                _scope, _shape, unsupported = ScopeResolver().resolve(question)
                self.assertFalse(unsupported)

    def test_sales_order_uses_only_declared_fields(self):
        sql = self.runtime._sales_sql(date(2026, 9, 1), date(2026, 9, 2))
        tree = sqlglot.parse_one(sql, read="doris")
        fields = {c.name for c in tree.find_all(exp.Column) if c.table == "so"}
        self.assertLessEqual(fields, {"sales_order_code", "company_code", "deleted_flag", "_fdl_marked_deleted"})


if __name__ == "__main__":
    unittest.main()
