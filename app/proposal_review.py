"""Trusted snapshot capture and screened, non-query proposal review output."""

from pathlib import Path
import re

from .errors import RuntimeErrorBase
from .knowledge import KnowledgeBase
from .knowledge_activation import active_snapshot
from .knowledge_changes import KnowledgeSnapshot
from .sql import TemplateCompiler


FIELD_LABELS = {
    "business_definition": "业务定义", "formula": "计算口径", "grain": "统计粒度",
    "unit": "计量单位", "display_name": "指标名称", "notes": "口径说明",
    "version": "口径版本", "updated_at": "修订日期",
}


class ProposalDescriptionError(RuntimeErrorBase):
    code = "proposal_description_required"
    http_status = 422

    def __init__(self):
        super().__init__("提案包含尚不能直接展示的内部定义，请先补充可核对的业务口径说明；未提交任何选择")


def active_proposal_snapshot(root=None):
    root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    snapshot, digest = active_snapshot(root)
    if digest is None:
        raise ValueError("activated_knowledge_required")
    knowledge = KnowledgeBase(snapshot, release_id=snapshot.name, reload_on_change=False,
                              require_trusted_release=True, trusted_release_sha256=digest)
    references = knowledge.selected_template_refs()
    compiler = TemplateCompiler(snapshot, reload_on_change=False, preload_template_refs=references)
    digests = compiler.cached_file_digests()
    knowledge.verify_runtime_snapshot_integrity(digests)
    return KnowledgeSnapshot(metrics=knowledge.metrics(), sources=knowledge.sources(),
                             templates={reference: digests[reference] for reference in references})


def public_proposal_assessment(baseline, report, *, screen_text):
    """Never manufacture a human explanation from arbitrary SQL/configuration."""
    def text(value):
        screened = screen_text(value) if isinstance(value, str) else None
        if screened is None or re.search(r"\b(?:sk-[A-Za-z0-9_-]{8,}|bearer\s+\S+)|\b[A-Za-z0-9]+_[A-Za-z0-9_]+\b|[A-Za-z_]\w*\s*\(", screened, re.IGNORECASE):
            raise ProposalDescriptionError()
        return screened

    rows = []
    questions = []
    changes = {(change["kind"], change["id"]): change for change in report["changes"]}
    for decision in report["questions"]:
        kind, identifier, field = decision["kind"], decision["contract_id"], decision["field"]
        if kind != "metric" or field not in FIELD_LABELS:
            raise ProposalDescriptionError()
        metric = baseline.contracts[kind + ":" + identifier]
        title = text(metric["display_name"])
        change = changes.get((kind, identifier))
        difference = next((item for item in change["fields"] if item["path"] == "/" + field), None) if change else None
        # Selected/deferred fields can be absent from the effective diff. The
        # original comparison is supplied by the context store for presentation.
        if difference is None:
            raise ProposalDescriptionError()
        before = text(difference["before"]) if difference["before_exists"] else "未定义"
        after = text(difference["after"]) if difference["after_exists"] else "移除该定义"
        label = FIELD_LABELS[field]
        rows.append({"decision_id": decision["id"], "metric": title, "field": label,
                     "before": before, "after": after})
        questions.append({"id": decision["id"], "prompt": title + "：" + label,
                          "selection": "single", "options": [
                              {"id": "current", "label": "保留当前定义"},
                              {"id": "proposed", "label": "采用本次提案"},
                              {"id": "defer", "label": "暂不确认"},
                          ]})
    return {
        "state": report["state"], "title": "口径变更评估", "comparisons": rows,
        "questions": questions, "choices": dict(report.get("choices", {})),
        "requires_confirmation": report["requires_confirmation"],
        "assessment_complete": report["assessment_complete"], "publication_authorized": False,
        "notice": "仅评估本次提案，未查询数据、未发布口径；历史结果不能直接与变更后的口径比较。",
        "dependency_state": "需要修正依赖关系" if report["issues"] else "已检查声明的依赖关系",
    }
