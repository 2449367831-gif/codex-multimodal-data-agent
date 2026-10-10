from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import logging
import os
from pathlib import Path
import re
from threading import RLock
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - environment guard
    raise RuntimeError("PyYAML is required; run: python -m pip install -r requirements.txt") from exc

from .errors import KnowledgeBaseError, MetricNotFoundError
from .release_integrity import ReleaseIntegrityError, validate_manifest_integrity


LOGGER = logging.getLogger("doris_query_agent.knowledge")


REQUIRED_METRIC_FIELDS = {
    "id",
    "version",
    "business_definition",
    "grain",
    "unit",
    "time_window",
    "sources",
    "parameters",
    "required_validations",
}
NON_PRODUCTION_CONTRACT_STATUSES = frozenset({"candidate"})
SEMVER_RE = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<prerelease>[0-9A-Za-z.-]+))?$"
)


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as stream:
            value = yaml.safe_load(stream)
    except (OSError, yaml.YAMLError) as exc:
        raise KnowledgeBaseError(f"Unable to load {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise KnowledgeBaseError(f"Top-level YAML value must be a mapping: {path}")
    return value


def _semver_key(version: str) -> tuple[Any, ...]:
    match = SEMVER_RE.fullmatch(version)
    if not match:
        raise KnowledgeBaseError(f"Invalid semantic version: {version}")
    prerelease = match.group("prerelease")
    prerelease_key: tuple[tuple[int, Any], ...] = ()
    if prerelease:
        prerelease_key = tuple(
            (0, int(part)) if part.isdigit() else (1, part)
            for part in prerelease.split(".")
        )
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
        prerelease is None,
        prerelease_key,
    )


def _versioned_ref(value: Any, *, kind: str) -> tuple[str, str]:
    if not isinstance(value, str) or "@" not in value:
        raise KnowledgeBaseError(f"{kind} manifest reference must be id@version: {value!r}")
    identifier, version = value.rsplit("@", 1)
    if not identifier or not version:
        raise KnowledgeBaseError(f"{kind} manifest reference must be id@version: {value!r}")
    return identifier, version


def _select_fallback_versions(
    versions: dict[tuple[str, str], dict[str, Any]],
    *,
    kind: str,
) -> dict[str, dict[str, Any]]:
    by_id: dict[str, list[dict[str, Any]]] = {}
    for (identifier, _version), contract in versions.items():
        by_id.setdefault(identifier, []).append(contract)

    selected: dict[str, dict[str, Any]] = {}
    for identifier, contracts in by_id.items():
        active = [contract for contract in contracts if contract.get("status") == "active"]
        if len(active) > 1:
            raise KnowledgeBaseError(
                f"Multiple active {kind} versions require a release manifest: {identifier}",
                details={"versions": sorted(str(item.get("version")) for item in active)},
            )
        selected[identifier] = active[0] if active else max(
            contracts,
            key=lambda item: _semver_key(str(item["version"])),
        )
    return selected


def _select_manifest_versions(
    versions: dict[tuple[str, str], dict[str, Any]],
    refs: Any,
    *,
    kind: str,
    release_id: str,
) -> dict[str, dict[str, Any]]:
    if not isinstance(refs, list):
        raise KnowledgeBaseError(
            f"Release {release_id} contracts.{kind} must be a list"
        )
    selected: dict[str, dict[str, Any]] = {}
    for ref in refs:
        identifier, version = _versioned_ref(ref, kind=kind)
        if identifier in selected:
            raise KnowledgeBaseError(
                f"Release {release_id} selects multiple {kind} versions for {identifier}"
            )
        try:
            selected[identifier] = versions[(identifier, version)]
        except KeyError as exc:
            raise KnowledgeBaseError(
                f"Release {release_id} references missing {kind} contract {ref}"
            ) from exc
    return selected


