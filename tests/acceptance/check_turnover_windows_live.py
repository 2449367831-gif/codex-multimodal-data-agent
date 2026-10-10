"""Live turnover pipeline acceptance; business rows and images stay in memory."""

from io import BytesIO
import json
import os
from pathlib import Path
import time

from PIL import Image

from app.turnover_service import TurnoverReportRuntime
from scripts.launch_hidden import _configure_environment


class MemoryArtifacts:
    def register_bytes(self, content, **metadata):
        with Image.open(BytesIO(content)) as image:
            assert image.format == "PNG"
            assert image.width >= 1000 and image.height >= 100
            image.verify()
        return {**metadata, "id": "acceptance-memory-only"}


def main():
    environment, backend_port, _ = _configure_environment()
    os.environ.update(environment)
    runtime = TurnoverReportRuntime(
        project_root=Path(__file__).resolve().parents[2],
        backend_url=f"http://127.0.0.1:{backend_port}",
        artifact_store=MemoryArtifacts(),
        allow_unapproved_contracts=False,
    )
    failed = False
    for days in (7, 14, 30):
        started = time.monotonic()
        try:
            result = runtime.run(
                principal_hash="turnover-window-acceptance",
                conversation_id="turnover-window-acceptance",
                request_id=f"turnover-window-{days}",
                window_days=days,
            )
            assert result["quality"] in {"passed", "passed_with_warning"}
            assert f"{days}个完整自然日" in result["period"]
            assert result["business_table"]["rows"]
            print(json.dumps({"days": days, "passed": True, "quality": result["quality"],
                              "seconds": round(time.monotonic() - started, 2)}), flush=True)
            del result
        except Exception as exc:
            failed = True
            print(json.dumps({"days": days, "passed": False, "error_type": type(exc).__name__,
                              "code": getattr(exc, "code", "acceptance_failed"),
                              "seconds": round(time.monotonic() - started, 2)}), flush=True)
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
