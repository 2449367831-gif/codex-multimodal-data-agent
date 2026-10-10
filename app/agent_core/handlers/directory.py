"""Warehouse directory queries from the source contract, not metric aliases."""

from dataclasses import replace
from decimal import Decimal, localcontext
from pathlib import Path
import re

from sqlglot import exp
import yaml

from ...knowledge_activation import active_snapshot
from ...query_scope import QueryScope
from ..semantic_types import SemanticAnswer, SemanticRequest, SemanticRuntimeError, _display_table
from .base import MetricHandler
from .sales_aggregate import _integer


def warehouse_lookup_request(question: str) -> SemanticRequest | None:
    text = question.strip()
    if "仓库" not in text:
        return None
    # Only a literal directory search is executable here. Additional business
    # predicates remain intact for the model planner instead of being dropped.
    if re.search(r"货主|仅限|只看|停用|启用|有效|状态|排除|不含|云仓|截至|截止|历史", text):
        return None
    match = re.search(r"名称(?:中)?\s*(?:包含|含有|含)\s*(?:[“‘\"']([^”’\"']+)[”’\"']|(.+?)(?=的?(?:所有|全部)?仓库(?:并|且|$|[，,。\s])|的?(?:所有|全部)?仓库$))", text)
    # Paired curly quotes allow literal apostrophes inside a warehouse name.
    curly = re.search(r"名称(?:中)?\s*(?:包含|含有|含)\s*“([^”]+)”", text)
    if curly:
        keyword = curly.group(1).strip()
    elif match:
        keyword = (match.group(1) or match.group(2)).strip()
    elif re.search(r"(?:全部|所有)仓库", text) and not re.search(r"包含|含有|名称中|匹配", text):
        keyword = ""
    else:
        return None
    if len(keyword) > 100 or any(ord(c) < 32 for c in keyword):
        return None
    matched = curly or match
    remaining = text[:matched.start()] + text[matched.end():] if matched else text
    # This route implements a literal list with a fixed projection, not an
    # aggregation planner. Every remaining clause must describe that contract.
    supported = (
        "以表格形式", "用表格", "查一下", "查询", "帮我", "请", "查", "列出", "返回", "输出", "显示", "展示",
        "仓库编码", "仓库名称", "仓库类型", "仓库节点", "所有", "全部", "仓库", "表格", "中文", "的", "和", "及", "与", "并", "且",
    )
    remaining = re.sub("|".join(re.escape(word) for word in sorted(supported, key=len, reverse=True)), "", remaining)
    if re.sub(r"[\s，,。;；:：、\"'“”‘’]+", "", remaining):
        return None
    return SemanticRequest("warehouse_directory", None, None, lookup_keyword=keyword, show_warehouses=True)


def warehouse_type_count_request(question: str) -> SemanticRequest | None:
    """Recognize the reviewed name-filter plus type-count operation."""
    # Treat name literals as opaque data before interpreting aggregation words.
    name = re.search(
        r"名称(?:中)?\s*(?:包含|含有|含)\s*(?:“([^”]+)”|‘([^’]+)’|\"([^\"]+)\"|'([^']+)'|(.+?)(?=的?(?:所有|全部)?仓库[，,。\s]|的?(?:所有|全部)?仓库$))",
        question,
    )
    keyword = next((value.strip() for value in name.groups() if value is not None), None) if name else None
    if keyword is not None and (not keyword or len(keyword) > 100 or any(ord(c) < 32 for c in keyword)):
        return None
    masked = question[:name.start()] + '名称包含“DIRECTORYKEYWORD”' + question[name.end():] if name else question
    text = re.sub(r"\s+", "", masked)
    wants_share = bool(re.search(r"占比|比例", text))
    # Consume only whole supported clauses. Never remove the keyword from
    # arbitrary positions, which could erase an additional name constraint.
    text, count = re.subn(
        r"按仓库类型(?:统计|汇总)(?:仓库数量|仓库数)"
        r"(?:[和与及、+](?:数量)?(?:占比|比例))?",
        "返回仓库类型",
        text,
    )
    if count != 1:
        return None
    for clause in (
        "按货主和仓库编码联合去重", "按货主与仓库编码联合去重",
        "仓库编码去重", "同名不同编码分别计数", "用中文表格展示", "中文表格展示",
        "仓库类型未维护的单列", "类型未维护的单列", "排除逻辑删除",
        "先生成分类统计", "不展示对账明细",
        "再用独立查询核对各分类仓库数量合计与同范围去重总仓库数量一致",
        "全部核对通过后只展示仓库类型、仓库数量、数量占比三列业务表格",
    ):
        text = re.sub(r"(?:^|(?<=[，,。;；]))" + clause + r"(?=$|[，,。;；])", "", text)
    text = re.sub(r"(?:^|(?<=[，,。;；]))(?:计算|列出|展示|给出)(?:各类型)?(?:数量)?(?:占比|比例)(?=$|[，,。;；])", "", text)
    request = warehouse_lookup_request(text)
    if request is None:
        return None
    return replace(request, route="warehouse_type_summary" if wants_share else "warehouse_type_count",
                   lookup_keyword=keyword if name else request.lookup_keyword, show_warehouses=False)


