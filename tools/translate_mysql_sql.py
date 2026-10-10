"""Translate a JSON stdin request against activated source contracts; never execute."""

from dataclasses import asdict
from decimal import Decimal
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.adhoc_sql import AdHocSqlValidator
from app.errors import RuntimeErrorBase
from app.knowledge import KnowledgeBase
from app.knowledge_activation import active_snapshot
from app.settings import RuntimeSettings
from app.sql_translation import SqlTranslationError
from app.sql_conversion import convert_request, _unique_pairs, _reject_constant


def main(*, stdin=None, stdout=None, settings=None):
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    try:
        raw = stdin.read(2000001)
        if len(raw) > 2000000:
            raise SqlTranslationError("request_too_large")
        request = json.loads(raw, parse_float=Decimal, parse_constant=_reject_constant,
                             object_pairs_hook=_unique_pairs)
        settings = settings or RuntimeSettings.from_env(ROOT)
        snapshot, digest = active_snapshot(settings.project_root)
        if digest is None:
            raise SqlTranslationError("activated_knowledge_required")
        knowledge = KnowledgeBase(snapshot, reload_on_change=False, require_trusted_release=True,
                                  trusted_release_sha256=digest)
        validator = AdHocSqlValidator(allowed_schemas=settings.sql_allowed_schemas,
                                     allowed_catalogs=settings.sql_allowed_catalogs,
                                     max_rows=settings.max_rows, max_sql_length=settings.max_sql_length)
        converted = convert_request(request, sources=knowledge.sources(), validator=validator)
        print(json.dumps(asdict(converted), ensure_ascii=True), file=stdout)
        return 0
    except (SqlTranslationError, RuntimeErrorBase, ValueError, TypeError, KeyError, OSError) as exc:
        code = exc.code if isinstance(exc, SqlTranslationError) else "invalid_request_or_knowledge"
        print(json.dumps({"status": "translation_rejected", "code": code}), file=stdout)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
