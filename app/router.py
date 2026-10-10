from __future__ import annotations

import re
from typing import Any

from .errors import ParameterValidationError
from .knowledge import KnowledgeBase
from .models import RouteCandidate, RouteDecision


ASCII_TOKEN_RE = re.compile(r"[a-z0-9_]+")


def _ascii_tokens(value: str) -> set[str]:
    return set(ASCII_TOKEN_RE.findall(value.casefold()))


class MetricRouter:
    """Deterministic candidate retrieval; it does not guess through ambiguity."""

    def __init__(self, knowledge_base: KnowledgeBase) -> None:
        self.knowledge_base = knowledge_base

    def route(self, question: str, *, limit: int = 5) -> RouteDecision:
        if not isinstance(question, str) or not question.strip():
            raise ParameterValidationError("question must be a non-empty string")
        normalized_question = " ".join(question.casefold().split())
        question_tokens = _ascii_tokens(normalized_question)
        candidates: list[RouteCandidate] = []

        for metric in self.knowledge_base.metrics():
            metric_id = metric["id"]
            reasons: list[str] = []
            score = 0.0

            if metric_id.casefold() in normalized_question:
                score += 100.0
                reasons.append("explicit_metric_id")

            for alias in self.knowledge_base.aliases_for(metric_id):
                normalized_alias = " ".join(alias.casefold().split())
                if normalized_alias and normalized_alias in normalized_question:
                    # Chinese business aliases are short. Give specificity a
                    # full point per character so an exact named variant wins
                    # over a shorter shared family alias, while equal family
                    # aliases still remain ambiguous.
                    alias_score = 30.0 + min(len(normalized_alias), 30)
                    score = max(score, alias_score)
                    reasons.append(f"alias:{alias}")

            searchable = " ".join(
                [
                    metric_id.replace(".", " "),
                    str(metric.get("business_definition", "")),
                    str(metric.get("formula", "")),
                ]
            ).casefold()
            overlap = question_tokens & _ascii_tokens(searchable)
            if overlap:
                score += min(len(overlap), 8) * 1.5
                reasons.append("terms:" + ",".join(sorted(overlap)))

            if score > 0:
                candidates.append(
                    RouteCandidate(
                        metric_id=metric_id,
                        version=str(metric["version"]),
                        score=round(score, 3),
                        reasons=tuple(dict.fromkeys(reasons)),
                    )
                )

        candidates.sort(key=lambda item: (-item.score, item.metric_id))
        top = candidates[: max(1, min(limit, 20))]
        selected: str | None = None
        needs_clarification = True
        clarification = "请明确要查询的指标口径。"

        if top:
            leader = top[0]
            runner_score = top[1].score if len(top) > 1 else 0.0
            if leader.score >= 30.0 and leader.score - runner_score >= 2.0:
                selected = leader.metric_id
                needs_clarification = False
                clarification = None
            else:
                clarification = "存在多个可能的指标口径，请指定 metric_id。"
        else:
            clarification = "未匹配到已登记指标，请补充指标名称或直接指定 metric_id。"

        return RouteDecision(
            question=question.strip(),
            selected_metric_id=selected,
            candidates=tuple(top),
            needs_clarification=needs_clarification,
            clarification_question=clarification,
        )