class WarehouseDirectoryHandler(MetricHandler):
    def _contract(self):
        root, _ = active_snapshot(Path(__file__).resolve().parents[3])
        contract = yaml.safe_load((root / "sources/tables/master_wms.yaml").read_text(encoding="utf-8"))
        if not contract.get("lookup") or contract.get("aggregate_key") != ["cargo_owner_id", "wms_code"]:
            raise SemanticRuntimeError("仓库资料查询合同尚未就绪", reason="directory_contract_missing")
        return contract

    @staticmethod
    def _sql(keyword: str) -> str:
        literal = exp.Literal.string(keyword).sql(dialect="doris")
        return f"""WITH mappings AS (
  SELECT TRIM(CAST(cargo_owner_id AS STRING)) AS cargo_owner,
         TRIM(wms_code) AS warehouse_code, TRIM(wms_desc) AS warehouse_name,
         NULLIF(TRIM(CAST(wms_type AS STRING)), '') AS warehouse_type,
         NULLIF(TRIM(CAST(wms_node AS STRING)), '') AS warehouse_node,
         MAX(updated_date) AS source_updated_at
  FROM ywzt_ods.master_wms
  WHERE COALESCE(TRIM(CAST(status AS STRING)), '0') = '0'
  GROUP BY TRIM(CAST(cargo_owner_id AS STRING)), TRIM(wms_code), TRIM(wms_desc),
           NULLIF(TRIM(CAST(wms_type AS STRING)), ''),
           NULLIF(TRIM(CAST(wms_node AS STRING)), '')
), controlled AS (
  SELECT *, COUNT(*) OVER (PARTITION BY cargo_owner, warehouse_code) AS mapping_variants
  FROM mappings
), matched AS (
  SELECT * FROM controlled WHERE LOCATE({literal}, warehouse_name) > 0
)
SELECT cargo_owner, warehouse_code, warehouse_name, warehouse_type, warehouse_node,
       mapping_variants, source_updated_at, COUNT(*) OVER () AS matched_count
FROM matched ORDER BY cargo_owner, warehouse_code LIMIT 101"""

    @staticmethod
    def _label(value, labels):
        code = "" if value is None else str(value).strip()
        if not code:
            return "未配置"
        name = labels.get(code)
        return f"{name}（{code}）" if name else f"未映射（{code}）"

    def run(self, request: SemanticRequest) -> SemanticAnswer:
        contract = self._contract()
        rows = self._execute(self._sql(request.lookup_keyword or ""), purpose="warehouse directory literal name lookup")
        if len(rows) > 100:
            raise SemanticRuntimeError("仓库匹配结果超过展示上限，请缩小名称范围", reason="result_truncated")
        seen = set()
        displayed = []
        latest = []
        for row in rows:
            key = tuple(str(row.get(k) or "").strip() for k in ("cargo_owner", "warehouse_code"))
            name = str(row.get("warehouse_name") or "").strip()
            if (not all(key) or not name or key in seen or str(row.get("mapping_variants")) != "1"):
                raise SemanticRuntimeError("仓库资料存在重复或冲突，暂未展示结果", reason="directory_mapping_conflict")
            if str(row.get("matched_count")) != str(len(rows)):
                raise SemanticRuntimeError("仓库匹配结果不完整，暂未展示结果", reason="result_truncated")
            seen.add(key)
            displayed.append({
                "cargo_owner": key[0], "warehouse_code": key[1], "warehouse_name": name,
                "warehouse_type": self._label(row.get("warehouse_type"), contract["lookup"]["type_labels"]),
                "warehouse_node": self._label(row.get("warehouse_node"), contract["lookup"]["node_labels"]),
            })
            if row.get("source_updated_at"):
                latest.append(str(row["source_updated_at"]))
        match_label = f"名称包含“{request.lookup_keyword}”" if request.lookup_keyword else "全部仓库"
        text = f"查询成功，{match_label}：找到 {len(displayed)} 条仓库资料。范围为全部货主和仓库类型，按货主与仓库编码区分。"
        if not displayed:
            text = f"查询成功，{match_label}：当前没有匹配的仓库资料。"
        elif latest:
            text += f"资料最近变更时间：{max(latest)}（不代表同步时间）。未映射的类型或节点保留原编码。"
        else:
            text += "资料变更时间缺失，尚不能确认资料鲜度。"
        return SemanticAnswer(text, _display_table(
            table_id="warehouse_directory", title="仓库资料", caption=match_label,
            columns=[], rows=displayed,
            summary=[{"label": "仓库数", "value": str(len(displayed)), "unit": "个"}],
            empty_label="查询成功，当前名称范围没有匹配的仓库资料",
        ))


