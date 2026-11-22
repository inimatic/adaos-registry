"""Bounded published-catalog checks; never import or execute Application code."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import hashlib
from pathlib import Path, PurePosixPath
import re
import subprocess

DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
VERSION = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:[-+][0-9A-Za-z.-]+)?\Z")
# Published main immediately before mandatory full-plan proof adoption. This is
# an immutable migration boundary, never an arbitrary previous (possibly failed)
# push. Every later identity requires a proof on every qualification attempt.
LEGACY_PUBLISHED_CATALOG_REVISION = "a4f3b955524cd8c971028bc44f466dc9065efb8e"


def safe_path(root: Path, value: str) -> Path:
    relative = PurePosixPath(str(value))
    if (not value or relative.is_absolute() or ".." in relative.parts
            or "\\" in value or ":" in value):
        raise ValueError("unsafe_catalog_path")
    target = root.joinpath(*relative.parts)
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError("catalog_path_escapes_checkout")
    return target


def read_json(path: Path) -> dict:
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("catalog_record_too_large")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("catalog_object_required")
    return value


def canonical_digest(value: dict) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(value, ensure_ascii=False,
        sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def test_proof_path(root: Path, digest: str) -> Path:
    if not DIGEST.fullmatch(str(digest)):
        raise ValueError('catalog_test_proof_release_digest_invalid')
    return root / 'semantic/test-acceptance' / (digest.removeprefix('sha256:') + '.json')


def validate_test_proof(root: Path, *, project_id: str, version: str, release_digest: str,
                        package_digest: str | None = None, source_revision: str | None = None,
                        source_branch: str | None = None) -> dict:
    proof = read_json(test_proof_path(root, release_digest))
    expected = {'schema':'adaos.application.catalog_test_proof.v1', 'status':'passed',
                'project_id':project_id, 'version':version, 'release_digest':release_digest}
    if (any(proof.get(key) != value for key,value in expected.items())
            or canonical_digest({k:v for k,v in proof.items() if k != 'proof_digest'}) != proof.get('proof_digest')):
        raise ValueError('catalog_test_proof_identity_mismatch')
    packages = proof.get('package_digests')
    if (not isinstance(packages, list) or not packages or len(packages) != len(set(packages))
            or any(not DIGEST.fullmatch(str(item)) for item in packages)
            or (package_digest is not None and package_digest not in packages)
            or (source_revision is not None and proof.get('source_revision') != source_revision)
            or (source_branch is not None and proof.get('source_branch') != source_branch)
            or not proof.get('source_branch')
            or not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', str(proof.get('source_revision') or ''))
            or any(not DIGEST.fullmatch(str(proof.get(key) or '')) for key in
                   ('source_snapshot_digest','environment_digest','candidate_acceptance_digest','test_receipt_digest'))):
        raise ValueError('catalog_test_proof_release_mismatch')
    plan = proof.get('test_plan') or {}
    suites = plan.get('suites')
    rows = proof.get('packages')
    if (plan.get('schema') != 'adaos.project.test_plan.v1'
            or plan.get('complete_suite') is not True or not isinstance(suites,list) or not suites
            or any(not isinstance(ref,str) for ref in suites) or len(suites) != len(set(suites))
            or not isinstance(rows,list) or any(not isinstance(row,dict) for row in rows)
            or sorted(suites) != sorted(row.get('ref','') for row in rows)
            or any(not re.fullmatch(r'[0-9a-f]{64}',str(row.get('log_sha256') or ''))
                   or not isinstance(row.get('statuses'),list) or not row['statuses']
                   or any(status != 'passed' for status in row['statuses']) for row in rows)):
        raise ValueError('catalog_test_proof_plan_incomplete')
    return proof


def retain_test_proof(root: Path, proof: dict) -> str:
    path = test_proof_path(root, proof['release_digest'])
    expected = {key:proof[key] for key in ('project_id','version','release_digest','source_branch','source_revision')}
    if path.is_file():
        existing = validate_test_proof(root, **expected)
        if existing['package_digests'] != proof['package_digests']:
            raise ValueError('catalog_test_proof_immutable_identity_conflict')
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = root / '.tmp/registry-catalog-validation' / path.name
        temporary.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(proof, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        temporary.replace(path)
        try:
            validate_test_proof(root, **expected)
        except (ValueError, TypeError):
            path.unlink()
            raise
    return path.relative_to(root).as_posix()


def _baseline_identities(root: Path, base_ref: str | None) -> set[tuple]:
    if base_ref is None:
        return set()
    if not re.fullmatch(r'(?:HEAD\^?|[0-9a-f]{40}|[0-9a-f]{64})', base_ref):
        raise ValueError('catalog_base_revision_invalid')
    def historical(path, *, required=False):
        result = subprocess.run(['git','-C',str(root),'show',base_ref+':'+path],
            capture_output=True, timeout=10, check=False)
        if result.returncode:
            if required:
                raise ValueError('catalog_base_revision_unavailable')
            return {}
        if len(result.stdout) > 2*1024*1024:
            raise ValueError('catalog_base_record_too_large')
        return json.loads(result.stdout)
    catalog = historical('registry.json', required=True)
    bindings = historical('semantic/index.json').get('application_releases', {})
    identities = set()
    for kind in ('projects','skills','scenarios'):
        for row in catalog.get(kind,[]):
            for pointer in (row.get('channels') or {}).values():
                project = str(pointer.get('release','')).split('@',1)[0]
                binding = bindings.get('project:'+project+'@'+str(pointer.get('release_digest')), {})
                identities.add(('channel', project, pointer.get('version'),
                    pointer.get('release_digest'),pointer.get('package_digest'),pointer.get('source_revision'),
                    pointer.get('source_branch', 'main'), binding.get('projection_digest')))
    # Prerelease catalog entries do not replace the stable source manifest.
    for row in historical('semantic/catalog/index.json').get('releases',{}).values():
        identities.add(('public',row.get('version'),row.get('release_digest'),row.get('projection_digest')))
    return identities


def published_projects(root: Path) -> dict:
    path = root / "semantic/catalog/index.json"
    if not path.is_file():
        return {}
    index = read_json(path)
    result = {}
    for app_id, app in index.get("applications", {}).items():
        digest = app.get("stable_release_digest")
        if not digest:
            continue
        entry = index.get("releases", {}).get(app.get("release_key"))
        if not isinstance(entry, dict) or entry.get("release_digest") != digest:
            raise ValueError("public_application_release_binding_missing:" + app_id)
        record = read_json(safe_path(root, entry["path"]))
        if (canonical_digest({k: v for k, v in record.items() if k != "projection_digest"})
                != record.get("projection_digest") or record.get("projection_digest") != entry.get("projection_digest")):
            raise ValueError("public_application_projection_digest_mismatch:" + app_id)
        release = record.get("release") or {}
        project = release.get("project_release") or {}
        if (release.get("application_id") != app_id or release.get("release_digest") != digest
                or project.get("release_digest") != digest or (record.get("channels") or {}).get("stable") != digest):
            raise ValueError("public_application_release_identity_mismatch:" + app_id)
        identifier = project["project_id"]
        components = project["components"]
        primary = next((m["ref"] for m in (project.get("composition_lock") or {}).get("members", [])
                        if m.get("role") == "primary"), None)
        package = next((c for c in components if c["kind"] + ":" + c["artifact_id"] == primary), components[0])
        result[identifier] = {"version": project["version"], "release": identifier + "@" + project["version"],
            "release_digest": digest, "source_revision": project["source_ref"]["revision"],
            "package_digest": package["digest"]}
    return result


def validate(root: Path, *, retire_unavailable: bool = False, synchronize_published: bool = False,
             base_ref: str | None = None) -> dict:
    root = root.resolve()
    catalog = read_json(root / "registry.json")
    original_digest = canonical_digest(catalog)
    if base_ref is not None and not re.fullmatch(r'(?:HEAD\^?|[0-9a-f]{40}|[0-9a-f]{64})', base_ref):
        raise ValueError('catalog_base_revision_invalid')
    baseline = _baseline_identities(root, LEGACY_PUBLISHED_CATALOG_REVISION)
    try:
        tracked = subprocess.run(["git", "-C", str(root), "ls-files", "-v", "--",
            "skills/*/skill.yaml", "scenarios/*/scenario.yaml", "projects/*/project.yaml"],
            capture_output=True, text=True, encoding="utf-8", timeout=10)
        sparse_manifests = {line[2:] for line in tracked.stdout.splitlines() if line.startswith("S ")}
    except (OSError, subprocess.TimeoutExpired):
        sparse_manifests = set()
    if catalog.get("version") != 2:
        raise ValueError("registry_version_2_required")
    semantic_path = root / "semantic/index.json"
    semantic = read_json(semantic_path).get("application_releases", {}) if semantic_path.is_file() else {}
    errors, retired, counts = [], [], {}
    published = published_projects(root)
    public_index = root / 'semantic/catalog/index.json'
    if public_index.is_file():
        for entry in read_json(public_index).get('releases',{}).values():
            record = read_json(safe_path(root,entry['path']))
            project = (record.get('release') or {}).get('project_release') or {}
            digest = entry.get('release_digest')
            if (canonical_digest({k:v for k,v in record.items() if k != 'projection_digest'}) != record.get('projection_digest')
                    or record.get('projection_digest') != entry.get('projection_digest')):
                errors.append({'entry':str(entry.get('application_id') or digest),
                    'code':'public_application_projection_digest_mismatch'})
                continue
            if (project.get('release_digest') != digest
                    or (record.get('release') or {}).get('release_digest') != digest
                    or project.get('version') != entry.get('version')
                    or (record.get('release') or {}).get('application_id') != entry.get('application_id')):
                errors.append({'entry':str(entry.get('application_id') or digest),
                    'code':'public_application_release_identity_mismatch'})
                continue
            historical = ('public',project.get('version'),digest,entry.get('projection_digest')) in baseline
            if test_proof_path(root,digest).is_file() or not historical:
                try:
                    proof = validate_test_proof(root, project_id=project['project_id'],version=project['version'],
                        release_digest=digest,source_revision=project['source_ref']['revision'],
                        source_branch=project.get('source_branch','main'))
                    if sorted(proof['package_digests']) != sorted(row['digest'] for row in project['components']):
                        raise ValueError('catalog_test_proof_composition_mismatch')
                except (OSError,ValueError,KeyError,TypeError) as exc:
                    errors.append({'entry':str(entry.get('application_id') or digest),
                        'code':'public_application_tests_unverified:'+type(exc).__name__})
    if synchronize_published:
        existing = {e.get("id"): e for e in catalog.get("projects", [])}
        for identifier, pointer in published.items():
            entry = existing.get(identifier)
            if entry is None:
                source = "projects/" + identifier
                entry = {"kind": "project", "id": identifier, "name": identifier, "path": source,
                    "manifest": source + "/project.yaml", "source": {"path": source, "manifest": source + "/project.yaml"},
                    "install": {"kind": "project", "id": identifier, "name": identifier}}
                catalog.setdefault("projects", []).append(entry)
            entry["version"] = pointer["version"]
            entry.setdefault("channels", {})["stable"] = pointer
    indexed = {e.get("id"): e for e in catalog.get("projects", [])}
    for identifier, pointer in published.items():
        existing = indexed.get(identifier)
        if (existing is None or existing.get("version") != pointer["version"]
                or (existing.get("channels") or {}).get("stable", {}).get("release_digest") != pointer["release_digest"]):
            errors.append({"entry": "projects:" + identifier, "code": "published_catalog_projection_stale"})
    for collection, kind in (("skills", "skill"), ("scenarios", "scenario"), ("projects", "project")):
        rows = catalog.get(collection)
        if not isinstance(rows, list) or len(rows) > 10000:
            raise ValueError("invalid_registry_collection:" + collection)
        seen, retained = set(), []
        for entry in rows:
            if not isinstance(entry, dict):
                raise ValueError("invalid_registry_entry")
            name = str(entry.get("name") or entry.get("id") or "")
            identity = collection + ":" + name
            faults = []
            if not name or name in seen:
                faults.append("duplicate_or_empty_identity")
            seen.add(name)
            if entry.get("kind") != kind or not VERSION.fullmatch(str(entry.get("version") or "")):
                faults.append("kind_or_version_invalid")
            source = entry.get("source") or {}
            source_path = str(entry.get("path") or source.get("path") or "")
            manifest_path = str(entry.get("manifest") or source.get("manifest") or "")
            manifest = safe_path(root, manifest_path)
            directory = safe_path(root, source_path)
            if (not source_path.startswith(collection + "/") or manifest.parent != directory
                    or manifest.name != kind + ".yaml"):
                faults.append("manifest_path_mismatch")
            channels = entry.get("channels") or {}
            if not isinstance(channels, dict):
                faults.append("channels_object_required")
                channels = {}
            has_retained_release = False
            for channel, pointer in channels.items():
                if not isinstance(pointer, dict):
                    faults.append("invalid_channel:" + channel)
                    continue
                if (not VERSION.fullmatch(str(pointer.get("version") or ""))
                        or any(not DIGEST.fullmatch(str(pointer.get(field) or ""))
                               for field in ("release_digest", "package_digest"))):
                    faults.append("invalid_channel_identity:" + channel)
                    continue
                project = str(pointer.get("release") or "").split("@", 1)[0]
                key = "project:" + project + "@" + pointer["release_digest"]
                binding = semantic.get(key)
                if not isinstance(binding, dict):
                    faults.append("published_release_binding_missing:" + channel)
                    continue
                record = read_json(safe_path(root, str(binding.get("path") or "")))
                if (record.get("project_id") != project
                        or record.get("project_release_digest") != pointer["release_digest"]
                        or binding.get("project_release_digest") != pointer["release_digest"]
                        or (kind == "project" and record.get("version") != pointer["version"])
                        or record.get("projection_digest") != binding.get("projection_digest")
                        or canonical_digest({k: v for k, v in record.items() if k != "projection_digest"})
                            != record.get("projection_digest")):
                    faults.append("published_release_binding_mismatch:" + channel)
                else:
                    has_retained_release = True
                    release_identity = ('channel',project,pointer['version'],pointer['release_digest'],
                                pointer['package_digest'],pointer.get('source_revision'),
                                pointer.get('source_branch','main'), binding.get('projection_digest'))
                    if test_proof_path(root,pointer['release_digest']).is_file() or release_identity not in baseline:
                        try:
                            validate_test_proof(root,project_id=project,version=pointer['version'],
                                release_digest=pointer['release_digest'],package_digest=pointer['package_digest'],
                                source_revision=pointer.get('source_revision'))
                        except (OSError,ValueError,KeyError,TypeError):
                            faults.append('published_release_tests_unverified:'+channel)
            if not manifest.is_file() and manifest_path not in sparse_manifests and not has_retained_release:
                if retire_unavailable and all(f.startswith("published_release_binding_missing:") for f in faults):
                    retired.append({"collection": collection, "entry": entry,
                                    "reason": "source_and_verified_distribution_unavailable"})
                    continue
                faults.append("source_and_verified_distribution_unavailable")
            if kind == "project" and channels.get("stable", {}).get("version") not in {None, entry.get("version")}:
                faults.append("project_index_differs_from_stable_version")
            errors.extend({"entry": identity, "code": fault} for fault in faults)
            retained.append(entry)
        counts[collection] = len(retained)
        catalog[collection] = retained
    if ((synchronize_published or (retire_unavailable and retired)) and not errors
            and canonical_digest(catalog) != original_digest):
        catalog.setdefault("retired_entries", []).extend(retired)
        catalog["updated_at"] = datetime.now(timezone.utc).isoformat()
        temporary = root / ".tmp" / "registry-catalog-validation" / "registry.json"
        temporary.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(root / "registry.json")
    return {"ok": not errors, "counts": counts, "errors": errors, "retired_count": len(retired)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--retire-unavailable", action="store_true")
    parser.add_argument("--synchronize-published", action="store_true")
    parser.add_argument('--base-ref', help='Previous source revision (does not expand the fixed legacy allowance)')
    parser.add_argument('--legacy-baseline', action='store_true', help='Print the immutable pre-enforcement catalog revision')
    args = parser.parse_args()
    if args.legacy_baseline:
        print(LEGACY_PUBLISHED_CATALOG_REVISION)
        return 0
    result = validate(args.root, retire_unavailable=args.retire_unavailable,
        synchronize_published=args.synchronize_published, base_ref=args.base_ref)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
