"""Request-local scope and execution capsule."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class ScopeCapsule:
    owner: str
    warehouses: tuple[str, ...]
    skus: tuple[str, ...]
    channel: str
    unit: str
    grain: str
    finished_goods: bool = False
    finished_goods_exceptions: tuple[str, ...] = ()
    order_codes: tuple[str, ...] = ()
    owner_codes: tuple[str, ...] = ()

    @classmethod
    def from_intent(cls, intent: Any) -> "ScopeCapsule":
        scope = intent.scope if isinstance(intent.scope, Mapping) else {}
        return cls(
            owner=str(scope.get("owner") or "ALL"),
            warehouses=tuple(str(x) for x in scope.get("warehouse_codes") or ()),
            skus=tuple(str(x) for x in scope.get("sku_codes") or ()),
            channel=str(scope.get("channel") or "ALL"),
            unit=str(scope.get("unit") or "EA"),
            grain=str(intent.grain or "total"),
            finished_goods=bool(scope.get("finished_goods")),
            finished_goods_exceptions=tuple(str(x) for x in scope.get("finished_goods_exceptions") or ()),
            order_codes=tuple(str(x) for x in scope.get("order_codes") or ()),
            owner_codes=tuple(str(x) for x in scope.get("owner_codes") or ()),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "owner": self.owner,
            "warehouse_codes": list(self.warehouses),
            "sku_codes": list(self.skus),
            "channel": self.channel,
            "unit": self.unit,
            "grain": self.grain,
            "finished_goods": self.finished_goods,
            "finished_goods_exceptions": list(self.finished_goods_exceptions),
            **({"order_codes": list(self.order_codes)} if self.order_codes else {}),
            **({"owner_codes": list(self.owner_codes)} if self.owner_codes else {}),
        }


@dataclass(frozen=True, slots=True)
class ExecutionCapsule:
    route: str
    scope: ScopeCapsule
    question_digest: str

    @classmethod
    def create(cls, question: str, intent: Any) -> "ExecutionCapsule":
        return cls(
            route=intent.route,
            scope=ScopeCapsule.from_intent(intent),
            question_digest=sha256(question.encode("utf-8")).hexdigest(),
        )