class WarehouseTypeCountHandler(WarehouseDirectoryHandler):
    @staticmethod
    def _sql(keyword: str) -> str:
        literal = exp.Literal.string(keyword).sql(dialect="doris")
        return f"""WITH mappings AS (
  SELECT DISTINCT TRIM(CAST(cargo_owner_id AS STRING)) AS cargo_owner,
         TRIM(wms_code) AS warehouse_code, TRIM(wms_desc) AS warehouse_name,
         NULLIF(TRIM(CAST(wms_type AS STRING)), '') AS warehouse_type
  FROM ywzt_ods.master_wms
  WHERE COALESCE(TRIM(CAST(status AS STRING)), '0') = '0'
), matched_codes AS (
  SELECT DISTINCT cargo_owner, warehouse_code FROM mappings WHERE LOCATE({literal}, warehouse_name) > 0
), code_types AS (
  SELECT m.cargo_owner, m.warehouse_code, MIN(m.warehouse_type) AS warehouse_type,
    COUNT(DISTINCT m.warehouse_type) + MAX(CASE WHEN m.warehouse_type IS NULL THEN 1 ELSE 0 END) AS type_variants,
    COUNT(DISTINCT m.warehouse_name) + MAX(CASE WHEN m.warehouse_name IS NULL THEN 1 ELSE 0 END) AS name_variants
  FROM mappings m JOIN matched_codes c
    ON m.cargo_owner <=> c.cargo_owner AND m.warehouse_code <=> c.warehouse_code
  GROUP BY m.cargo_owner, m.warehouse_code
), grouped AS (
  SELECT warehouse_type, COUNT(*) AS warehouse_count FROM code_types GROUP BY warehouse_type
), controls AS (
  SELECT COUNT(*) AS matched_warehouse_count,
    COALESCE(SUM(CASE WHEN type_variants <> 1 OR name_variants <> 1 THEN 1 ELSE 0 END), 0) AS conflicting_code_count,
    COALESCE(SUM(CASE WHEN COALESCE(cargo_owner, '') = '' OR COALESCE(warehouse_code, '') = '' THEN 1 ELSE 0 END), 0) AS blank_code_count
  FROM code_types
)
SELECT warehouse_type, warehouse_count, matched_warehouse_count, conflicting_code_count, blank_code_count,
       (SELECT COUNT(*) FROM grouped) AS group_count
FROM grouped CROSS JOIN controls
ORDER BY warehouse_type LIMIT 101"""

    def run(self, request: SemanticRequest) -> SemanticAnswer:
        contract = self._contract()
        rows = self._execute(self._sql(request.lookup_keyword or ""), purpose="warehouse type count by literal name lookup")
        if len(rows) > 100:
            raise SemanticRuntimeError("仓库类型统计结果超过展示上限", reason="result_truncated")
        displayed, seen = [], set()
        total, expected = 0, None
        try:
            for row in rows:
                count = _integer(row.get("warehouse_count"))
                matched = _integer(row.get("matched_warehouse_count"))
                key = row.get("warehouse_type")
                if (count < 1 or key in seen or _integer(row.get("group_count")) != len(rows)
                        or _integer(row.get("conflicting_code_count"))
                        or _integer(row.get("blank_code_count"))
                        or (expected is not None and matched != expected)):
                    raise ValueError("invalid warehouse controls")
                seen.add(key)
                expected = matched
                total += count
                displayed.append({"warehouse_type": self._label(key, contract["lookup"]["type_labels"]),
                                  "warehouse_count": str(count)})
            if total != (expected or 0):
                raise ValueError("warehouse count mismatch")
        except (TypeError, ValueError) as exc:
            raise SemanticRuntimeError("仓库资料存在冲突或统计不完整，暂未展示", reason="directory_reconciliation_failed") from exc
        return SemanticAnswer(
            f"查询成功，仓库共 {total} 个，按货主与仓库编码去重；同名不同编码分别计数。",
            _display_table(table_id="warehouse_type_count", title="仓库类型统计", caption=f"名称包含“{request.lookup_keyword}”",
                           columns=[], rows=displayed, summary=[{"label":"仓库数","value":str(total),"unit":"个"}]),
        )


