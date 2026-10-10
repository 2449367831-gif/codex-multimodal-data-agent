"""Safely check and optionally sync the curated source-project knowledge snapshot.

The source project contains useful historical SQL and Python, but it is not a
runtime contract.  This tool treats ``logic-catalog.yaml`` as an explicit
allow-list and copies only files named by that catalog.  The default operation
is read-only, reports source-like files outside the allow-list by path only,
and emits a JSON report suitable for CI or an audit trail.

Examples::

    python -m tools.sync_source_project --source-root D:\\数仓取数规则
    python -m tools.sync_source_project --sync --source-root D:\\数仓取数规则
    python -m tools.sync_source_project --fail-on-unlisted-source --source-root D:\\数仓取数规则

``--sync`` never edits metrics, source contracts, release manifests, or the
catalog itself.  It also refuses to copy when a listed source is missing,
unsafe, or contains a high-confidence hard-coded secret.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tempfile
from typing import Any, Iterable, Mapping, Sequence

import yaml


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_ROOT = Path(r"D:\数仓取数规则")
DEFAULT_DESTINATION_ROOT = ROOT / "knowledge" / "source-project"
DEFAULT_CATALOG = DEFAULT_DESTINATION_ROOT / "logic-catalog.yaml"
CATALOG_PREFIX = PurePosixPath("knowledge/source-project")
ALLOWED_EXTENSIONS = {".md", ".sql", ".py"}

# These names are rejected even when someone accidentally adds them to the
# catalog.  In particular, a basename allow-list alone would allow a secret
# file under a harmless-looking directory.
FORBIDDEN_COMPONENTS = {
    ".env",
    ".git",
    ".venv",
    "build",
    "credentials",
    "credential",
    "deploy",
    "dist",
    "node_modules",
    "runtime",
    "runtime_data",
    "secrets",
    "secret",
    "tmp",
}

# Source-project workspaces also contain investigation scratchpads, generated
# outputs, and Python caches.  They are deliberately excluded from the
# unlisted-file inventory; the inventory is for human review of source-like
# business files, not a recursive copy or a content scan.
SOURCE_SCAN_EXCLUDED_COMPONENTS = FORBIDDEN_COMPONENTS | {
    ".codex_tmp",
    "__pycache__",
    "outputs",
    "var",
}

# Deliberately conservative patterns.  Environment lookups and placeholders
# are allowed; literal values that look like credentials are not.  Matched
# values are never included in the report.
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai_api_key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    (
        "credential_literal",
        re.compile(
            r"(?i)\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|password|secret|webhook[_-]?url)\b"
            r"\s*[:=]\s*['\"]([^'\"]{8,})['\"]"
        ),
    ),
)


class SourceProjectSyncError(ValueError):
    """Raised when the catalog or a requested path is unsafe/invalid."""


class DuplicateKeyLoader(yaml.SafeLoader):
    """YAML loader that rejects duplicate keys instead of silently merging."""


def _construct_mapping_without_duplicates(
    loader: DuplicateKeyLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key: {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


DuplicateKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping_without_duplicates,
)


@dataclass(frozen=True)
class ManagedFile:
    """A catalog path and its corresponding path in the source project."""

    destination_rel: str
    source_rel: str
    extension: str


@dataclass
class FileReport:
    destination: str
    source: str
    status: str
    source_sha256: str | None = None
    destination_sha256: str | None = None
    source_bytes: int | None = None
    destination_bytes: int | None = None
    source_error: str | None = None
    destination_error: str | None = None
    action: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "destination": self.destination,
            "source": self.source,
            "status": self.status,
            "source_sha256": self.source_sha256,
            "destination_sha256": self.destination_sha256,
            "source_bytes": self.source_bytes,
            "destination_bytes": self.destination_bytes,
            "source_error": self.source_error,
            "destination_error": self.destination_error,
            "action": self.action,
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _display_path(path: Path) -> str:
    """Use a stable, slash-separated path in JSON reports."""

    return path.as_posix()


def _resolve_root(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve(strict=False)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True


def _safe_relative(value: str, *, field: str) -> PurePosixPath:
    if not isinstance(value, str) or not value.strip():
        raise SourceProjectSyncError(f"{field} must be a non-empty relative path")
    # Catalog paths are normalized POSIX paths so the same catalog behaves on
    # Windows and in CI/Linux.  Backslashes are rejected rather than guessed.
    if "\\" in value:
        raise SourceProjectSyncError(f"{field} must use '/' separators: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise SourceProjectSyncError(f"{field} escapes its root: {value!r}")
    if not path.parts:
        raise SourceProjectSyncError(f"{field} is empty")
    return path


def _validate_destination_catalog_path(value: str) -> PurePosixPath:
    path = _safe_relative(value, field="catalog source file")
    try:
        rel = path.relative_to(CATALOG_PREFIX)
    except ValueError as exc:
        raise SourceProjectSyncError(
            f"catalog file must be under {CATALOG_PREFIX.as_posix()}/: {value!r}"
        ) from exc
    if not rel.parts:
        raise SourceProjectSyncError(f"catalog file points at the source-project root: {value!r}")
    extension = Path(rel.name).suffix.casefold()
    if extension not in ALLOWED_EXTENSIONS:
        raise SourceProjectSyncError(
            f"catalog file extension {extension!r} is not allowed for {value!r}"
        )
    if any(part.casefold() in FORBIDDEN_COMPONENTS for part in rel.parts):
        raise SourceProjectSyncError(f"catalog file is under a forbidden component: {value!r}")
    if extension == ".md" and rel.parts[0].casefold() != "docs":
        raise SourceProjectSyncError(f"Markdown source files must be under docs/: {value!r}")
    if extension == ".sql" and rel.parts[0].casefold() != "sql":
        raise SourceProjectSyncError(f"SQL source files must be under sql/: {value!r}")
    return rel


def _catalog_paths(catalog: Mapping[str, Any]) -> list[str]:
    source_project = catalog.get("source_project")
    if not isinstance(source_project, Mapping):
        raise SourceProjectSyncError("catalog.source_project must be a mapping")

    values: list[str] = []

    def add_many(value: Any, field: str) -> None:
        if value is None:
            return
        if not isinstance(value, list):
            raise SourceProjectSyncError(f"catalog.{field} must be a list")
        for item in value:
            if not isinstance(item, str):
                raise SourceProjectSyncError(f"catalog.{field} entries must be strings")
            values.append(item)

    add_many(source_project.get("primary_documents"), "source_project.primary_documents")
    add_many(source_project.get("imported_script_files"), "source_project.imported_script_files")
    # An explicit imported SQL list is supported for future catalog versions.
    add_many(source_project.get("imported_sql_files"), "source_project.imported_sql_files")

    families = catalog.get("logic_families", [])
    if not isinstance(families, list):
        raise SourceProjectSyncError("catalog.logic_families must be a list")
    for index, family in enumerate(families):
        if not isinstance(family, Mapping):
            raise SourceProjectSyncError(f"catalog.logic_families[{index}] must be a mapping")
        add_many(family.get("source_files"), f"logic_families[{index}].source_files")

    # Preserve first-seen order for human-readable reports, while rejecting
    # invalid paths before deduplication can hide a bad catalog entry.
    unique: list[str] = []
    seen: set[str] = set()
    seen_casefold: dict[str, str] = {}
    for value in values:
        _validate_destination_catalog_path(value)
        folded = value.casefold()
        previous = seen_casefold.get(folded)
        if previous is not None and previous != value:
            raise SourceProjectSyncError(
                f"catalog paths collide on a case-insensitive filesystem: {previous!r} and {value!r}"
            )
        seen_casefold[folded] = value
        if value not in seen:
            seen.add(value)
            unique.append(value)
    if not unique:
        raise SourceProjectSyncError("catalog does not list any docs/SQL/Python source files")
    return unique


def _source_relative_for(destination_rel: PurePosixPath, catalog: Mapping[str, Any]) -> PurePosixPath:
    """Map a destination path to its source-project relative path.

    The deliverable SQL package is the only non-identity mapping in the current
    snapshot.  Catalogs may add an explicit ``source_project.path_mappings``
    map when another package layout is needed.
    """

    source_project = catalog.get("source_project", {})
    mappings = source_project.get("path_mappings", {}) if isinstance(source_project, Mapping) else {}
    destination_full = f"{CATALOG_PREFIX.as_posix()}/{destination_rel.as_posix()}"
    if isinstance(mappings, Mapping):
        explicit = mappings.get(destination_full) or mappings.get(destination_rel.as_posix())
        if explicit is not None:
            if not isinstance(explicit, str):
                raise SourceProjectSyncError(f"path mapping for {destination_full!r} must be a string")
            return _safe_relative(explicit, field=f"path mapping for {destination_full}")

    if destination_rel.parts[:2] == ("sql", "deliverables"):
        return PurePosixPath(
            "deliverables",
            "数仓Doris报表SQL包_20260824",
            "sql",
            destination_rel.name,
        )
    return destination_rel


def load_catalog(path: str | Path = DEFAULT_CATALOG) -> dict[str, Any]:
    """Load and validate the catalog, returning a plain mapping."""

    catalog_path = Path(path)
    try:
        with catalog_path.open(encoding="utf-8") as stream:
            value = yaml.load(stream, Loader=DuplicateKeyLoader)
    except OSError as exc:
        raise SourceProjectSyncError(f"cannot read catalog {catalog_path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise SourceProjectSyncError(f"catalog YAML is invalid: {exc}") from exc
    if not isinstance(value, dict):
        raise SourceProjectSyncError("catalog top level must be a mapping")
    # Validate all paths now, even in check mode, before touching any file.
    _catalog_paths(value)
    return value


def managed_files(catalog: Mapping[str, Any]) -> list[ManagedFile]:
    """Return the deduplicated catalog allow-list."""

    result: list[ManagedFile] = []
    for value in _catalog_paths(catalog):
        destination_rel = _validate_destination_catalog_path(value)
        source_rel = _source_relative_for(destination_rel, catalog)
        if any(part.casefold() in FORBIDDEN_COMPONENTS for part in source_rel.parts):
            raise SourceProjectSyncError(
                f"source mapping for {value!r} is under a forbidden component"
            )
        if Path(source_rel.name).suffix.casefold() not in ALLOWED_EXTENSIONS:
            raise SourceProjectSyncError(f"mapped source extension is not allowed for {value!r}")
        result.append(
            ManagedFile(
                destination_rel=destination_rel.as_posix(),
                source_rel=source_rel.as_posix(),
                extension=Path(destination_rel.name).suffix.casefold(),
            )
        )
    return result


def _sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _secret_findings(path: Path) -> list[str]:
    """Return pattern names only; never return secret text."""

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    findings: list[str] = []
    for name, pattern in SECRET_PATTERNS:
        matches = list(pattern.finditer(text))
        if not matches:
            continue
        if name == "credential_literal":
            # A source reference may document how to read a credential from an
            # environment variable.  That is not a literal secret and is safe
            # to carry as evidence.
            literal_values = [match.group(1).strip() for match in matches]
            literal_values = [
                value
                for value in literal_values
                if not (
                    value.startswith(
                        (
                            "${",
                            "%",
                            "os.getenv",
                            "os.environ",
                            "getenv(",
                            "env.",
                            "settings.",
                            "f\"",
                            "f'",
                        )
                    )
                    or value.casefold() in {"changeme", "replace-me", "your-value"}
                )
            ]
            if not literal_values:
                continue
        findings.append(name)
    return findings


def _path_is_safe_file(path: Path, root: Path) -> tuple[bool, str | None]:
    if not _is_within(path, root):
        return False, "resolved path escapes its root"
    if path.is_symlink():
        return False, "symbolic links are not allowed"
    if not path.exists():
        return False, "file does not exist"
    if not path.is_file():
        return False, "path is not a regular file"
    return True, None


def _destination_files(destination_root: Path) -> Iterable[Path]:
    if not destination_root.is_dir():
        return ()
    # Keep symlinks in the scan so they are reported and can never be followed
    # accidentally by a later sync.
    return (path for path in destination_root.rglob("*") if path.is_file() or path.is_symlink())


def _source_like_files(source_root: Path) -> Iterable[str]:
    """Yield source-like paths without opening or following excluded trees."""

    if not source_root.is_dir():
        return ()
    # os.walk lets us prune excluded directories before touching their
    # contents.  Do not follow symlinked directories while building this
    # review-only inventory.
    paths: list[str] = []
    for current, directories, filenames in os.walk(source_root, topdown=True, followlinks=False):
        current_path = Path(current)
        directories[:] = [
            name
            for name in directories
            if name.casefold() not in SOURCE_SCAN_EXCLUDED_COMPONENTS
            and not (current_path / name).is_symlink()
        ]
        for name in filenames:
            path = current_path / name
            if path.is_symlink() or path.suffix.casefold() not in ALLOWED_EXTENSIONS:
                continue
            relative = path.relative_to(source_root).as_posix()
            if any(part.casefold() in SOURCE_SCAN_EXCLUDED_COMPONENTS for part in Path(relative).parts):
                continue
            paths.append(relative)
    return tuple(sorted(paths))


def _make_file_report(
    item: ManagedFile,
    source_root: Path,
    destination_root: Path,
) -> FileReport:
    source_path = source_root / Path(item.source_rel)
    destination_path = destination_root / Path(item.destination_rel)
    report = FileReport(
        destination=item.destination_rel,
        source=item.source_rel,
        status="unknown",
    )

    source_ok, source_error = _path_is_safe_file(source_path, source_root)
    if not source_ok:
        report.status = "missing_source" if source_error == "file does not exist" else "blocked"
        report.source_error = source_error
        return report
    try:
        report.source_sha256, report.source_bytes = _sha256(source_path)
    except OSError as exc:
        report.status = "blocked"
        report.source_error = f"cannot hash source: {exc}"
        return report
    findings = _secret_findings(source_path)
    if findings:
        report.status = "blocked"
        report.source_error = "possible hard-coded credential pattern(s): " + ", ".join(findings)
        return report

    if destination_path.exists() or destination_path.is_symlink():
        destination_ok, destination_error = _path_is_safe_file(destination_path, destination_root)
        if not destination_ok:
            report.status = "blocked"
            report.destination_error = destination_error
            return report
        try:
            report.destination_sha256, report.destination_bytes = _sha256(destination_path)
        except OSError as exc:
            report.status = "blocked"
            report.destination_error = f"cannot hash destination: {exc}"
            return report
        destination_findings = _secret_findings(destination_path)
        if destination_findings:
            report.status = "blocked"
            report.destination_error = (
                "destination contains possible hard-coded credential pattern(s): "
                + ", ".join(destination_findings)
            )
            return report
        report.status = "clean" if report.source_sha256 == report.destination_sha256 else "drift"
    else:
        report.status = "missing_destination"
    return report


def _relative_destination(path: Path, root: Path) -> str:
    # Use the lexical path here.  Resolving a symlink could escape the root and
    # raise before we have a chance to report that it is unsafe.
    return path.relative_to(root).as_posix()


def inspect_source_project(
    source_root: str | Path,
    destination_root: str | Path = DEFAULT_DESTINATION_ROOT,
    catalog_path: str | Path = DEFAULT_CATALOG,
    *,
    mode: str = "check",
) -> dict[str, Any]:
    """Build a JSON-compatible check report without changing files."""

    if mode not in {"check", "sync"}:
        raise SourceProjectSyncError(f"unsupported mode: {mode!r}")
    source_root_path = _resolve_root(source_root)
    destination_root_path = _resolve_root(destination_root)
    catalog_path_obj = Path(catalog_path)
    if not catalog_path_obj.is_absolute():
        # Callers commonly pass a path relative to the repository root.
        catalog_path_obj = ROOT / catalog_path_obj
    catalog_path_obj = catalog_path_obj.resolve(strict=False)
    if not _is_within(catalog_path_obj, destination_root_path.parent) and catalog_path_obj != destination_root_path / "logic-catalog.yaml":
        # A custom catalog is allowed, but it must not be inside the source
        # project tree where sync could overwrite it.
        if _is_within(catalog_path_obj, source_root_path):
            raise SourceProjectSyncError("catalog must not be located under source-root")

    catalog = load_catalog(catalog_path_obj)
    files = managed_files(catalog)
    file_reports = [_make_file_report(item, source_root_path, destination_root_path) for item in files]
    source_like_paths = tuple(_source_like_files(source_root_path))
    managed_sources = {item.source_rel for item in files}
    unlisted_source_files = sorted(path for path in source_like_paths if path not in managed_sources)
    managed_destinations = {item.destination_rel for item in files}

    extras: list[str] = []
    forbidden_destination_files: list[str] = []
    unsafe_destination_files: list[str] = []
    for path in _destination_files(destination_root_path):
        rel = _relative_destination(path, destination_root_path)
        if rel in managed_destinations:
            continue
        if path.is_symlink():
            unsafe_destination_files.append(rel)
            continue
        # README/catalog and other project metadata are intentionally retained,
        # but source-like extras are surfaced so the allow-list cannot silently
        # grow.
        if path.suffix.casefold() in ALLOWED_EXTENSIONS:
            extras.append(rel)
        if any(part.casefold() in FORBIDDEN_COMPONENTS for part in path.relative_to(destination_root_path).parts):
            forbidden_destination_files.append(rel)

    counts: dict[str, int] = {}
    for item in file_reports:
        counts[item.status] = counts.get(item.status, 0) + 1
    source_project = catalog.get("source_project", {})
    declared_scope = source_project.get("imported_scope", {}) if isinstance(source_project, Mapping) else {}
    actual_scope = {
        "imported_sql_files": sum(item.extension == ".sql" for item in files),
        "imported_core_sql_files": sum(
            item.extension == ".sql" and not item.destination_rel.startswith("sql/deliverables/")
            for item in files
        ),
        "imported_deliverable_sql_files": sum(
            item.extension == ".sql" and item.destination_rel.startswith("sql/deliverables/")
            for item in files
        ),
        "doris_query_scripts": sum(item.extension == ".py" for item in files),
        "source_sql_files_reviewed": sum(Path(path).suffix.casefold() == ".sql" for path in source_like_paths),
        "source_script_files_reviewed": sum(Path(path).suffix.casefold() == ".py" for path in source_like_paths),
        "source_document_files_reviewed": sum(Path(path).suffix.casefold() == ".md" for path in source_like_paths),
        "known_unlisted_sql_files": sum(Path(path).suffix.casefold() == ".sql" for path in unlisted_source_files),
        "known_unlisted_script_files": sum(Path(path).suffix.casefold() == ".py" for path in unlisted_source_files),
        "known_unlisted_document_files": sum(Path(path).suffix.casefold() == ".md" for path in unlisted_source_files),
    }
    scope_count_mismatches: list[dict[str, int | str]] = []
    if isinstance(declared_scope, Mapping):
        for key, actual in actual_scope.items():
            declared = declared_scope.get(key)
            if isinstance(declared, int) and declared != actual:
                scope_count_mismatches.append({"field": key, "declared": declared, "actual": actual})
    blocking_statuses = {"missing_source", "blocked"}
    drift_statuses = {"missing_source", "missing_destination", "drift", "blocked"}
    report: dict[str, Any] = {
        "schema_version": "1.0.0",
        "generated_at": _utc_now(),
        "mode": mode,
        "catalog": {
            "path": _display_path(catalog_path_obj),
            "id": catalog.get("catalog_id"),
            "version": catalog.get("catalog_version"),
            "status": catalog.get("status"),
        },
        "source_root": _display_path(source_root_path),
        "destination_root": _display_path(destination_root_path),
        "files": [item.to_dict() for item in file_reports],
        "extra_destination_files": sorted(extras),
        "forbidden_destination_files": sorted(forbidden_destination_files),
        "unsafe_destination_files": sorted(unsafe_destination_files),
        "unlisted_source_files": unlisted_source_files,
        "source_scan": {
            "enabled": True,
            "excluded_components": sorted(SOURCE_SCAN_EXCLUDED_COMPONENTS),
            "content_read": False,
        },
        "catalog_scope": {
            "declared": dict(declared_scope) if isinstance(declared_scope, Mapping) else {},
            "actual_allowlist": actual_scope,
            "count_mismatches": scope_count_mismatches,
        },
        "summary": {
            "managed_file_count": len(file_reports),
            "clean": counts.get("clean", 0),
            "drift": counts.get("drift", 0),
            "missing_source": counts.get("missing_source", 0),
            "missing_destination": counts.get("missing_destination", 0),
            "blocked": counts.get("blocked", 0),
            "extra_destination_count": len(extras),
            "forbidden_destination_count": len(forbidden_destination_files),
            "unsafe_destination_count": len(unsafe_destination_files),
            "unlisted_source_count": len(unlisted_source_files),
            "scope_count_mismatch_count": len(scope_count_mismatches),
            "has_drift": any(item.status in drift_statuses for item in file_reports),
            "sync_blocked": any(item.status in blocking_statuses for item in file_reports)
            or bool(forbidden_destination_files)
            or bool(unsafe_destination_files)
            or bool(scope_count_mismatches),
            # A catalog count mismatch means the allow-list itself is stale;
            # fail closed until a human updates it deliberately.
            "catalog_scope_mismatch": bool(scope_count_mismatches),
        },
        "notes": [
            "Only catalog-listed .md/.sql/.py files are managed.",
            "Unlisted source-like files are reported by path only; they are not read or copied.",
            "This report does not approve or modify metric/source contracts.",
            "Imported source files are reference evidence and are never executed by this tool.",
        ],
    }
    return report


def _atomic_copy(source: Path, destination: Path) -> None:
    """Copy one file atomically, refusing to traverse a destination symlink."""

    if destination.is_symlink():
        raise SourceProjectSyncError(f"destination is a symbolic link: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not destination.is_file():
        raise SourceProjectSyncError(f"destination is not a regular file: {destination}")
    fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.sync-", dir=str(destination.parent))
    os.close(fd)
    temporary = Path(temp_name)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def sync_source_project(report: Mapping[str, Any]) -> dict[str, Any]:
    """Apply a previously inspected report and return a post-sync report.

    The report must have been produced by :func:`inspect_source_project` in
    ``sync`` mode.  Validation happens again before every copy; a stale report
    cannot cause an unsafe file to be written.
    """

    if report.get("mode") != "sync":
        raise SourceProjectSyncError("sync_source_project requires a report created with mode='sync'")
    summary = report.get("summary")
    if not isinstance(summary, Mapping) or summary.get("sync_blocked"):
        raise SourceProjectSyncError("sync blocked by missing, unsafe, or forbidden files")

    source_root = _resolve_root(str(report["source_root"]))
    destination_root = _resolve_root(str(report["destination_root"]))
    catalog_path = Path(str(report["catalog"]["path"]))
    catalog = load_catalog(catalog_path)
    actions: list[dict[str, str]] = []
    for item in managed_files(catalog):
        source = source_root / Path(item.source_rel)
        destination = destination_root / Path(item.destination_rel)
        source_ok, source_error = _path_is_safe_file(source, source_root)
        if not source_ok:
            raise SourceProjectSyncError(f"cannot sync {item.destination_rel}: {source_error}")
        if _secret_findings(source):
            raise SourceProjectSyncError(f"cannot sync {item.destination_rel}: source credential pattern detected")
        # Always resolve the target, including a not-yet-created file.  The
        # short-circuit form would allow a symlinked parent directory to make
        # an apparently missing destination escape the managed root.
        destination_ok = (
            not destination.is_symlink()
            and _is_within(destination, destination_root)
            and all(
                not parent.is_symlink()
                for parent in destination.parents
                if parent != destination_root and _is_within(parent, destination_root)
            )
        )
        if not destination_ok:
            raise SourceProjectSyncError(f"cannot sync {item.destination_rel}: unsafe destination")
        source_hash, _ = _sha256(source)
        destination_hash = None
        if destination.exists():
            destination_hash, _ = _sha256(destination)
        if source_hash != destination_hash:
            _atomic_copy(source, destination)
            actions.append({"destination": item.destination_rel, "action": "copied"})
        else:
            actions.append({"destination": item.destination_rel, "action": "unchanged"})

    post = inspect_source_project(
        source_root,
        destination_root,
        catalog_path,
        mode="sync",
    )
    post["sync_actions"] = actions
    post["summary"]["sync_action_count"] = sum(action["action"] == "copied" for action in actions)
    return post


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="read-only check (default)")
    mode.add_argument("--sync", action="store_true", help="copy catalog-listed files after validation")
    parser.add_argument(
        "--source-root",
        default=os.environ.get("SOURCE_PROJECT_ROOT", str(DEFAULT_SOURCE_ROOT)),
        help="original source project root (default: D:\\数仓取数规则)",
    )
    parser.add_argument(
        "--destination-root",
        default=str(DEFAULT_DESTINATION_ROOT),
        help="knowledge/source-project destination root",
    )
    parser.add_argument("--catalog", default=str(DEFAULT_CATALOG), help="logic-catalog.yaml path")
    parser.add_argument("--output", help="optional JSON report path; omitted means stdout only")
    parser.add_argument(
        "--fail-on-extra",
        action="store_true",
        help="return exit code 1 when unlisted source-like destination files exist",
    )
    parser.add_argument(
        "--fail-on-unlisted-source",
        action="store_true",
        help="return exit code 1 when source-like files are outside the catalog allow-list",
    )
    return parser


def _write_json_report(report: Mapping[str, Any], output: str | None) -> None:
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if output is None:
        print(payload, end="")
        return
    target = Path(output).expanduser().resolve(strict=False)
    # A report must not be written into either managed tree.  This prevents a
    # typo from turning an audit output into a source/knowledge overwrite.
    source_root = Path(str(report["source_root"])).resolve(strict=False)
    destination_root = Path(str(report["destination_root"])).resolve(strict=False)
    if _is_within(target, source_root) or _is_within(target, destination_root):
        raise SourceProjectSyncError("--output must be outside source-root and destination-root")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(payload, encoding="utf-8")
    print(payload, end="")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    mode = "sync" if args.sync else "check"
    try:
        report = inspect_source_project(
            args.source_root,
            args.destination_root,
            args.catalog,
            mode=mode,
        )
        if mode == "sync":
            if report["summary"]["sync_blocked"]:
                report["notes"].append("Sync was not applied because the preflight report is blocked.")
            else:
                report = sync_source_project(report)
        _write_json_report(report, args.output)
    except (SourceProjectSyncError, OSError, ValueError, yaml.YAMLError) as exc:
        error = {"schema_version": "1.0.0", "status": "error", "error": str(exc)}
        print(json.dumps(error, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2

    summary = report["summary"]
    if (
        summary.get("has_drift")
        or summary.get("forbidden_destination_count")
        or summary.get("unsafe_destination_count")
        or summary.get("scope_count_mismatch_count")
        or (args.fail_on_extra and summary.get("extra_destination_count"))
        or (args.fail_on_unlisted_source and summary.get("unlisted_source_count"))
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
