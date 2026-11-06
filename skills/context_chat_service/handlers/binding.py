"""Owner-written service binding to one verified immutable Application runtime.

The global service gateway has one service name. Its supervised bootstrap may
run this application's selected immutable code and data without changing Core.
Only the deployment owner writes the binding; no browser endpoint accepts it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

from adaos.domain.artifact_release import ProjectRelease

BINDING = 'internal/context-chat-binding.json'
SCHEMA = 'context_chat.runtime_binding.v1'
DIGEST = re.compile(r'^sha256:[0-9a-f]{64}$')


def verify_binding(value):
    if not isinstance(value,dict) or value.get('schema')!=SCHEMA or value.get('application_id')!='context_chat':
        raise RuntimeError('Invalid Context Chat runtime binding')
    candidate=value.get('candidate_id','')
    digest=value.get('release_digest','')
    if not isinstance(candidate,str) or not re.fullmatch(r'context_chat-[a-z0-9-]+',candidate) or not DIGEST.fullmatch(digest):
        raise RuntimeError('Invalid Context Chat release identity')
    root=Path(value['runtime_root']).resolve()
    source=Path(value['source_root']).resolve()
    env=Path(value['env_path']).resolve()
    if root.name!=candidate or not source.is_relative_to(root) or not env.is_relative_to(root):
        raise RuntimeError('Runtime binding escaped the selected owner')
    if source.name!='context_chat_service' or env.name!='skill_env.json' or env.parent.name!='db':
        raise RuntimeError('Runtime binding does not name the owned service')
    release_file=root/'.adaos/releases'/(digest.split(':')[1]+'.json')
    release=ProjectRelease.from_mapping(json.loads(release_file.read_text(encoding='utf-8'))).seal()
    if release.project_id!='context_chat' or release.release_digest!=digest:
        raise RuntimeError('Runtime binding release differs from its immutable source')
    component=next((p for p in release.components if p.kind=='skill' and p.artifact_id=='context_chat_service'),None)
    if component is None or component.digest!=value.get('component_digest'):
        raise RuntimeError('Runtime binding component differs from its release')
    hashes=value.get('files')
    if not isinstance(hashes,dict) or not 1<=len(hashes)<=500 or not {'handlers/service.py','handlers/binding.py','handlers/openai_account.py','skill.yaml','web/app.js'}.issubset(hashes):
        raise RuntimeError('Runtime binding lacks its verified source inventory')
    for name,checksum in hashes.items():
        relative=PurePosixPath(name)
        if relative.is_absolute() or any(p in ('.','..') for p in relative.parts) or '\\' in name or ':' in name:
            raise RuntimeError('Runtime binding contains an unsafe source path')
        path=source/Path(name)
        if path.is_symlink() or not path.resolve().is_relative_to(source) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=checksum:
            raise RuntimeError('Runtime binding source integrity check failed')
    return {'root':root,'source':source,'env':env,'release_digest':digest,'version':release.version}


def run_bound(data_root):
    if os.getenv('ADAOS_APPLICATION_RELEASE_DIGEST'):
        # Current Core resolves the verified Application channel itself.
        return None
    if os.getenv('CONTEXT_CHAT_PINNED_RELEASE'):
        return None
    path=Path(data_root)/BINDING
    if not path.exists():
        return None
    if path.stat().st_size>262144:
        raise RuntimeError('Context Chat runtime binding is too large')
    binding=verify_binding(json.loads(path.read_text(encoding='utf-8')))
    env={**os.environ,'ADAOS_SKILL_ENV_PATH':str(binding['env']),
         'CONTEXT_CHAT_PINNED_RELEASE':binding['release_digest'],
         'CONTEXT_CHAT_RELEASE_VERSION':binding['version'],
         'PYTHONPATH':str(binding['source'])+os.pathsep+os.environ.get('PYTHONPATH','')}
    child=subprocess.Popen([sys.executable,'-B','-m','handlers.service'],cwd=binding['source'],env=env,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    try:
        return child.wait()
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=10)