class WarehouseTypeShareHandler(WarehouseTypeCountHandler):
    """Add a ratio only after independent classification counts agree."""

    def run(self, request: SemanticRequest) -> SemanticAnswer:
        answer = super().run(request)
        table = answer.business_table
        controls = self._execute(
            WarehouseTypeSummaryDetailHandler._control_sql(request.lookup_keyword or ""),
            purpose="independent warehouse type count reconciliation",
        )
        labels = self._contract()["lookup"]["type_labels"]
        expected = {}
        try:
            for row in controls:
                label = self._label(row.get("warehouse_type"), labels)
                count = _integer(row.get("warehouse_count"))
                if label in expected or count < 1:
                    raise ValueError("invalid independent warehouse group")
                expected[label] = count
            if expected != {row["warehouse_type"]: int(row["warehouse_count"]) for row in table["rows"]}:
                raise ValueError("independent warehouse count mismatch")
        except (ValueError, TypeError, KeyError) as exc:
            raise SemanticRuntimeError("仓库类型独立对账未通过，暂未展示", reason="directory_reconciliation_failed") from exc
        total = sum(expected.values())
        with localcontext() as context:
            context.prec = 64
            for row in table["rows"]:
                row["warehouse_share"] = format(Decimal(row["warehouse_count"]) * 100 / Decimal(total), ".2f")
        table["table_id"] = "warehouse_type_summary"
        table["title"] = "仓库类型汇总"
        table["caption"] = f"名称包含“{request.lookup_keyword}”" if request.lookup_keyword else "全部仓库"
        return SemanticAnswer(str(answer) + "各类型数量已独立核对；占比以同范围去重仓库总数为分母，未维护类型单列。资料同步鲜度尚不能确认。", table)

