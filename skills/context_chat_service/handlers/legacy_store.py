"""Project-scoped local storage. Uploaded documents stay in the runtime database."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from adaos.domain.relational_storage import RelationalMigration


SCHEMA = [
    "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '', memory TEXT NOT NULL DEFAULT '{}', memory_revision INTEGER NOT NULL DEFAULT 0, context_revision INTEGER NOT NULL DEFAULT 0, memory_status TEXT NOT NULL DEFAULT 'empty', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS sources (id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE, title TEXT NOT NULL, kind TEXT NOT NULL, content TEXT NOT NULL, included INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE, role TEXT NOT NULL, content TEXT NOT NULL, model TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'completed', attachments TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE, kind TEXT NOT NULL, status TEXT NOT NULL, message_id TEXT, error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL)",
]

BETA_SCHEMA = [
    'ALTER TABLE runs ADD COLUMN request_key TEXT',
    'ALTER TABLE runs ADD COLUMN user_message_id TEXT',
    'CREATE UNIQUE INDEX IF NOT EXISTS run_request_key ON runs(project_id,request_key) WHERE request_key IS NOT NULL',
    'CREATE TABLE IF NOT EXISTS audit_events (id TEXT PRIMARY KEY, action TEXT NOT NULL, status INTEGER NOT NULL, created_at TEXT NOT NULL)',
]

TOOLS_SCHEMA = [
    'CREATE TABLE message_metadata (message_id TEXT PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE, web_search INTEGER NOT NULL DEFAULT 0, citations TEXT NOT NULL DEFAULT \'[]\')',
    'CREATE INDEX messages_project ON messages(project_id)',
]
MIGRATIONS = ((1, 'context_chat_initial', SCHEMA), (2, 'context_chat_beta_turns', BETA_SCHEMA), (3, 'context_chat_tools', TOOLS_SCHEMA))


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.path = self.root / 'db' / 'context_chat.db'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS adaos_schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL, applied_at TEXT NOT NULL, staged INTEGER NOT NULL)')
            for version, name, statements in MIGRATIONS:
                digest = RelationalMigration(version=version, name=name, statements=tuple(statements), idempotent=version == 1, dialects=('sqlite',)).checksum
                old = db.execute('SELECT checksum FROM adaos_schema_migrations WHERE version=?', (version,)).fetchone()
                if old and old['checksum'] != digest:
                    raise RuntimeError('Context Chat storage migration checksum changed')
                if not old:
                    for statement in statements:
                        db.execute(statement)
                    db.execute('INSERT INTO adaos_schema_migrations VALUES (?,?,?,?,0)', (version, name, digest, now()))
            db.execute("UPDATE messages SET status='interrupted' WHERE status='running'")
            db.execute("UPDATE runs SET status='interrupted', error='Сервис был перезапущен. Повторите запрос.' WHERE status IN ('queued','running')")
            db.execute("UPDATE projects SET memory_status='stale' WHERE memory_status='running'")

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.create_function('casefold',1,lambda value:str(value or '').casefold(),deterministic=True)
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def setting(self, key, default=None):
        with self.db() as db:
            row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return json.loads(row['value']) if row else default

    def set_setting(self, key, value):
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, json.dumps(value)))

    def audit(self, action, status):
        """Record the route template and result, never a body, query, or token."""
        with self.db() as db:
            db.execute('INSERT INTO audit_events VALUES (?,?,?,?)', (uuid4().hex, action, status, now()))
            db.execute('DELETE FROM audit_events WHERE id IN (SELECT id FROM audit_events ORDER BY created_at DESC LIMIT -1 OFFSET 2000)')

    def project(self, project_id):
        with self.db() as db:
            row = db.execute('SELECT * FROM projects WHERE id=?', (project_id,)).fetchone()
        if not row:
            raise KeyError('Проект не найден')
        result = dict(row)
        result['memory'] = json.loads(result['memory'])
        return result

    def projects(self):
        with self.db() as db:
            return [dict(row) for row in db.execute('SELECT id,title,description,model,memory_status,created_at,updated_at FROM projects ORDER BY updated_at DESC,id')]

    def create_project(self, title, description='', context=''):
        identity = uuid4().hex
        stamp = now()
        with self.db() as db:
            db.execute('INSERT INTO projects (id,title,description,created_at,updated_at) VALUES (?,?,?,?,?)', (identity, title, description, stamp, stamp))
        if context.strip():
            self.add_source(identity, 'Начальный контекст', 'text', context)
        return self.project(identity)

    def update_project(self, project_id, **values):
        self.project(project_id)
        allowed = {key: value for key, value in values.items() if key in ('title', 'description', 'model')}
        if allowed:
            allowed['updated_at'] = now()
            with self.db() as db:
                db.execute('UPDATE projects SET ' + ','.join(key+'=?' for key in allowed) + ' WHERE id=?', (*allowed.values(), project_id))
        return self.project(project_id)

    def delete_project(self, project_id):
        self.project(project_id)
        with self.db() as db:
            db.execute('DELETE FROM projects WHERE id=?', (project_id,))

    def sources(self, project_id, include_content=False):
        self.project(project_id)
        with self.db() as db:
            rows = [dict(row) for row in db.execute('SELECT * FROM sources WHERE project_id=? ORDER BY created_at,id', (project_id,))]
        for row in rows:
            row['characters'] = len(row['content'])
            row['included'] = bool(row['included'])
            if not include_content:
                row.pop('content')
        return rows

    def invalidate_memory(self, db, project_id, clear=True):
        memory = ",memory='{}'" if clear else ''
        db.execute("UPDATE projects SET context_revision=context_revision+1,memory_revision=memory_revision+1,memory_status='stale',updated_at=?"+memory+" WHERE id=?", (now(), project_id))

    def add_source(self, project_id, title, kind, content):
        self.project(project_id)
        identity = uuid4().hex
        with self.db() as db:
            db.execute('INSERT INTO sources (id,project_id,title,kind,content,created_at) VALUES (?,?,?,?,?,?)', (identity, project_id, title, kind, content, now()))
            self.invalidate_memory(db, project_id, clear=False)
        return identity

    def change_source(self, project_id, source_id, included=None, delete=False):
        self.project(project_id)
        with self.db() as db:
            row = db.execute('SELECT id FROM sources WHERE id=? AND project_id=?', (source_id, project_id)).fetchone()
            if not row:
                raise KeyError('Источник не найден в этом проекте')
            if delete:
                db.execute('DELETE FROM sources WHERE id=? AND project_id=?', (source_id, project_id))
            else:
                db.execute('UPDATE sources SET included=? WHERE id=? AND project_id=?', (int(bool(included)), source_id, project_id))
            self.invalidate_memory(db, project_id)

    def save_memory(self, project_id, memory, expected_revision, context_revision=None):
        payload = json.dumps(memory, ensure_ascii=False)
        with self.db() as db:
            query = 'UPDATE projects SET memory=?,memory_revision=memory_revision+1,memory_status=?,updated_at=? WHERE id=? AND memory_revision=?'
            args = [payload, 'ready', now(), project_id, expected_revision]
            if context_revision is not None:
                query += ' AND context_revision=?'
                args.append(context_revision)
            if db.execute(query, args).rowcount != 1:
                raise ValueError('Память или источники изменились. Обновите страницу и повторите действие.')

    def messages(self, project_id):
        self.project(project_id)
        with self.db() as db:
            rows = [dict(row) for row in db.execute('SELECT * FROM messages WHERE project_id=? ORDER BY created_at,rowid', (project_id,))]
        for row in rows:
            row['attachments'] = json.loads(row['attachments'])
        return rows

    def recent_context(self, project_id, budget, skip_message=None):
        """Read a bounded suffix; the complete transcript remains in storage."""
        rows, used = [], 0
        with self.db() as db:
            for row in db.execute("SELECT id,role,content FROM messages WHERE project_id=? AND status='completed' AND content!='' AND id!=? ORDER BY rowid DESC LIMIT 200", (project_id, skip_message or '')):
                if used + len(row['content']) > budget:
                    break
                rows.append({'role': row['role'], 'content': row['content']})
                used += len(row['content'])
        rows.reverse()
        while rows and rows[0]['role'] != 'user':
            rows.pop(0)
        return rows

    def message_tools(self, message_id, web_search=None, citations=None):
        with self.db() as db:
            if web_search is not None:
                db.execute('INSERT INTO message_metadata(message_id,web_search) VALUES (?,?) ON CONFLICT(message_id) DO UPDATE SET web_search=excluded.web_search,citations=\'[]\'', (message_id, int(web_search)))
            if citations is not None:
                db.execute('UPDATE message_metadata SET citations=? WHERE message_id=?', (json.dumps(citations, ensure_ascii=False), message_id))

    def tools_for(self, message_id):
        with self.db() as db:
            row = db.execute('SELECT web_search FROM message_metadata WHERE message_id=?', (message_id,)).fetchone()
        return bool(row and row['web_search'])

    def add_message(self, project_id, role, content, model='', status='completed', attachments=()):
        self.project(project_id)
        identity = uuid4().hex
        with self.db() as db:
            db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?,?,?)', (identity, project_id, role, content, model, status, json.dumps(list(attachments)), now()))
        return identity

    def message_progress(self, identity, content, status):
        with self.db() as db:
            db.execute('UPDATE messages SET content=?,status=? WHERE id=?', (content, status, identity))

    def start_run(self, project_id, kind, message_id=None):
        self.project(project_id)
        identity = uuid4().hex
        with self.db() as db:
            active = db.execute("SELECT id FROM runs WHERE project_id=? AND status IN ('queued','running')", (project_id,)).fetchone()
            if active:
                raise ValueError('В проекте уже выполняется запрос')
            db.execute('INSERT INTO runs (id,project_id,kind,status,message_id,error,created_at) VALUES (?,?,?,?,?,?,?)', (identity, project_id, kind, 'running', message_id, '', now()))
        return identity

    def request_run(self, project_id, request_key):
        if not request_key:
            return None
        with self.db() as db:
            row = db.execute('SELECT * FROM runs WHERE project_id=? AND request_key=?', (project_id, request_key)).fetchone()
        return dict(row) if row else None

    def run(self, project_id, run_id):
        with self.db() as db:
            row = db.execute('SELECT * FROM runs WHERE project_id=? AND id=?', (project_id, run_id)).fetchone()
        if not row:
            raise KeyError('Запрос не найден в этом проекте')
        return dict(row)

    def reserve_turn(self, project_id, kind, text, model, attachments, request_key=None, retry=None):
        """Admit one turn and both messages in the same SQLite transaction."""
        identity, user_id, message_id = uuid4().hex, None, None
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM runs WHERE project_id=? AND status IN ('queued','running')", (project_id,)).fetchone():
                raise ValueError('В проекте уже выполняется запрос')
            if kind == 'chat':
                user_id = retry['user_message_id'] if retry else uuid4().hex
                message_id = retry['message_id'] if retry else uuid4().hex
                if retry:
                    db.execute("UPDATE messages SET content='',status='running' WHERE id=? AND project_id=?", (message_id, project_id))
                else:
                    db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?,?,?)', (user_id, project_id, 'user', text, model, 'completed', json.dumps(list(attachments)), now()))
                    db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?,?,?)', (message_id, project_id, 'assistant', '', model, 'running', '[]', now()))
            db.execute('INSERT INTO runs (id,project_id,kind,status,message_id,error,created_at,request_key,user_message_id) VALUES (?,?,?,?,?,?,?,?,?)',
                       (identity, project_id, kind, 'running', message_id, '', now(), request_key, user_id))
        return identity, message_id

    def run_progress(self, run_id, message_id, content, status):
        with self.db() as db:
            db.execute("UPDATE messages SET content=?,status=? WHERE id=? AND EXISTS(SELECT 1 FROM runs WHERE id=? AND status='running')", (content, status, message_id, run_id))

    def memory_progress(self, run_id, project_id, status):
        with self.db() as db:
            db.execute("UPDATE projects SET memory_status=? WHERE id=? AND EXISTS(SELECT 1 FROM runs WHERE id=? AND status='running')", (status, project_id, run_id))

    def finish_run(self, identity, status, error=''):
        with self.db() as db:
            db.execute("UPDATE runs SET status=?,error=? WHERE id=? AND status IN ('queued','running')", (status, error, identity))

    def runs(self, project_id):
        self.project(project_id)
        with self.db() as db:
            return [dict(row) for row in db.execute('SELECT * FROM runs WHERE project_id=? ORDER BY created_at DESC LIMIT 10', (project_id,))]

    def snapshot(self, project_id, before=None, after=None, limit=50, full_history=False):
        if before is not None and after is not None or not 1 <= limit <= 100:
            raise ValueError('Некорректное окно истории')
        with self.db() as db:
            db.execute('BEGIN')
            row = db.execute('SELECT * FROM projects WHERE id=?', (project_id,)).fetchone()
            if not row:
                raise KeyError('Проект не найден')
            project = dict(row)
            project['memory'] = json.loads(project['memory'])
            sources = [dict(row) for row in db.execute('SELECT id,title,kind,included,length(content) AS characters,created_at FROM sources WHERE project_id=? ORDER BY created_at,rowid', (project_id,))]
            where, params = 'm.project_id=?', [project_id]
            if before is not None:
                where += ' AND m.rowid<?'; params.append(before)
            if after is not None:
                where += ' AND m.rowid>?'; params.append(after)
            order = 'ASC' if after is not None or full_history else 'DESC'
            query = 'SELECT m.*,m.rowid AS sequence,coalesce(t.web_search,0) AS web_search,coalesce(t.citations,\'[]\') AS citations FROM messages m LEFT JOIN message_metadata t ON t.message_id=m.id WHERE '+where+' ORDER BY m.rowid '+order
            if not full_history:
                query += ' LIMIT ?'; params.append(limit)
            messages = [dict(row) for row in db.execute(query, params)]
            if order == 'DESC': messages.reverse()
            total = db.execute('SELECT count(*) FROM messages WHERE project_id=?', (project_id,)).fetchone()[0]
            first = messages[0]['sequence'] if messages else None
            last = messages[-1]['sequence'] if messages else None
            older = bool(first and db.execute('SELECT 1 FROM messages WHERE project_id=? AND rowid<? LIMIT 1', (project_id, first)).fetchone())
            newer = bool(last and db.execute('SELECT 1 FROM messages WHERE project_id=? AND rowid>? LIMIT 1', (project_id, last)).fetchone())
            runs = [dict(row) for row in db.execute('SELECT * FROM runs WHERE project_id=? ORDER BY created_at DESC,rowid DESC LIMIT 10', (project_id,))]
        for message in messages:
            message['attachments'] = json.loads(message['attachments'])
            message['citations'] = json.loads(message['citations'])
            message['web_search'] = bool(message['web_search'])
        for source in sources:
            source['included'] = bool(source['included'])
        return {'project': project, 'sources': sources, 'messages': messages, 'runs': runs,
                'history': {'total': total, 'first': first, 'last': last, 'has_older': older, 'has_newer': newer, 'limit': limit}}
