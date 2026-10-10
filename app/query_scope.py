"""Request-local scope resolution and AST application for fixed query routes."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import re
from typing import Any

import sqlglot
from sqlglot import exp
import yaml
from .knowledge_activation import active_snapshot
from .sku_references import extract_sku_references
from .request_syntax import normalize_recent_day_windows


ROOT = Path(__file__).resolve().parents[1]

# Do not ignore operators or measures here: only the planner that implements
# an operation may consume it. Business scope values remain release-owned.
_GENERIC_SCOPE_GRAMMAR = (
    "天数", "分别", "出货", "出货量",
)


def has_unconsumed_scope_operators(question: str) -> bool:
    _, text = extract_sku_references(question)
    text = re.sub(r"\s+", "", text)
    return bool(re.search(r"[<>≤≥≠=*×÷^]|(?<!\d)[+-](?=\d)|(?<!\d)/(?!/)", text))


@dataclass(frozen=True)
class QueryScope:
    owner: str = "ALL"
    warehouse_codes: tuple[str, ...] = ()
    warehouse_labels: tuple[str, ...] = ()
    sku_codes: tuple[str, ...] = ()
    product_terms: tuple[str, ...] = ()
    finished_goods: bool = False
    channel: str = "ALL"
    unit: str = "EA"
    finished_goods_exceptions: tuple[str, ...] = ()

    def __post_init__(self):
        if self.finished_goods_exceptions and not self.finished_goods:
            raise ValueError("Finished-goods exceptions require a finished-goods scope")
        if len(set(self.finished_goods_exceptions)) != len(self.finished_goods_exceptions) or any(
            not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", code)
            for code in self.finished_goods_exceptions
        ):
            raise ValueError("Invalid finished-goods exception identifiers")

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def label(self) -> str:
        parts = [self.owner if self.owner != "ALL" else "全部货主"]
        parts.append("、".join(self.warehouse_labels) or "全部仓库")
        parts.append("、".join(self.sku_codes or self.product_terms) or ("成品（含指定例外）" if self.finished_goods_exceptions else "成品" if self.finished_goods else "全部商品"))
        return " · ".join(parts)


class ScopeResolver:
    def __init__(self, root: Path = ROOT) -> None:
        self.root = root

    def resolve(self, question: str) -> tuple[QueryScope, str, bool]:
        sku_codes, scope_question = extract_sku_references(question)
        scope_question = normalize_recent_day_windows(scope_question)
        text = re.sub(r"\s+", "", scope_question).lower()
        declarations = text
        semantic_root, _ = active_snapshot(self.root)
        profile = yaml.safe_load((semantic_root / "profiles/hjxh_operating_centers.yaml").read_text(encoding="utf-8"))
        aliases = yaml.safe_load((semantic_root / "rules/query-scope.yaml").read_text(encoding="utf-8"))
        aliases["warehouse_aliases"] = {
            str(code): list(names or [])
            for code, names in (aliases.get("warehouse_aliases") or {}).items()
        }
        warehouses = {str(row["code"]): row["name"] for row in profile["scope"]["centers"]}
        # The approved v3 report profile contains the current nine-center
        # assignment, while the legacy operating-center profile may still
        # describe eight centers. Merge the newer center names into this
        # request-local resolver without changing historical master data.
        turnover_profile_path = semantic_root / "profiles/hjxh_turnover_template_v3.yaml"
        if turnover_profile_path.is_file():
            turnover_profile = yaml.safe_load(turnover_profile_path.read_text(encoding="utf-8"))
            for center in turnover_profile.get("centers", []):
                rows = [center, *(center.get("details") or [])]
                for row in rows:
                    code = str(row.get("code") or "").strip()
                    name = str(row.get("name") or "").strip()
                    if not code or not name:
                        continue
                    warehouses[code] = name
                    aliases["warehouse_aliases"].setdefault(code, [])
                    if name not in aliases["warehouse_aliases"][code]:
                        aliases["warehouse_aliases"][code].append(name)
        codes, labels = [], []
        for code, names in aliases["warehouse_aliases"].items():
            code = str(code)
            candidates = sorted([code, warehouses.get(code, code), *names], key=len, reverse=True)
            # Match against the same text before masking overlapping aliases.
            # Otherwise "长沙黄兴" consumes part of "黄兴仓", leaving a
            # spurious unknown warehouse, or deletion joins unrelated words.
            matches = [match for name in candidates for match in re.finditer(
                re.escape(name.lower()) + (r"(?:库(?!存))?" if name.endswith("仓") else r"(?:仓库|仓)?"),
                text,
            )]
            if matches:
                for match in matches:
                    text = text[:match.start()] + " " * (match.end() - match.start()) + text[match.end():]
                codes.append(code)
                labels.append(str(names[0]))
        text = text.replace(" ", "")
        owner = "ALL"
        for alias in ("皇家小虎", "hjxh"):
            if alias in text:
                owner = "HJXH"
                text = re.sub(r"(?:货主)?" + alias + r"(?:货主)?", "", text)
        terms: tuple[str, ...] = ()
        for family in aliases["product_families"]:
            if any(token in text for token in family["aliases"]):
                terms = tuple(family["name_contains"])
                for token in family["aliases"]:
                    text = text.replace(token, "")
                break
        finished = "成品" in text or "周转" in text or bool(terms)
        text = re.sub(r"(?:所有|全部)货主", "", text)
        filter_text = re.sub(r"按货主|各货主|货主维度", "", text)
        unsupported = bool(re.search(r"渠道|平台|客户|供应商|店铺|货主|公司|金额|成本|利润|含税|不含税|排除|剔除|除了|不要|仅限", filter_text))
        # Remaining operators are request semantics, not punctuation. A route
        # must consume a supported operation before scope validation; otherwise
        # retain the complete request for general planning. Date separators and
        # opaque SKU identifiers are not arithmetic operators here.
        if has_unconsumed_scope_operators(filter_text):
            unsupported = True
        # A remaining named warehouse must never be treated as an ALL request.
        remainder = text
        for generic in ("各仓库", "各仓", "所有仓库", "全部仓库", "按仓库", "按仓", "每个仓库", "每个仓", "每仓", "分仓", "仓库维度", "入仓", "仓库数", "仓库", "中心仓"):
            remainder = remainder.replace(generic, "")
        if "仓" in remainder:
            unsupported = True
        # Explicit codes and product constraints outside the registered aliases
        # belong to the model planner, not a fixed route that drops them.
        residue = remainder
        known_words = sorted(
            set(aliases["fixed_route_words"]) | set(_GENERIC_SCOPE_GRAMMAR),
            key=len,
            reverse=True,
        )
        # Match against the original text in one pass. Sequential deletion can
        # create new adjacent words and then consume part of an unrelated word.
        residue = re.sub("|".join(re.escape(token.lower()) for token in known_words), "", residue)
        residue = re.sub(r"[\d\W_]+", "", residue)
        if residue:
            unsupported = True
        # One fast-path scope cannot represent both an ALL population and a
        # subset. Preserve that composition for general planning or clarification.
        if ((owner != "ALL" and re.search(r"(?:所有|全部)货主", declarations))
                or (codes and re.search(r"(?:所有|全部)仓库", declarations))
                or (sku_codes and re.search(r"(?:所有|全部)(?:sku|商品|货品)", declarations))):
            unsupported = True
        scope = QueryScope(owner, tuple(codes), tuple(labels), sku_codes, terms, finished)
        return scope, text, unsupported


# Fields used by the current declared source contracts; the compiler preserves
# all existing predicates and only narrows physical facts before aggregation.
SOURCE_DIMENSIONS = {
    "master_wms": ("cargo_owner_id", "wms_code", None),
    "master_sku": (None, None, "sku_code"),
    "scm_sales_shipment_header": ("company_code", "facility_code", None),
    "scm_sales_order_header": ("company_code", None, None),
    "scm_sales_shipment_detail": ("company_code", None, "sku_code"),
    # sku_inventory stores the warehouse display name. Warehouse-code scope is
    # applied by the governed handler through the approved master_wms mapping.
    "sku_inventory": ("owner", None, "sku_code"),
    "scm_sales_shipment_allocate_wms": ("cargo_owner_id", "wms_code", "sku_code"),
    "scm_warehous_manage": ("cargo_owner_id", "wms_code", "sku_code"),
    "scm_warehous_manage_backups": ("cargo_owner_id", "wms_code", "sku_code"),
    "scm_warehous_occupy": ("cargo_owner_id", "wms_code", "sku_code"),
    "scm_warehous_header": ("cargo_owner_id", None, None),
    "scm_warehous_detail": ("cargo_owner_id", "wms_code", "sku_code"),
}


def _trim(column: str) -> exp.Expression:
    return exp.Trim(this=exp.cast(exp.column(column), "STRING"))


def apply_scope(
    sql: str, scope: QueryScope, *, legacy_owner: str,
    inventory_warehouse_pairs: tuple[tuple[str, str], ...] | None = None,
) -> str:
    tree = sqlglot.parse_one(sql, read="doris")
    # Replace only the fixed route's legacy owner predicates. The original
    # joins remain owner-qualified; business predicates are not removed.
    for equality in list(tree.find_all(exp.EQ)):
        literals = [n for n in (equality.this, equality.expression) if isinstance(n, exp.Literal) and n.is_string]
        columns = list(equality.find_all(exp.Column))
        if len(literals) == 1 and literals[0].this == legacy_owner and len(columns) == 1 and columns[0].name in {"company_code", "cargo_owner_id", "cargo_owner", "owner"}:
            if scope.owner == "ALL":
                equality.replace(exp.true())
            else:
                literals[0].replace(exp.Literal.string(scope.owner))
    for table in list(tree.find_all(exp.Table)):
        container = table.find_ancestor(exp.CTE)
        # Current-inventory inputs are joined at the three-key output grain.
        # Filtering allocation.wms_code would filter the shipping warehouse,
        # whereas transfer stock belongs to the receiving warehouse.
        if container is not None and (container.alias.startswith(("inv_", "bs_")) or container.alias in {"inventory_current", "sales_current"}):
            continue
        current_inventory = not table.db and table.name == "inventory_current"
        if not current_inventory and (table.db != "ywzt_ods" or table.name not in SOURCE_DIMENSIONS):
            continue
        owner_field, warehouse_field, sku_field = ("owner", "wms_code", "sku_code") if current_inventory else SOURCE_DIMENSIONS[table.name]
        predicates = []
        if owner_field and scope.owner != "ALL":
            predicates.append(_trim(owner_field).eq(exp.Literal.string(scope.owner)))
        if warehouse_field and scope.warehouse_codes:
            predicates.append(_trim(warehouse_field).isin(*[exp.Literal.string(v) for v in scope.warehouse_codes]))
        if table.name == "sku_inventory" and scope.warehouse_codes:
            if not inventory_warehouse_pairs:
                raise ValueError("inventory warehouse names must be resolved before compilation")
            predicates.append(exp.or_(*[
                exp.and_(
                    _trim("owner").eq(exp.Literal.string(owner)),
                    _trim("wms_desc").eq(exp.Literal.string(name)),
                )
                for owner, name in inventory_warehouse_pairs
            ]))
        if sku_field and scope.sku_codes:
            predicates.append(_trim(sku_field).isin(*[exp.Literal.string(v) for v in scope.sku_codes]))
        if sku_field and (scope.product_terms or scope.finished_goods):
            sku_filter = []
            if scope.finished_goods:
                category = _trim("sku_group").eq(exp.Literal.string("50"))
                if scope.finished_goods_exceptions:
                    category = exp.or_(category, _trim("sku_code").isin(*[exp.Literal.string(code) for code in scope.finished_goods_exceptions]))
                sku_filter.append(category)
            if scope.product_terms:
                sku_filter.append(exp.or_(*[exp.Like(this=exp.column("sku_name"), expression=exp.Literal.string("%" + v + "%")) for v in scope.product_terms]))
            if table.name == "master_sku":
                predicates.extend(sku_filter)
            else:
                sku_set = exp.select(_trim("sku_code")).distinct().from_("ywzt_ods.master_sku").where(exp.and_(*sku_filter))
                membership = exp.In(this=_trim(sku_field), query=sku_set.subquery())
                if scope.finished_goods_exceptions and not scope.product_terms:
                    membership = exp.or_(membership, _trim(sku_field).isin(*[exp.Literal.string(code) for code in scope.finished_goods_exceptions]))
                predicates.append(membership)
        if predicates:
            source = table.copy()
            source.set("alias", None)
            table.replace(exp.select("*").from_(source).where(exp.and_(*predicates)).subquery(table.alias_or_name))
    return tree.sql(dialect="doris")