def warehouse_type_summary_detail_request(question: str) -> SemanticRequest | None:
    """Recognize a two-grain warehouse report before the model fallback."""

    text = re.sub(r"\s+", "", question)
    if not ("两张" in text or "两表" in text) or "仓库清单" not in text:
        return None
    if not ("按仓库类型" in text or "仓库类型汇总" in text) or not re.search(r"占比|比例", text):
        return None
    match = re.search(
        r"名称(?:中)?\s*(?:包含|含有|含)\s*(?:[“‘\"']([^”’\"']+)[”’\"']|(.+?)(?=的?(?:所有|全部)?仓库[，,。\s]|的?(?:所有|全部)?仓库$))",
        text,
    )
    if match:
        keyword = (match.group(1) or match.group(2)).strip()
    elif re.search(r"(?:全部|所有)仓库", text) and not re.search(r"包含|含有|名称中|匹配", text):
        keyword = ""
    else:
        return None
    if len(keyword) > 100 or any(ord(char) < 32 for char in keyword):
        return None

    # This route has a fixed directory grain.  Extract only explicit physical
    # scope predicates; anything left over must be rejected rather than being
    # silently widened to ALL.
    owner = "ALL"
    warehouse_codes: list[str] = []

    owner_patterns = (
        r"(?:仅限|只看|只查)?货主(?:编码)?(?:为|是|[:：])?`?([A-Za-z0-9_-]{1,40})`?",
        r"(?:仅限|只看|只查)?(HJXH)货主",
    )
    owner_matches: list[tuple[int, int, str]] = []
    for pattern in owner_patterns:
        for found in re.finditer(pattern, text, flags=re.IGNORECASE):
            value = found.group(1).strip()
            owner_matches.append((found.start(), found.end(), value.upper() if value.upper() == "HJXH" else value))
    if len(owner_matches) > 1 or (owner_matches and "或" in text[owner_matches[0][1]:]):
        return None
    if owner_matches:
        start, end, owner = owner_matches[0]
        text = text[:start] + text[end:]

    warehouse_matches = list(re.finditer(
        r"(?:仅限|只看|只查)?仓库编码(?:为|是|[:：])?`?([A-Za-z0-9_-]{1,40})`?", text,
        flags=re.IGNORECASE,
    ))
    if len(warehouse_matches) > 1:
        return None
    if warehouse_matches:
        found = warehouse_matches[0]
        warehouse_codes.append(found.group(1).strip())
        text = text[:found.start()] + text[found.end():]

    # Remove the matched name expression and the fixed, reviewed report
    # wording. Do not add broad words such as "仅限" or "排除" here: leftover
    # predicates must keep the route from becoming an unintended ALL query.
    name_match = re.search(
        r"名称(?:中)?\s*(?:包含|含有|含)\s*(?:[“‘\"']([^”’\"']+)[”’\"']|(.+?)(?=的?(?:所有|全部)?仓库[，,。\s]|的?(?:所有|全部)?仓库$))",
        text,
    )
    if name_match:
        text = text[:name_match.start()] + text[name_match.end():]
    supported = (
        "的所有仓库", "的全部仓库", "所有仓库", "全部仓库", "仓库",
        "排除逻辑删除", "按货主和仓库编码联合去重", "同名不同编码分别计数",
        "类型未维护的单列", "需要两张不同粒度的表", "不同粒度", "粒度", "第一张标题为仓库类型汇总",
        "列出仓库类型仓库数量数量占比（%）", "第二张标题为仓库清单",
        "完整列出货主编码仓库编码仓库名称仓库类型", "不使用TOP或LIMIT截断清单",
        "再独立核对两张表各类型数量及总数一致", "全部通过后同时展示两张表",
        "不展示对账明细", "按仓库类型汇总", "按仓库类型统计",
        "货主编码", "仓库编码", "仓库名称", "仓库类型", "数量占比（%）",
        "类型汇总", "类型", "数量", "占比", "联合去重", "计数", "分别",
        "查询", "列出", "完整", "标题为", "第一张", "第二张", "表格", "表",
        "独立核对", "各类型数量", "总数一致", "同时展示", "不展示",
        "对账明细", "需要", "的", "和", "、", "，", "。", ",", "；", ";",
    )
    for word in sorted(supported, key=len, reverse=True):
        text = text.replace(word, "")
    if re.sub(r"[\s`'\"“”‘’:：()（）%]+", "", text):
        return None
    return SemanticRequest(
        "warehouse_type_summary_detail",
        None,
        None,
        lookup_keyword=keyword,
        show_warehouses=True,
        scope=QueryScope(
            owner=owner, warehouse_codes=tuple(warehouse_codes),
        ),
    )


