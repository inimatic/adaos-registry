"""Owner-scoped drain protocol for the separate service process."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from pathlib import Path

import httpx
from adaos.sdk.core.decorators import tool

PREFIX = '/api/services/context_chat_service/ui'
MARKER = 'internal/context-chat-draining.lock'
INSTANCE = 'internal/context-chat-instance.lock'


def owner(root):
    return hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()


def atomic_json(path, data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    staged=path.with_suffix('.new')
    staged.write_text(json.dumps(data),encoding='utf-8')
    os.replace(staged,path)


def process_alive(pid):
    if os.name == 'nt':
        import ctypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.restype=ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes=[ctypes.c_void_p,ctypes.c_uint32]
        kernel.CloseHandle.argtypes=[ctypes.c_void_p]
        handle=kernel.OpenProcess(0x00100000,False,pid)
        if not handle:
            if ctypes.get_last_error()==87: return False
            raise RuntimeError('Cannot verify the service process state')
        try: return kernel.WaitForSingleObject(handle,0)!=0
        finally: kernel.CloseHandle(handle)
    try:
        os.kill(pid,0)
        return True
    except ProcessLookupError:
        return False


def drain_root(root, *, http=None, alive=process_alive, timeout=15, port=19084):
    root=Path(root); identity=owner(root)
    nonce=secrets.token_urlsafe(32)
    atomic_json(root/MARKER,{'owner':identity,'nonce':nonce})
    record=root/INSTANCE
    if not record.exists():
        return {'ok':True,'drained':True,'writers_stopped':True,'owner':identity,'reason':'service_not_started'}
    instance=json.loads(record.read_text(encoding='utf-8'))
    pid=instance.get('pid')
    if instance.get('owner')!=identity or not isinstance(pid,int) or pid<=0:
        raise RuntimeError('Service instance ownership is invalid')
    if not alive(pid):
        return {'ok':True,'drained':True,'writers_stopped':True,'owner':identity,'reason':'process_stopped'}
    client=http or httpx.Client(trust_env=False,timeout=5)
    try:
        url=f'http://127.0.0.1:{port}'+PREFIX
        response=client.get(url+'/health');response.raise_for_status()
        health=response.json()
        if health.get('owner')!=identity or health.get('pid')!=pid:
            raise RuntimeError('Refusing to drain a different runtime owner')
        response=client.post(url+'/internal/drain',headers={'X-ContextChat-Owner':nonce})
        response.raise_for_status()
        deadline=time.monotonic()+timeout
        while alive(pid) and time.monotonic()<deadline:
            time.sleep(.05)
        if alive(pid):
            raise RuntimeError('Service process did not drain before the deadline')
    finally:
        if http is None: client.close()
    return {'ok':True,'drained':True,'writers_stopped':True,'owner':identity}


@tool(summary='Drain only this Context Chat service before data cutover.', side_effects='local_write')
def drain_runtime(**_):
    from adaos.sdk.data.skill_env import skill_data_root_path
    return drain_root(skill_data_root_path())


@tool(summary='Verify owned storage and resume this Context Chat runtime.', side_effects='local_write')
def rehydrate_runtime(**_):
    from adaos.sdk.data.skill_env import skill_data_root
    from adaos.sdk.data.lifecycle import ensure_database
    root=skill_data_root()
    # A verified gateway binding owns no application database. Its selected
    # trial has already gone through the fenced data transition and migration.
    from .binding import BINDING, verify_binding
    if (root/BINDING).exists():
        verify_binding(json.loads((root/BINDING).read_text(encoding='utf-8')))
    else:
        ensure_database('db/context_chat.db')
    (root/MARKER).unlink(missing_ok=True)
    return {'ok':True,'rehydrated':True,'owner':owner(root)}
