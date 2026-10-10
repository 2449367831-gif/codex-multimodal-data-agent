"""Planning for explicit inventory-occupation split questions.

The question is parsed into a bounded plan before any model is involved.  The
plan deliberately keeps the source classification visible: occupation uses
the source freeze quantity, status 1 is excluded, and the transfer prefix is
not inferred from a warehouse or SKU name.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import re

from ..query_scope import QueryScope
from .semantic_types import SemanticRequest, _compact


_TOKEN = r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}"


@dataclass(frozen=True, slots=True)
class InventoryOccupationSplitPlan:
    observed_at: datetime
    since: datetime
    transfer_prefix: str = "HJXH-TH"
    measure: str = "freeze_quantity"

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None or self.since.tzinfo is None:
            raise ValueError("occupation plan requires timezone-aware boundaries")
        if self.since >= self.observed_at:
            raise ValueError("occupation window must be nonempty")
        if self.measure != "freeze_quantity":
            raise ValueError("occupation measure is not approved")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", self.transfer_prefix):
            raise ValueError("invalid transfer prefix")


def _value(text: str, label: str) -> str | None:
    match = re.search(rf"{label}\s*[:：]?\s*({_TOKEN})", text, re.IGNORECASE)
    return match.group(1).strip() if match else None


def occupation_split_request(question: str, *, planner) -> SemanticRequest | None:
    compact = _compact(question)
    has_split_wording = any(
        phrase in compact
        for phrase in ("占用拆分", "库存占用拆分", "占用分拆", "占用分类")
    )
    has_explicit_components = "调拨占用" in compact and "销售占用" in compact
    if not (has_split_wording or has_explicit_components):
        return None
    window = re.search(r"(?:最近|近|过去)\s*(\d{1,3})\s*(?:个)?(?:自然)?(?:天|日)", question)
    if window is None:
        return None
    days = int(window.group(1))
    if not 1 <= days <= 366:
        return None
    now = planner._now().replace(microsecond=0)
    since = now - timedelta(days=days)
    owner = _value(question, r"(?:货主|owner)") or "ALL"
    warehouse = _value(question, r"仓库")
    sku = _value(question, r"SKU")
    if owner != "ALL" and not re.fullmatch(_TOKEN, owner):
        return None
    if warehouse is not None and not re.fullmatch(_TOKEN, warehouse):
        return None
    if sku is not None and not re.fullmatch(_TOKEN, sku):
        return None
    scope = QueryScope(
        owner=owner,
        warehouse_codes=(warehouse,) if warehouse else (),
        warehouse_labels=(warehouse,) if warehouse else (),
        sku_codes=(sku,) if sku else (),
        unit="EA",
    )
    plan = InventoryOccupationSplitPlan(observed_at=now, since=since)
    return SemanticRequest(
        route="inventory_occupation_split",
        start=since.date(),
        end=now.date(),
        scope=scope,
        occupation_split_plan=plan,
    )