class WarehouseTypeSummaryDetailHandler(WarehouseDirectoryHandler):
    """Build and independently reconcile a warehouse type summary and list."""

    @staticmethod
    def _keyword_sql(keyword: str) -> str:
        return exp.Literal.string(keyword).sql(dialect="doris")

    def _detail_sql(self, keyword: str) -> str:
        literal = self._keyword_sql(keyword)
        return f"""WITH mappings AS (
  SELECT DISTINCT
    TRIM(CAST(cargo_owner_id AS STRING)) AS cargo_owner,
    TRIM(wms_code) AS warehouse_code,
    TRIM(wms_desc) AS warehouse_name,
    NULLIF(TRIM(CAST(wms_type AS STRING)), '') AS warehouse_type,
    NULLIF(TRIM(CAST(wms_node AS STRING)), '') AS warehouse_node
  FROM ywzt_ods.master_wms
  WHERE COALESCE(TRIM(CAST(status AS STRING)), '0') = '0'
), controlled AS (
  SELECT *, COUNT(*) OVER (PARTITION BY cargo_owner, warehouse_code) AS mapping_variants
  FROM mappings
), matched AS (
  SELECT * FROM controlled WHERE LOCATE({literal}, warehouse_name) > 0
)
SELECT
  cargo_owner,
  warehouse_code, warehouse_name, warehouse_type, warehouse_node, mapping_variants
FROM matched
ORDER BY cargo_owner, warehouse_code"""

    @staticmethod
    def _control_sql(keyword: str) -> str:
        literal = exp.Literal.string(keyword).sql(dialect="doris")
        return f"""WITH mappings AS (
  SELECT DISTINCT TRIM(CAST(cargo_owner_id AS STRING)) AS cargo_owner,
         TRIM(wms_code) AS warehouse_code, TRIM(wms_desc) AS warehouse_name,
         NULLIF(TRIM(CAST(wms_type AS STRING)), '') AS warehouse_type,
         NULLIF(TRIM(CAST(wms_node AS STRING)), '') AS warehouse_node
  FROM ywzt_ods.master_wms
  WHERE COALESCE(TRIM(CAST(status AS STRING)), '0') = '0'
), matched_codes AS (
  SELECT DISTINCT cargo_owner, warehouse_code
  FROM mappings WHERE LOCATE({literal}, warehouse_name) > 0
), keyed AS (
  SELECT m.cargo_owner, m.warehouse_code,
         COUNT(*) AS mapping_variants,
         COUNT(DISTINCT m.warehouse_type) + MAX(CASE WHEN m.warehouse_type IS NULL THEN 1 ELSE 0 END) AS type_variants,
         COUNT(DISTINCT m.warehouse_node) + MAX(CASE WHEN m.warehouse_node IS NULL THEN 1 ELSE 0 END) AS node_variants,
         COUNT(DISTINCT m.warehouse_name) + MAX(CASE WHEN m.warehouse_name IS NULL THEN 1 ELSE 0 END) AS name_variants,
         MIN(m.warehouse_type) AS warehouse_type
  FROM mappings m JOIN matched_codes c
    ON m.cargo_owner <=> c.cargo_owner AND m.warehouse_code <=> c.warehouse_code
  GROUP BY m.cargo_owner, m.warehouse_code
), valid AS (
  SELECT * FROM keyed WHERE mapping_variants = 1 AND type_variants = 1
    AND node_variants = 1 AND name_variants = 1
)
SELECT warehouse_type, COUNT(*) AS warehouse_count
FROM valid
GROUP BY warehouse_type
ORDER BY warehouse_type"""

    def run(self, request: SemanticRequest) -> SemanticAnswer:
        contract = self._contract()
        if (
            request.scope.sku_codes or request.scope.product_terms
            or request.scope.finished_goods or request.scope.channel != "ALL"
            or request.scope.unit != "EA" or request.start is not None or request.end is not None
        ):
            raise SemanticRuntimeError("仓库目录报表不支持商品、渠道或日期范围条件，暂未执行", reason="directory_scope_invalid")
        detail_rows, control_rows = self.execution.query_many(
            [
                self._detail_sql(request.lookup_keyword or ""),
                self._control_sql(request.lookup_keyword or ""),
            ],
            purpose="warehouse type summary and independent reconciliation",
            timeout_seconds=180,
        )

        detail_counts: dict[str, int] = {}
        seen: set[tuple[str, str]] = set()
        displayed: list[dict[str, str]] = []
        for row in detail_rows:
            owner = str(row.get("cargo_owner") or "").strip()
            code = str(row.get("warehouse_code") or "").strip()
            name = str(row.get("warehouse_name") or "").strip()
            raw_type = row.get("warehouse_type")
            type_key = "" if raw_type is None or not str(raw_type).strip() else str(raw_type).strip()
            identity = (owner, code)
            if not owner or not code or not name or identity in seen:
                raise SemanticRuntimeError("仓库清单存在重复或缺失，暂未展示", reason="directory_reconciliation_failed")
            seen.add(identity)
            detail_counts[type_key] = detail_counts.get(type_key, 0) + 1
            displayed.append({
                "cargo_owner": owner,
                "warehouse_code": code,
                "warehouse_name": name,
                "warehouse_type": self._label(raw_type, contract["lookup"]["type_labels"]),
            })

        control_counts: dict[str, int] = {}
        for row in control_rows:
            key = "" if row.get("warehouse_type") is None else str(row.get("warehouse_type")).strip()
            if key in control_counts:
                raise SemanticRuntimeError("仓库类型对账结果重复，暂未展示", reason="directory_reconciliation_failed")
            count = _integer(row.get("warehouse_count"))
            if count < 0:
                raise SemanticRuntimeError("仓库类型对账结果无效，暂未展示", reason="directory_reconciliation_failed")
            control_counts[key] = count
        if detail_counts != control_counts:
            raise SemanticRuntimeError("仓库汇总与独立清单对账不一致，暂未展示", reason="directory_reconciliation_failed")

        total = sum(control_counts.values())
        if total != len(displayed):
            raise SemanticRuntimeError("仓库总数对账不一致，暂未展示", reason="directory_reconciliation_failed")
        summary_rows = [
            {
                "warehouse_type": self._label(key or None, contract["lookup"]["type_labels"]),
                "warehouse_count": str(count),
                "warehouse_share": f"{count / total * 100:.2f}" if total else "0.00",
            }
            for key, count in sorted(control_counts.items(), key=lambda item: item[0])
        ]
        caption = f"名称包含“{request.lookup_keyword or ''}”"
        summary_table = _display_table(
            table_id="warehouse_type_summary",
            title="仓库类型汇总",
            caption=caption,
            columns=[
                {"key": "warehouse_type", "label": "仓库类型"},
                {"key": "warehouse_count", "label": "仓库数量"},
                {"key": "warehouse_share", "label": "数量占比（%）"},
            ],
            rows=summary_rows,
            summary=[{"label": "仓库数", "value": str(total), "unit": "个"}],
        )
        detail_table = _display_table(
            table_id="warehouse_directory_detail",
            title="仓库清单",
            caption=caption,
            columns=[
                {"key": "cargo_owner", "label": "货主编码"},
                {"key": "warehouse_code", "label": "仓库编码"},
                {"key": "warehouse_name", "label": "仓库名称"},
                {"key": "warehouse_type", "label": "仓库类型"},
            ],
            rows=displayed,
            summary=[{"label": "仓库数", "value": str(total), "unit": "个"}],
        )
        return SemanticAnswer(
            f"查询成功，已完成仓库类型汇总、完整清单和独立对账，共 {total} 个仓库。",
            summary_table,
            business_tables=[summary_table, detail_table],
        )