class KnowledgeBase:
    """Read-only, reloadable view over the versioned YAML contracts."""

    def __init__(
        self,
        project_root: Path,
        *,
        release_id: str | None = None,
        reload_on_change: bool = True,
        require_trusted_release: bool = False,
        trusted_release_sha256: str | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.configured_release_id = release_id or (os.getenv("AGENT_RELEASE_ID") or "").strip() or None
        self.reload_on_change = reload_on_change
        self.require_trusted_release = require_trusted_release
        self.trusted_release_sha256 = (
            trusted_release_sha256.casefold() if trusted_release_sha256 else None
        )
        self._lock = RLock()
        self._metrics: dict[str, dict[str, Any]] = {}
        self._metric_versions: dict[tuple[str, str], dict[str, Any]] = {}
        self._sources: dict[str, dict[str, Any]] = {}
        self._source_versions: dict[tuple[str, str], dict[str, Any]] = {}
        self._profiles: dict[str, dict[str, Any]] = {}
        self._validations: dict[str, dict[str, Any]] = {}
        self._releases: dict[str, dict[str, Any]] = {}
        self._release_paths: dict[str, Path] = {}
        self._release_digests: dict[str, str] = {}
        self._cached_file_digests: dict[str, str] = {}
        self._required_snapshot_paths: set[str] = set()
        self._planning_release_id: str | None = None
        self._released_release_id: str | None = None
        self._aliases: dict[str, list[str]] = {}
        self._signature: tuple[tuple[str, int, int], ...] = ()
        self._loaded_at: str | None = None
        self.reload()

    def _relative_project_path(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.project_root).as_posix()
        except ValueError as exc:
            raise KnowledgeBaseError(
                f"Runtime knowledge input must stay under the project root: {path}"
            ) from exc

    def _read_yaml_snapshot(
        self,
        path: Path,
        cached_digests: dict[str, str],
    ) -> dict[str, Any]:
        try:
            raw = path.read_bytes()
            value = yaml.safe_load(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            raise KnowledgeBaseError(f"Unable to load {path}: {exc}") from exc
        if not isinstance(value, dict):
            raise KnowledgeBaseError(f"Top-level YAML value must be a mapping: {path}")
        cached_digests[self._relative_project_path(path)] = sha256(raw).hexdigest()
        return value

    def _runtime_config_path(self, name: str) -> Path:
        repository_path = self.project_root / "app" / name
        return repository_path if repository_path.is_file() else Path(__file__).with_name(name)

    def _tracked_files(self) -> list[Path]:
        paths = list((self.project_root / "metrics").rglob("*.yaml"))
        # Keep both executable source contracts and grouped source maps in the
        # reload signature.  Group maps are prompt-facing knowledge; the
        # ``sources/tables`` contracts remain the executable source authority.
        paths.extend((self.project_root / "sources").rglob("*.yaml"))
        paths.extend((self.project_root / "profiles").rglob("*.yaml"))
        paths.extend((self.project_root / "releases").rglob("*.yaml"))
        paths.extend((self.project_root / "rules").rglob("*.yaml"))
        paths.extend((self.project_root / "evals").rglob("*.yaml"))
        paths.extend((self.project_root / "sql_templates").rglob("*.sql"))
        paths.append(self._runtime_config_path("template_capabilities.yaml"))
        alias_path = self._runtime_config_path("routing_aliases.yaml")
        if alias_path.is_file():
            paths.append(alias_path)
        return sorted({path.resolve() for path in paths})

    def _current_signature(self) -> tuple[tuple[str, int, int], ...]:
        signature = []
        for path in self._tracked_files():
            stat = path.stat()
            signature.append((str(path), stat.st_mtime_ns, stat.st_size))
        return tuple(signature)

    def reload_if_changed(self) -> bool:
        if not self.reload_on_change:
            return False
        signature = self._current_signature()
        with self._lock:
            if signature == self._signature:
                return False
        self.reload()
        return True

    def reload(self) -> None:
        metrics_by_version: dict[tuple[str, str], dict[str, Any]] = {}
        metric_paths_by_version: dict[tuple[str, str], str] = {}
        sources_by_version: dict[tuple[str, str], dict[str, Any]] = {}
        source_paths_by_version: dict[tuple[str, str], str] = {}
        profiles: dict[str, dict[str, Any]] = {}
        releases: dict[str, dict[str, Any]] = {}
        release_paths: dict[str, Path] = {}
        release_digests: dict[str, str] = {}
        cached_digests: dict[str, str] = {}
        profile_paths: set[str] = set()

        for path in sorted((self.project_root / "sources" / "tables").rglob("*.yaml")):
            value = self._read_yaml_snapshot(path, cached_digests)
            source_id = value.get("id")
            if not isinstance(source_id, str) or not source_id:
                raise KnowledgeBaseError(f"Source is missing a valid id: {path}")
            version = value.get("version")
            if not isinstance(version, str) or not version:
                raise KnowledgeBaseError(f"Source is missing a valid version: {path}")
            key = (source_id, version)
            if key in sources_by_version:
                raise KnowledgeBaseError(f"Duplicate source contract: {source_id}@{version}")
            sources_by_version[key] = value
            source_paths_by_version[key] = self._relative_project_path(path)

        for path in sorted((self.project_root / "metrics").rglob("*.yaml")):
            metric = self._read_yaml_snapshot(path, cached_digests)
            missing = sorted(REQUIRED_METRIC_FIELDS - metric.keys())
            if missing:
                # Omit unfinished candidates from the executable index. A
                # manifest selecting one still fails the exact-version check;
                # active and verified contracts are never silently skipped.
                if metric.get("status") in NON_PRODUCTION_CONTRACT_STATUSES:
                    LOGGER.warning(
                        "Ignoring incomplete non-production metric contract path=%s missing=%s",
                        self._relative_project_path(path),
                        ",".join(missing),
                    )
                    continue
                raise KnowledgeBaseError(
                    f"Metric contract {path.name} is missing fields",
                    details={"missing": missing},
                )
            metric_id = metric.get("id")
            if not isinstance(metric_id, str) or not metric_id:
                raise KnowledgeBaseError(f"Metric is missing a valid id: {path}")
            version = metric.get("version")
            if not isinstance(version, str) or not version:
                raise KnowledgeBaseError(f"Metric is missing a valid version: {path}")
            key = (metric_id, version)
            if key in metrics_by_version:
                raise KnowledgeBaseError(f"Duplicate metric contract: {metric_id}@{version}")
            source_section = metric.get("sources")
            required_sources = (
                source_section.get("required")
                if isinstance(source_section, dict)
                else None
            )
            if not isinstance(required_sources, list):
                if metric.get("status") in NON_PRODUCTION_CONTRACT_STATUSES:
                    LOGGER.warning(
                        "Ignoring non-production metric with invalid sources path=%s",
                        self._relative_project_path(path),
                    )
                    continue
                raise KnowledgeBaseError(
                    f"Metric {metric_id} has invalid sources.required",
                    details={"path": self._relative_project_path(path)},
                )
            unknown_sources = [
                source_id
                for source_id in required_sources
                if not any(source_key[0] == source_id for source_key in sources_by_version)
            ]
            if unknown_sources:
                if metric.get("status") in NON_PRODUCTION_CONTRACT_STATUSES:
                    LOGGER.warning(
                        "Ignoring non-production metric with unknown sources path=%s",
                        self._relative_project_path(path),
                    )
                    continue
                raise KnowledgeBaseError(
                    f"Metric {metric_id} references unknown required sources",
                    details={"sources": unknown_sources},
                )
            metrics_by_version[key] = metric
            metric_paths_by_version[key] = self._relative_project_path(path)

        for path in sorted((self.project_root / "profiles").rglob("*.yaml")):
            profile = self._read_yaml_snapshot(path, cached_digests)
            profile_id = profile.get("id")
            if isinstance(profile_id, str) and profile_id:
                profiles[profile_id] = profile
                profile_paths.add(self._relative_project_path(path))

        for path in sorted((self.project_root / "releases").rglob("*.yaml")):
            try:
                raw_manifest = path.read_bytes()
                manifest = yaml.safe_load(raw_manifest.decode("utf-8"))
            except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
                raise KnowledgeBaseError(f"Unable to load {path}: {exc}") from exc
            if not isinstance(manifest, dict):
                raise KnowledgeBaseError(f"Top-level YAML value must be a mapping: {path}")
            release_id = manifest.get("release_id")
            if not isinstance(release_id, str) or not release_id:
                raise KnowledgeBaseError(f"Release manifest is missing a valid release_id: {path}")
            if release_id in releases:
                raise KnowledgeBaseError(f"Duplicate release_id: {release_id}")
            releases[release_id] = manifest
            release_paths[release_id] = path.resolve()
            release_digests[release_id] = sha256(raw_manifest).hexdigest()

        released_ids = sorted(
            release_id
            for release_id, manifest in releases.items()
            if manifest.get("status") == "released"
        )
        if self.configured_release_id:
            if self.configured_release_id not in releases:
                raise KnowledgeBaseError(
                    f"Unknown AGENT_RELEASE_ID: {self.configured_release_id}",
                    details={"known_release_ids": sorted(releases)},
                )
            planning_release_id = self.configured_release_id
            released_release_id = (
                self.configured_release_id
                if releases[self.configured_release_id].get("status") == "released"
                else None
            )
        else:
            if len(released_ids) > 1:
                raise KnowledgeBaseError(
                    "Multiple released manifests exist; set AGENT_RELEASE_ID explicitly",
                    details={"released_ids": released_ids},
                )
            released_release_id = released_ids[0] if released_ids else None
            if released_release_id:
                planning_release_id = released_release_id
            else:
                candidate_ids = sorted(
                    release_id
                    for release_id, manifest in releases.items()
                    if manifest.get("status") == "candidate"
                )
                if len(candidate_ids) > 1:
                    raise KnowledgeBaseError(
                        "Multiple candidate manifests exist; set AGENT_RELEASE_ID explicitly",
                        details={"candidate_ids": candidate_ids},
                    )
                if not candidate_ids and releases:
                    raise KnowledgeBaseError(
                        "No current release manifest can be selected; set AGENT_RELEASE_ID explicitly",
                        details={"known_release_ids": sorted(releases)},
                    )
                planning_release_id = candidate_ids[0] if candidate_ids else None

        if released_release_id is not None:
            released_manifest = releases[released_release_id]
            try:
                validate_manifest_integrity(self.project_root, released_manifest)
            except ReleaseIntegrityError as exc:
                raise KnowledgeBaseError(
                    f"Released manifest integrity check failed: {released_release_id}",
                    details=exc.details or {"error": str(exc)},
                ) from exc
            if self.require_trusted_release:
                expected_digest = self.trusted_release_sha256
                if not expected_digest or not re.fullmatch(r"[a-f0-9]{64}", expected_digest):
                    raise KnowledgeBaseError(
                        "Doris runtime requires AGENT_TRUSTED_RELEASE_SHA256 for a released manifest",
                        details={"release_id": released_release_id},
                    )
                actual_digest = release_digests[released_release_id]
                if actual_digest != expected_digest:
                    raise KnowledgeBaseError(
                        "Trusted release manifest SHA-256 mismatch",
                        details={
                            "release_id": released_release_id,
                            "expected": expected_digest,
                            "actual": actual_digest,
                        },
                    )

        planning_manifest = (
            releases.get(planning_release_id) if planning_release_id is not None else None
        )
        if planning_manifest is not None:
            manifest_contracts = planning_manifest.get("contracts", {})
            if not isinstance(manifest_contracts, dict):
                raise KnowledgeBaseError(
                    f"Release {planning_release_id} contracts must be a mapping"
                )
            metrics = _select_manifest_versions(
                metrics_by_version,
                manifest_contracts.get("metrics"),
                kind="metrics",
                release_id=planning_release_id,
            )
            sources = _select_manifest_versions(
                sources_by_version,
                manifest_contracts.get("sources"),
                kind="sources",
                release_id=planning_release_id,
            )
        else:
            metrics = _select_fallback_versions(metrics_by_version, kind="metric")
            sources = _select_fallback_versions(sources_by_version, kind="source")

        validation_path = self.project_root / "rules" / "validations.yaml"
        validation_doc = self._read_yaml_snapshot(validation_path, cached_digests)
        validations = validation_doc.get("validations", {})
        if not isinstance(validations, dict):
            raise KnowledgeBaseError("rules/validations.yaml must contain a validations mapping")

        alias_path = self._runtime_config_path("routing_aliases.yaml")
        alias_doc = (
            self._read_yaml_snapshot(alias_path, cached_digests)
            if alias_path.is_file()
            else {}
        )
        aliases = alias_doc.get("aliases", {})
        if not isinstance(aliases, dict):
            raise KnowledgeBaseError("app/routing_aliases.yaml aliases must be a mapping")

        required_snapshot_paths = {
            metric_paths_by_version[(metric_id, str(metric["version"]))]
            for metric_id, metric in metrics.items()
        }
        required_snapshot_paths.update(
            source_paths_by_version[(source_id, str(source["version"]))]
            for source_id, source in sources.items()
        )
        required_snapshot_paths.update(profile_paths)
        required_snapshot_paths.add(self._relative_project_path(validation_path))
        if alias_path.is_file():
            required_snapshot_paths.add(self._relative_project_path(alias_path))
        capability_path = self._runtime_config_path("template_capabilities.yaml")
        required_snapshot_paths.add(self._relative_project_path(capability_path))
        for metric in metrics.values():
            execution = metric.get("execution", {})
            execution_template = (
                execution.get("template") if isinstance(execution, dict) else None
            )
            template_ref = execution_template or metric.get("template")
            if isinstance(template_ref, str) and template_ref:
                required_snapshot_paths.add(
                    self._relative_project_path((self.project_root / template_ref).resolve())
                )
            required_snapshot_paths.update(
                self._relative_project_path((self.project_root / ref).resolve())
                for ref in metric.get("supporting_templates", [])
            )

        with self._lock:
            self._metrics = metrics
            self._metric_versions = metrics_by_version
            self._sources = sources
            self._source_versions = sources_by_version
            self._profiles = profiles
            self._validations = validations
            self._releases = releases
            self._release_paths = release_paths
            self._release_digests = release_digests
            self._cached_file_digests = cached_digests
            self._required_snapshot_paths = required_snapshot_paths
            self._planning_release_id = planning_release_id
            self._released_release_id = released_release_id
            self._aliases = {
                str(metric_id): [str(term) for term in terms]
                for metric_id, terms in aliases.items()
                if isinstance(terms, list)
            }
            self._signature = self._current_signature()
            self._loaded_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")

    def selected_template_refs(self) -> tuple[str, ...]:
        with self._lock:
            refs: set[str] = set()
            for metric in self._metrics.values():
                execution = metric.get("execution", {})
                execution_template = (
                    execution.get("template") if isinstance(execution, dict) else None
                )
                template_ref = execution_template or metric.get("template")
                if isinstance(template_ref, str) and template_ref:
                    refs.add(template_ref)
                refs.update(metric.get("supporting_templates", []))
            return tuple(sorted(refs))

    def verify_runtime_snapshot_integrity(
        self,
        additional_cached_digests: dict[str, str] | None = None,
    ) -> None:
        """Bind the frozen in-memory runtime bytes to the selected manifest."""
        with self._lock:
            release_id = self._planning_release_id
            manifest = deepcopy(self._releases.get(release_id)) if release_id else None
            manifest_path = self._release_paths.get(release_id) if release_id else None
            loaded_manifest_digest = self._release_digests.get(release_id) if release_id else None
            cached_digests = dict(self._cached_file_digests)
            required_paths = set(self._required_snapshot_paths)
            released_release_id = self._released_release_id

        if manifest is None or manifest_path is None or loaded_manifest_digest is None:
            raise KnowledgeBaseError(
                "Frozen Doris runtime requires a selected release manifest"
            )
        try:
            raw_manifest = manifest_path.read_bytes()
            current_manifest = yaml.safe_load(raw_manifest.decode("utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            raise KnowledgeBaseError(
                f"Unable to revalidate selected release manifest: {manifest_path}"
            ) from exc
        current_manifest_digest = sha256(raw_manifest).hexdigest()
        if (
            not isinstance(current_manifest, dict)
            or current_manifest.get("release_id") != release_id
            or current_manifest.get("status") != manifest.get("status")
            or current_manifest_digest != loaded_manifest_digest
        ):
            raise KnowledgeBaseError(
                "Selected release manifest changed while the runtime snapshot was being built",
                details={"release_id": release_id},
            )

        try:
            validate_manifest_integrity(self.project_root, manifest)
        except ReleaseIntegrityError as exc:
            raise KnowledgeBaseError(
                f"Runtime snapshot integrity check failed: {release_id}",
                details=exc.details or {"error": str(exc)},
            ) from exc

        if self.require_trusted_release and release_id == released_release_id:
            expected_digest = self.trusted_release_sha256
            if not expected_digest or not re.fullmatch(r"[a-f0-9]{64}", expected_digest):
                raise KnowledgeBaseError(
                    "Doris runtime requires AGENT_TRUSTED_RELEASE_SHA256 for a released manifest",
                    details={"release_id": release_id},
                )
            if current_manifest_digest != expected_digest:
                raise KnowledgeBaseError(
                    "Trusted release manifest SHA-256 mismatch",
                    details={
                        "release_id": release_id,
                        "expected": expected_digest,
                        "actual": current_manifest_digest,
                    },
                )

        if additional_cached_digests:
            cached_digests.update(additional_cached_digests)
        integrity = manifest.get("integrity", {})
        declared_files = integrity.get("files", {}) if isinstance(integrity, dict) else {}
        if not isinstance(declared_files, dict):
            raise KnowledgeBaseError(
                "Selected release manifest has no valid protected-file digest mapping",
                details={"release_id": release_id},
            )
        missing_paths = sorted(required_paths - set(cached_digests))
        undeclared_paths = sorted(required_paths - set(declared_files))
        mismatched_paths = sorted(
            path
            for path in required_paths.intersection(cached_digests, declared_files)
            if str(cached_digests[path]).casefold()
            != str(declared_files[path]).casefold()
        )
        if missing_paths or undeclared_paths or mismatched_paths:
            raise KnowledgeBaseError(
                "Frozen runtime bytes do not match the selected release manifest",
                details={
                    "release_id": release_id,
                    "missing_cached_paths": missing_paths,
                    "undeclared_paths": undeclared_paths,
                    "digest_mismatches": mismatched_paths,
                },
            )

    def metric(self, metric_id: str, version: str | None = None) -> dict[str, Any]:
        self.reload_if_changed()
        with self._lock:
            try:
                value = (
                    self._metric_versions[(metric_id, version)]
                    if version is not None
                    else self._metrics[metric_id]
                )
                return deepcopy(value)
            except KeyError as exc:
                raise MetricNotFoundError(
                    f"Unknown metric contract: {metric_id}"
                    + (f"@{version}" if version is not None else ""),
                    details={
                        "known_metric_ids": sorted(self._metrics),
                        "known_versions": sorted(
                            item_version
                            for item_id, item_version in self._metric_versions
                            if item_id == metric_id
                        ),
                    },
                ) from exc

    def metrics(self) -> list[dict[str, Any]]:
        self.reload_if_changed()
        with self._lock:
            return [deepcopy(self._metrics[key]) for key in sorted(self._metrics)]

    def sources(self) -> list[dict[str, Any]]:
        self.reload_if_changed()
        with self._lock:
            return [deepcopy(self._sources[key]) for key in sorted(self._sources)]

    def aliases_for(self, metric_id: str) -> list[str]:
        with self._lock:
            return list(self._aliases.get(metric_id, []))

    def validation(self, validation_id: str) -> dict[str, Any] | None:
        with self._lock:
            value = self._validations.get(validation_id)
            return deepcopy(value) if value is not None else None

    def profile(self, profile_id: str) -> dict[str, Any] | None:
        self.reload_if_changed()
        with self._lock:
            value = self._profiles.get(profile_id)
            return deepcopy(value) if value is not None else None

    def releases(self) -> list[dict[str, Any]]:
        self.reload_if_changed()
        with self._lock:
            return [deepcopy(self._releases[key]) for key in sorted(self._releases)]

    def release(self, release_id: str) -> dict[str, Any] | None:
        self.reload_if_changed()
        with self._lock:
            value = self._releases.get(release_id)
            return deepcopy(value) if value is not None else None

    def planning_manifest(self) -> dict[str, Any] | None:
        """Manifest attached to plans; a candidate is never production-eligible."""
        self.reload_if_changed()
        with self._lock:
            if self._planning_release_id is None:
                return None
            return deepcopy(self._releases[self._planning_release_id])

    def released_manifest(self) -> dict[str, Any] | None:
        """The configured or sole production-released manifest."""
        self.reload_if_changed()
        with self._lock:
            if self._released_release_id is None:
                return None
            return deepcopy(self._releases[self._released_release_id])

    def current_release(self) -> dict[str, Any] | None:
        """Alias for the production manifest used by callers outside the runtime."""
        return self.released_manifest()

    def current_release_manifest(self) -> dict[str, Any] | None:
        return self.released_manifest()

    def release_manifest(
        self,
        release_id: str | None = None,
        *,
        for_plan: bool = False,
    ) -> dict[str, Any] | None:
        """Resolve a manifest explicitly, or the current/plan manifest by policy."""
        if release_id is not None:
            return self.release(release_id)
        return self.planning_manifest() if for_plan else self.released_manifest()

    def plan_release_context(
        self,
        *,
        metric_id: str,
        metric_version: str,
        source_ids: tuple[str, ...] | list[str],
    ) -> tuple[str | None, dict[str, str]]:
        """Return the manifest and pinned source versions represented by a plan."""
        manifest = self.planning_manifest()
        metric_ref = f"{metric_id}@{metric_version}"
        release_id: str | None = None
        manifest_sources: dict[str, str] = {}
        contracts = manifest.get("contracts", {}) if manifest else {}
        if not isinstance(contracts, dict):
            contracts = {}
        metric_in_manifest = bool(manifest and metric_ref in contracts.get("metrics", []))
        if manifest:
            for ref in contracts.get("sources", []):
                if isinstance(ref, str) and "@" in ref:
                    source_id, version = ref.rsplit("@", 1)
                    manifest_sources[source_id] = version

        source_versions: dict[str, str] = {}
        with self._lock:
            for source_id in source_ids:
                source = self._sources.get(source_id)
                if source is not None and source.get("version") is not None:
                    source_versions[source_id] = str(source["version"])
        sources_in_manifest = all(
            manifest_sources.get(source_id) == version
            for source_id, version in source_versions.items()
        ) and len(source_versions) == len(source_ids)
        if manifest and metric_in_manifest and sources_in_manifest:
            release_id = str(manifest["release_id"])
        return release_id, source_versions

    def health(self) -> dict[str, Any]:
        reloaded = self.reload_if_changed()
        with self._lock:
            return {
                "metric_count": len(self._metrics),
                "metric_version_count": len(self._metric_versions),
                "source_count": len(self._sources),
                "source_version_count": len(self._source_versions),
                "profile_count": len(self._profiles),
                "release_count": len(self._releases),
                "planning_release_id": self._planning_release_id,
                "released_release_id": self._released_release_id,
                "reload_on_change": self.reload_on_change,
                "loaded_at": self._loaded_at,
                "reloaded_on_this_check": reloaded,
            }
