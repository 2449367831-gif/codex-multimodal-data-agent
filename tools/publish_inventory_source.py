"""Prepare the explicitly approved 2026-10-10 inventory-source release.

Only the enumerated inventory contracts, their source declarations and templates
are imported. Activation remains a separate final deployment operation.
"""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import shutil
import sys
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from app.knowledge import KnowledgeBase
from app.knowledge_activation import active_snapshot
from app.release_integrity import build_manifest_integrity

METRICS = (
    "inventory.current_components.yaml", "inventory.available_realtime.v2.yaml",
    "inventory.finished_goods_realtime.v3.yaml", "inventory.turnover_7d_realtime.v4.yaml",
    "inventory.turnover_finished_goods_realtime.v2.yaml",
)


def prepare(root: Path, release_id: str, *, metric_names=METRICS, supporting_files=(), profile_overrides=None) -> Path:
    import re
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{1,70}", release_id):
        raise ValueError("invalid release id")
    base, _ = active_snapshot(root)
    old = KnowledgeBase(base)
    manifest = deepcopy(old.released_manifest())
    if not manifest or manifest.get("approval", {}).get("state") != "approved":
        raise ValueError("an approved base release is required")
    changes = [yaml.safe_load((root / "metrics" / name).read_text(encoding="utf-8")) for name in metric_names]
    from jsonschema import Draft202012Validator
    import json
    validator = Draft202012Validator(json.loads((root / "schemas/metric-contract.schema.json").read_text(encoding="utf-8")))
    for metric in changes:
        validator.validate(metric)
        if metric["status"] != "active" or metric["approval"]["state"] != "approved":
            raise ValueError("inventory contract must be explicitly approved")
        previous = next((m for m in old.metrics() if m['id'] == metric['id']), None)
        if previous and previous['id'] + '@' + previous['version'] not in metric['validity']['supersedes']:
            raise ValueError("missing superseded inventory contract")
    target = root / "var/knowledge-releases" / release_id
    if target.exists():
        raise ValueError("immutable release already exists")
    shutil.copytree(base, target, ignore=lambda directory, names: {"releases"} if Path(directory).resolve() == base.resolve() else set())
    (target / "releases").mkdir()
    refs = {m['id']: m['version'] for m in old.metrics()}
    source_refs = dict(ref.rsplit('@', 1) for ref in manifest['contracts']['sources'])
    for name, metric in zip(metric_names, changes):
        refs[metric['id']] = metric['version']
        shutil.copy2(root / "metrics" / name, target / "metrics" / name)
        template = metric.get('template')
        if template:
            shutil.copy2(root / template, target / template)
        for supporting in metric.get('supporting_templates', []):
            shutil.copy2(root / supporting, target / supporting)
        for source_id in metric['sources']['required']:
            # Already selected sources keep the base snapshot's declarations.
            if source_id in source_refs:
                continue
            candidates = list((root / 'sources/tables').glob('*.yaml'))
            path = next(p for p in candidates if yaml.safe_load(p.read_text(encoding='utf-8'))['id'] == source_id)
            source = yaml.safe_load(path.read_text(encoding='utf-8'))
            source_refs[source_id] = source['version']
            shutil.copy2(path, target / 'sources/tables' / path.name)
    for path in (target / 'metrics').glob('*.yaml'):
        value = yaml.safe_load(path.read_text(encoding='utf-8'))
        if value['id'] in {m['id'] for m in changes} and value['version'] != refs[value['id']]:
            value['status'] = 'deprecated'
            value.setdefault('validity', {})['effective_to'] = '2026-10-10T00:00:00+08:00'
            path.write_text(yaml.safe_dump(value, allow_unicode=True, sort_keys=False), encoding='utf-8')
    shutil.copy2(root / 'sources/ywzt_ods/inventory.yaml', target / 'sources/ywzt_ods/inventory.yaml')
    for relative in supporting_files:
        source = (root / relative).resolve()
        source.relative_to(root.resolve())
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    capabilities_path = target / 'app/template_capabilities.yaml'
    capabilities = yaml.safe_load(capabilities_path.read_text(encoding='utf-8'))
    for metric in changes:
        capabilities['metrics'][metric['id']+'@'+metric['version']] = (
            {'executable': True, 'output_grains': ['warehouse_sku']} if metric.get('template') else
            {'executable': True, 'mode': 'composite', 'output_grains': ['center_and_subwarehouse']} if metric['id']=='inventory.turnover_7d_realtime' else
            {'executable': False, 'reason': 'Executed from the governed inventory and sales components.'}
        )
    capabilities_path.write_text(yaml.safe_dump(capabilities,allow_unicode=True,sort_keys=False),encoding='utf-8')
    # Keep the reviewed warehouse scope, but replace its retired source advice.
    profile_path = target / 'profiles/hjxh_operating_centers.yaml'
    profile = yaml.safe_load(profile_path.read_text(encoding='utf-8'))
    profile['filters']['inventory'] = {'source_metric': 'inventory.current_components@1.0.0', 'default_formula': 'SUM(available_quantity + in_transit_quantity)', 'join_key': 'cargo_owner_id + wms_code + sku_code', 'negative_policy': 'Clamp available at the three-key grain exactly as the approved SQL.'}
    profile['freshness']['inventory'] = 'Catalog ingestion freshness for component sources; empty stock input blocks publication.'
    for section, values in (profile_overrides or {}).items():
        profile.setdefault(section, {}).update(values)
    profile_path.write_text(yaml.safe_dump(profile,allow_unicode=True,sort_keys=False),encoding='utf-8')
    now = datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()
    previous_release = manifest['release_id']
    manifest.update(release_id=release_id, created_at=now, released_at=now,
                    approval={'state':'approved','approved_by':'current_project_requester','approved_at':now},
                    rollback={'previous_release_id':previous_release})
    manifest['contracts']['metrics'] = sorted(k+'@'+v for k,v in refs.items())
    manifest['contracts']['sources'] = sorted(k+'@'+v for k,v in source_refs.items())
    manifest['integrity'] = build_manifest_integrity(target,manifest)
    output = target / 'releases' / (release_id+'.yaml')
    output.write_text(yaml.safe_dump(manifest,allow_unicode=True,sort_keys=False),encoding='utf-8')
    KnowledgeBase(target,release_id=release_id).verify_runtime_snapshot_integrity(manifest['integrity']['files'])
    return output


if __name__ == '__main__':
    print(prepare(ROOT, sys.argv[1]))
