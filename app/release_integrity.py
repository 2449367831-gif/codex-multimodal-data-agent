from __future__ import annotations

from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

import yaml


INTEGRITY_ALGORITHM = "sha256"
RUNTIME_DEPENDENCY_PATHS = (
    "app/routing_aliases.yaml",
    "app/template_capabilities.yaml",
)


class ReleaseIntegrityError(ValueError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ReleaseIntegrityError(f"Unable to read protected YAML file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReleaseIntegrityError(f"Protected YAML file must contain a mapping: {path}")
    return value


def _safe_path(project_root: Path, relative_path: str) -> Path:
    pure = PurePosixPath(relative_path)
    if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        raise ReleaseIntegrityError(f"Unsafe protected path: {relative_path!r}")
    path = (project_root / Path(*pure.parts)).resolve()
    try:
        path.relative_to(project_root)
    except ValueError as exc:
        raise ReleaseIntegrityError(f"Protected path escapes project root: {relative_path!r}") from exc
    return path


def _relative(project_root: Path, path: Path) -> str:
    return path.resolve().relative_to(project_root).as_posix()


def _index_contracts(
    paths: Iterable[Path],
    *,
    kind: str,
    identifier_field: str = "id",
) -> dict[tuple[str, str], Path]:
    index: dict[tuple[str, str], Path] = {}
    for path in sorted(paths):
        value = _load_yaml(path)
        identifier = value.get(identifier_field)
        version = value.get("version")
        if not isinstance(identifier, str) or not isinstance(version, str):
            raise ReleaseIntegrityError(
                f"{kind} contract lacks {identifier_field}/version: {path}"
            )
        key = (identifier, version)
        if key in index:
            raise ReleaseIntegrityError(f"Duplicate {kind} contract: {identifier}@{version}")
        index[key] = path.resolve()
    return index


def _split_ref(value: Any, *, kind: str) -> tuple[str, str]:
    if not isinstance(value, str) or "@" not in value:
        raise ReleaseIntegrityError(f"Invalid {kind} reference: {value!r}")
    identifier, version = value.rsplit("@", 1)
    if not identifier or not version:
        raise ReleaseIntegrityError(f"Invalid {kind} reference: {value!r}")
    return identifier, version


def protected_release_paths(project_root: Path, manifest: dict[str, Any]) -> tuple[str, ...]:
    """Return the exact, deterministic protected file set for a release manifest."""
    root = project_root.resolve()
    contracts = manifest.get("contracts")
    if not isinstance(contracts, dict):
        raise ReleaseIntegrityError("Release manifest contracts must be a mapping")
    metric_refs = contracts.get("metrics")
    source_refs = contracts.get("sources")
    if not isinstance(metric_refs, list) or not isinstance(source_refs, list):
        raise ReleaseIntegrityError("Release manifest contracts.metrics/sources must be lists")

    metric_index = _index_contracts(
        (root / "metrics").rglob("*.yaml"), kind="metric"
    )
    source_index = _index_contracts(
        (root / "sources" / "tables").rglob("*.yaml"), kind="source"
    )
    golden_refs = manifest.get("golden_suites")
    if not isinstance(golden_refs, list):
        raise ReleaseIntegrityError("Release manifest golden_suites must be a list")
    golden_index = _index_contracts(
        (root / "tests" / "golden_questions").rglob("*.yaml"),
        kind="golden suite",
        identifier_field="suite_id",
    )
    protected: set[str] = set(RUNTIME_DEPENDENCY_PATHS)
    protected.update(
        _relative(root, path)
        for path in (root / "sources").rglob("*.yaml")
        if path.parent != (root / "sources" / "tables")
    )
    protected.update(
        _relative(root, path)
        for path in (root / "evals").rglob("*.yaml")
    )
    protected.update(
        _relative(root, path) for path in (root / "profiles").rglob("*.yaml")
    )
    protected.update(_relative(root, path) for path in (root / "rules").rglob("*.yaml"))

    manifest_source_ids = {_split_ref(ref, kind="source")[0] for ref in source_refs}
    for ref in metric_refs:
        key = _split_ref(ref, kind="metric")
        try:
            metric_path = metric_index[key]
        except KeyError as exc:
            raise ReleaseIntegrityError(f"Missing metric contract referenced by release: {ref}") from exc
        protected.add(_relative(root, metric_path))
        metric = _load_yaml(metric_path)
        metric_sources = metric.get("sources", {})
        if not isinstance(metric_sources, dict):
            raise ReleaseIntegrityError(f"Metric {ref} sources must be a mapping")
        declared_source_ids: set[str] = set()
        for source_kind in ("required", "conditional"):
            source_ids = metric_sources.get(source_kind, [])
            if not isinstance(source_ids, list) or not all(
                isinstance(source_id, str) for source_id in source_ids
            ):
                raise ReleaseIntegrityError(
                    f"Metric {ref} sources.{source_kind} must be a string list"
                )
            declared_source_ids.update(source_ids)
        missing_source_ids = sorted(declared_source_ids - manifest_source_ids)
        if missing_source_ids:
            raise ReleaseIntegrityError(
                f"Release omits source contracts declared by metric {ref}",
                details={"missing_source_ids": missing_source_ids},
            )
        execution = metric.get("execution", {})
        execution_template = execution.get("template") if isinstance(execution, dict) else None
        template_ref = execution_template or metric.get("template")
        if template_ref is not None:
            if not isinstance(template_ref, str) or not template_ref:
                raise ReleaseIntegrityError(f"Invalid template reference in metric {ref}")
            template_path = _safe_path(root, template_ref)
            protected.add(_relative(root, template_path))
        for supporting_ref in metric.get("supporting_templates", []):
            if not isinstance(supporting_ref, str) or not supporting_ref:
                raise ReleaseIntegrityError(f"Invalid supporting template in metric {ref}")
            protected.add(_relative(root, _safe_path(root, supporting_ref)))

    for ref in source_refs:
        key = _split_ref(ref, kind="source")
        try:
            source_path = source_index[key]
        except KeyError as exc:
            raise ReleaseIntegrityError(f"Missing source contract referenced by release: {ref}") from exc
        protected.add(_relative(root, source_path))

    for ref in golden_refs:
        key = _split_ref(ref, kind="golden suite")
        try:
            golden_path = golden_index[key]
        except KeyError as exc:
            raise ReleaseIntegrityError(
                f"Missing golden suite referenced by release: {ref}"
            ) from exc
        protected.add(_relative(root, golden_path))

    for relative_path in sorted(protected):
        path = _safe_path(root, relative_path)
        if not path.is_file():
            raise ReleaseIntegrityError(f"Protected file does not exist: {relative_path}")
    return tuple(sorted(protected))


def build_manifest_integrity(project_root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    root = project_root.resolve()
    files = {
        relative_path: sha256(_safe_path(root, relative_path).read_bytes()).hexdigest()
        for relative_path in protected_release_paths(root, manifest)
    }
    return {"algorithm": INTEGRITY_ALGORITHM, "files": files}


def manifest_integrity_errors(project_root: Path, manifest: dict[str, Any]) -> list[str]:
    declared = manifest.get("integrity")
    if not isinstance(declared, dict):
        return ["missing integrity mapping"]
    if declared.get("algorithm") != INTEGRITY_ALGORITHM:
        return [f"integrity.algorithm must be {INTEGRITY_ALGORITHM}"]
    declared_files = declared.get("files")
    if not isinstance(declared_files, dict):
        return ["integrity.files must be a mapping"]
    if not all(isinstance(path, str) and isinstance(digest, str) for path, digest in declared_files.items()):
        return ["integrity.files keys and digests must be strings"]

    try:
        expected = build_manifest_integrity(project_root, manifest)["files"]
    except ReleaseIntegrityError as exc:
        return [str(exc)]
    declared_paths = set(declared_files)
    expected_paths = set(expected)
    errors: list[str] = []
    for path in sorted(expected_paths - declared_paths):
        errors.append(f"missing protected file hash: {path}")
    for path in sorted(declared_paths - expected_paths):
        errors.append(f"unexpected protected file hash: {path}")
    for path in sorted(expected_paths & declared_paths):
        if declared_files[path].casefold() != expected[path]:
            errors.append(f"SHA-256 mismatch: {path}")
    return errors


def validate_manifest_integrity(project_root: Path, manifest: dict[str, Any]) -> None:
    errors = manifest_integrity_errors(project_root, manifest)
    if errors:
        raise ReleaseIntegrityError(
            "Release manifest integrity validation failed",
            details={"release_id": manifest.get("release_id"), "errors": errors},
        )
