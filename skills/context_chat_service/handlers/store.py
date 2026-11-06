"""Owner-local chat workspace, with one explicit legacy history authority.

The authenticated Core gateway admits this single-owner installation. Callers
cannot supply an owner/account/namespace. All chat lookups validate their parent.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from uuid import uuid4

from adaos.domain.relational_storage import RelationalMigration
from .legacy_store import Store as LegacyStore, SCHEMA, BETA_SCHEMA, TOOLS_SCHEMA, now

CHAT_SCHEMA = [
    "ALTER TABLE projects ADD COLUMN defaults_revision INTEGER NOT NULL DEFAULT 0",
    "CREATE TABLE chats (id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE, title TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0, profile TEXT NOT NULL DEFAULT '{}', profile_revision INTEGER NOT NULL DEFAULT 0, memory TEXT NOT NULL DEFAULT '{}', memory_revision INTEGER NOT NULL DEFAULT 0, context_revision INTEGER NOT NULL DEFAULT 0, memory_status TEXT NOT NULL DEFAULT 'empty', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "ALTER TABLE messages ADD COLUMN chat_id TEXT REFERENCES chats(id) ON DELETE CASCADE",
    "ALTER TABLE runs ADD COLUMN chat_id TEXT REFERENCES chats(id) ON DELETE CASCADE",
    "ALTER TABLE runs ADD COLUMN snapshot TEXT NOT NULL DEFAULT '{}'",
    "ALTER TABLE runs ADD COLUMN retry_of TEXT",
    "CREATE TABLE source_versions (id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE, revision INTEGER NOT NULL, content TEXT, digest TEXT NOT NULL, extraction TEXT NOT NULL, original BLOB, original_digest TEXT, created_at TEXT NOT NULL, revoked_at TEXT, UNIQUE(source_id,revision))",
    "CREATE TABLE chat_sources (chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE, source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE, version_id TEXT NOT NULL REFERENCES source_versions(id), PRIMARY KEY(chat_id,source_id))",
    "CREATE TABLE source_tombstones (id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE, digest TEXT NOT NULL, deleted_at TEXT NOT NULL)",
    "CREATE TABLE migration_journal (id TEXT PRIMARY KEY, receipt TEXT NOT NULL, created_at TEXT NOT NULL)",
    "CREATE INDEX chat_history ON messages(chat_id)",
    "CREATE INDEX chat_runs ON runs(chat_id,created_at)",
    "CREATE UNIQUE INDEX run_retry_once ON runs(retry_of) WHERE retry_of IS NOT NULL",
    "CREATE INDEX project_chats ON chats(project_id,archived,updated_at)",
    "CREATE TABLE source_presets (id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE, title TEXT NOT NULL, sources TEXT NOT NULL)",
    "CREATE TABLE shared_memories (project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE, memory TEXT NOT NULL, revision INTEGER NOT NULL, promoted_from TEXT, updated_at TEXT NOT NULL)",
    "INSERT INTO chats(id,project_id,title,profile,memory,memory_revision,context_revision,memory_status,created_at,updated_at) SELECT 'legacy-'||id,id,'Основной',json_object('model',model,'effort_mode','provider_default','effort',NULL,'tools',json('[]'),'account_binding',NULL),memory,memory_revision,context_revision,memory_status,created_at,updated_at FROM projects",
    "UPDATE messages SET chat_id='legacy-'||project_id",
    "UPDATE runs SET chat_id='legacy-'||project_id",
    "INSERT INTO source_versions(id,source_id,revision,content,digest,extraction,created_at) SELECT 'legacy-'||id,id,1,content,'','legacy-extracted-text',created_at FROM sources",
    "INSERT INTO chat_sources SELECT 'legacy-'||project_id,id,'legacy-'||id FROM sources WHERE included=1",
]
MIGRATIONS = ((1,'context_chat_initial',SCHEMA),(2,'context_chat_beta_turns',BETA_SCHEMA),(3,'context_chat_tools',TOOLS_SCHEMA),(4,'context_chat_workspace',CHAT_SCHEMA))


def digest(value):
    raw=value if isinstance(value,bytes) else json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
    return hashlib.sha256(raw).hexdigest()


class Store(LegacyStore):
    def __init__(self,root,*,managed=False):
        root=Path(root); path=root/'db/context_chat.db'; recovery=None
        if managed and not path.exists(): raise RuntimeError('Core must initialize the declared application database before service start')
        if managed and path.exists():
            with sqlite3.connect(path) as admitted:
                history=dict(admitted.execute('SELECT version,checksum FROM adaos_schema_migrations'))
            expected={v:RelationalMigration(version=v,name=n,statements=tuple(sql),idempotent=v==1,dialects=('sqlite',)).checksum for v,n,sql in MIGRATIONS}
            if history!=expected: raise RuntimeError('Installed data requires the governed Application migration before service start')
        if path.exists():
            with sqlite3.connect(path) as old:
                ready=old.execute("SELECT 1 FROM sqlite_master WHERE name='chats'").fetchone()
                if not ready and old.execute('SELECT count(*) FROM projects').fetchone()[0]:
                    private=next((p for p in root.resolve().parents if p.name=='.adaos'),root)
                    recovery=private/'recovery/applications/context_chat/m1'/digest(str(root.resolve()))[:24]/uuid4().hex
                    recovery.mkdir(parents=True,exist_ok=False)
                    with sqlite3.connect(recovery/'before.sqlite') as copy: old.backup(copy)
                    credential=root/'files/chatgpt-session.enc'
                    if credential.exists(): (recovery/'chatgpt-session.enc').write_bytes(credential.read_bytes())
        super().__init__(root)
        self.namespace=self.setting('data_namespace')
        if not self.namespace:
            self.namespace='context-chat:'+uuid4().hex; self.set_setting('data_namespace',self.namespace)
        version,name,statements=MIGRATIONS[-1]
        checksum=RelationalMigration(version=version,name=name,statements=tuple(statements),idempotent=False,dialects=('sqlite',)).checksum
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT checksum FROM adaos_schema_migrations WHERE version=?',(version,)).fetchone()
            if old and old[0]!=checksum: raise RuntimeError('Workspace migration checksum changed')
            if not old:
                before=self._legacy_receipt(db)
                for statement in statements: db.execute(statement)
                for row in db.execute('SELECT id,content FROM source_versions').fetchall():
                    db.execute('UPDATE source_versions SET digest=? WHERE id=?',(digest(row['content']),row['id']))
                self._bind_migrated_account(db)
                after=self._legacy_receipt(db)
                if before!=after: raise RuntimeError('Workspace migration changed legacy content/order')
                receipt={'before':before,'after':after,'recovery_ref':str(recovery) if recovery else None,'history_authority':'application_legacy','account_ciphertext_changed':False}
                db.execute('INSERT INTO migration_journal VALUES(?,?,?)',('default-chats-v1',json.dumps(receipt),now()))
                db.execute('INSERT INTO adaos_schema_migrations VALUES(?,?,?,?,0)',(version,name,checksum,now()))
            elif not db.execute("SELECT 1 FROM migration_journal WHERE id='default-chats-v1'").fetchone():
                # Governed Core cutover applies the declared SQL chain to a
                # fenced snapshot before starting this process. Verify its
                # mapping and hydrate digests; Core owns that recovery receipt.
                if db.execute("SELECT 1 FROM messages m LEFT JOIN chats c ON c.id=m.chat_id WHERE c.id IS NULL OR c.project_id!=m.project_id LIMIT 1").fetchone():
                    raise RuntimeError('Managed migration did not preserve message ownership')
                for row in db.execute("SELECT v.id,v.content,s.content AS original FROM source_versions v JOIN sources s ON s.id=v.source_id WHERE v.digest='' AND v.revision=1").fetchall():
                    if row['content']!=row['original']: raise RuntimeError('Managed source migration changed content')
                    db.execute('UPDATE source_versions SET digest=? WHERE id=?',(digest(row['content']),row['id']))
                receipt={'verified':self._legacy_receipt(db),'recovery_authority':'core.application_data_lifecycle','history_authority':'application_legacy','account_ciphertext_changed':False}
                self._bind_migrated_account(db)
                db.execute('INSERT INTO migration_journal VALUES(?,?,?)',('default-chats-v1',json.dumps(receipt),now()))
            db.execute("UPDATE chats SET memory_status='stale' WHERE memory_status='running'")
        if recovery: (recovery/'receipt.json').write_text(json.dumps(receipt,indent=2),encoding='utf-8')

    @staticmethod
    def _bind_migrated_account(db):
        settings={r['key']:json.loads(r['value']) for r in db.execute("SELECT key,value FROM settings WHERE key IN ('subject','client_id')")}
        if settings.get('subject') and settings.get('client_id'):
            binding=hashlib.sha256((settings['subject']+'|'+settings['client_id']).encode()).hexdigest()
            for row in db.execute("SELECT id,profile FROM chats WHERE id='legacy-'||project_id").fetchall():
                profile=json.loads(row['profile']); profile['account_binding']=binding
                db.execute('UPDATE chats SET profile=? WHERE id=?',(json.dumps(profile),row['id']))

    @staticmethod
    def _legacy_receipt(db):
        result={}
        for table in ('projects','sources','messages','runs','message_metadata'):
            columns={'projects':'id,title,description,model,memory,memory_revision,context_revision,memory_status,created_at,updated_at','messages':'id,project_id,role,content,model,status,attachments,created_at','runs':'id,project_id,kind,status,message_id,error,created_at,request_key,user_message_id'}.get(table,'*')
            rows=[dict(r) for r in db.execute(f'SELECT {columns} FROM {table} ORDER BY rowid')]
            result[table]={'count':len(rows),'digest':digest(rows)}
        return result

    def default_chat(self,project_id):
        self.project(project_id); return self.chat(project_id,'legacy-'+project_id)

    def project_metadata(self,project_id):
        project=self.project(project_id)
        return {k:v for k,v in project.items() if k not in ('memory','memory_revision','context_revision','memory_status')}

    def update_metadata(self,project_id,values,expected_revision):
        self.project(project_id)
        allowed={k:v for k,v in values.items() if k in ('title','description','model')}
        with self.db() as db:
            if db.execute('UPDATE projects SET '+','.join(k+'=?' for k in allowed)+',updated_at=?,defaults_revision=defaults_revision+1 WHERE id=? AND defaults_revision=?',(*allowed.values(),now(),project_id,expected_revision)).rowcount!=1:
                raise ValueError('Настройки проекта изменились. Обновите страницу.')
        return self.project_metadata(project_id)

    def project(self,project_id):
        result=super().project(project_id)
        with self.db() as db:
            row=db.execute('SELECT memory,memory_revision,context_revision,memory_status FROM chats WHERE id=?',('legacy-'+project_id,)).fetchone()
        if row:
            result.update(dict(row)); result['memory']=json.loads(row['memory'])
        return result

    def chat(self,project_id,chat_id):
        with self.db() as db: row=db.execute('SELECT * FROM chats WHERE id=? AND project_id=?',(chat_id,project_id)).fetchone()
        if not row: raise KeyError('Чат не найден в этом проекте')
        result=dict(row)
        for key in ('memory','profile'): result[key]=json.loads(result[key])
        result['archived']=bool(result['archived']); result['history_authority']='application_legacy'
        result['conversation_ref']=self.namespace+':chat:'+chat_id
        result['control_conversation_ref']=self.namespace+':control'
        return result

    def chats(self,project_id,*,query='',archived=False,offset=0,limit=50):
        self.project(project_id)
        if not 1<=limit<=100 or offset<0: raise ValueError('Некорректное окно списка чатов')
        with self.db() as db:
            where,params='project_id=? AND archived=? AND instr(casefold(title),casefold(?))>0',(project_id,int(archived),query)
            rows=[dict(r) for r in db.execute('SELECT id,title,archived,created_at,updated_at FROM chats WHERE '+where+' ORDER BY updated_at DESC,id LIMIT ? OFFSET ?',(*params,limit,offset))]
            total=db.execute('SELECT count(*) FROM chats WHERE '+where,params).fetchone()[0]
        return {'items':rows,'total':total,'offset':offset,'limit':limit,'has_more':offset+len(rows)<total}

    def create_project(self,title,description='',context=''):
        identity,stamp=uuid4().hex,now()
        with self.db() as db: db.execute('INSERT INTO projects(id,title,description,created_at,updated_at) VALUES(?,?,?,?,?)',(identity,title,description,stamp,stamp))
        self.create_chat(identity,'Основной',chat_id='legacy-'+identity)
        if context.strip(): self.add_source(identity,'Начальный контекст','text',context)
        return self.project(identity)

    def create_chat(self,project_id,title,*,chat_id=None):
        project=self.project(project_id); identity,stamp=chat_id or uuid4().hex,now()
        profile={'model':project['model'],'effort_mode':'provider_default','effort':None,'tools':[],'account_binding':None}
        with self.db() as db: db.execute('INSERT INTO chats(id,project_id,title,profile,created_at,updated_at) VALUES(?,?,?,?,?,?)',(identity,project_id,title,json.dumps(profile),stamp,stamp))
        return self.chat(project_id,identity)

    def update_chat(self,project_id,chat_id,*,title=None,archived=None):
        self.chat(project_id,chat_id)
        with self.db() as db:
            if title is not None: db.execute('UPDATE chats SET title=?,updated_at=? WHERE id=?',(title,now(),chat_id))
            if archived is not None:
                if archived and db.execute("SELECT 1 FROM runs WHERE chat_id=? AND status='running'",(chat_id,)).fetchone(): raise ValueError('Сначала остановите текущий ответ')
                db.execute('UPDATE chats SET archived=?,updated_at=? WHERE id=?',(int(archived),now(),chat_id))
        return self.chat(project_id,chat_id)

    def delete_chat(self,project_id,chat_id):
        self.chat(project_id,chat_id)
        if chat_id=='legacy-'+project_id: raise ValueError('Основной чат сохраняет совместимость истории. Его можно архивировать.')
        with self.db() as db: db.execute('DELETE FROM chats WHERE id=?',(chat_id,))

    def update_project(self,project_id,**values):
        result=super().update_project(project_id,**values)
        if 'model' in values:
            chat=self.default_chat(project_id)
            self.save_profile(project_id,chat['id'],{**chat['profile'],'model':values['model']},chat['profile_revision'])
        return result

    def save_profile(self,project_id,chat_id,profile,expected_revision):
        self.chat(project_id,chat_id)
        with self.db() as db:
            if db.execute('UPDATE chats SET profile=?,profile_revision=profile_revision+1,updated_at=? WHERE id=? AND profile_revision=?',(json.dumps(profile),now(),chat_id,expected_revision)).rowcount!=1: raise ValueError('Настройки чата изменились. Обновите их перед сохранением.')
        return self.chat(project_id,chat_id)

    def sources(self,project_id,include_content=False,chat_id=None):
        chat_id=chat_id or self.default_chat(project_id)['id']; self.chat(project_id,chat_id)
        with self.db() as db:
            rows=[dict(r) for r in db.execute("SELECT s.id,s.title,s.kind,s.created_at,v.id AS version_id,v.revision,v.digest,v.extraction,v.original_digest,v.content,c.source_id IS NOT NULL AS included,EXISTS(SELECT 1 FROM source_versions retained WHERE retained.source_id=s.id AND retained.original IS NOT NULL AND retained.revoked_at IS NULL) AS original_retained FROM sources s LEFT JOIN chat_sources c ON c.source_id=s.id AND c.chat_id=? JOIN source_versions v ON v.id=coalesce(c.version_id,(SELECT id FROM source_versions WHERE source_id=s.id AND revoked_at IS NULL ORDER BY revision DESC LIMIT 1)) AND v.revoked_at IS NULL WHERE s.project_id=? ORDER BY s.created_at,s.id",(chat_id,project_id))]
        for row in rows:
            row['characters']=len(row['content'] or ''); row['included']=bool(row['included']); row['original_retained']=bool(row['original_retained'])
            if not include_content: row.pop('content')
        return rows

    def add_source(self,project_id,title,kind,content,*,chat_id=None,original=None,extraction='text-v1'):
        chat_id=chat_id or self.default_chat(project_id)['id']; self.chat(project_id,chat_id)
        identity,version,stamp=uuid4().hex,uuid4().hex,now()
        retained=original if self.setting('retain_originals',False) else None
        with self.db() as db:
            db.execute('INSERT INTO sources(id,project_id,title,kind,content,created_at) VALUES(?,?,?,?,?,?)',(identity,project_id,title,kind,content,stamp))
            db.execute('INSERT INTO source_versions(id,source_id,revision,content,digest,extraction,original,original_digest,created_at) VALUES(?,?,1,?,?,?,?,?,?)',(version,identity,content,digest(content),extraction,retained,hashlib.sha256(original).hexdigest() if original is not None else None,stamp))
            db.execute('INSERT INTO chat_sources VALUES(?,?,?)',(chat_id,identity,version)); self._invalidate_chat(db,chat_id,clear=False)
        return identity

    def _invalidate_chat(self,db,chat_id,clear=True):
        db.execute("UPDATE chats SET context_revision=context_revision+1,memory_revision=memory_revision+1,memory_status='stale',updated_at=?"+(",memory='{}'" if clear else '')+' WHERE id=?',(now(),chat_id))

    def change_source(self,project_id,source_id,included=None,delete=False,*,chat_id=None):
        chat_id=chat_id or self.default_chat(project_id)['id']; self.chat(project_id,chat_id)
        with self.db() as db:
            row=db.execute('SELECT * FROM sources WHERE id=? AND project_id=?',(source_id,project_id)).fetchone()
            if not row: raise KeyError('Источник не найден в этом проекте')
            if delete:
                affected=[r[0] for r in db.execute('SELECT chat_id FROM chat_sources WHERE source_id=?',(source_id,))]
                db.execute('INSERT OR REPLACE INTO source_tombstones VALUES(?,?,?,?)',(source_id,project_id,digest(row['content']),now()))
                db.execute('DELETE FROM chat_sources WHERE source_id=?',(source_id,))
                db.execute('UPDATE source_versions SET content=NULL,original=NULL,revoked_at=? WHERE source_id=?',(now(),source_id))
                db.execute("UPDATE sources SET content='',included=0 WHERE id=?",(source_id,))
                for run in db.execute("SELECT id,snapshot FROM runs WHERE project_id=? AND snapshot!='{}'",(project_id,)).fetchall():
                    snapshot=json.loads(run['snapshot'])
                    if any(s['id']==source_id for s in snapshot.get('context',{}).get('source_revisions',[])):
                        snapshot['inputs']=[]; snapshot['context_revoked_at']=now()
                        db.execute('UPDATE runs SET snapshot=? WHERE id=?',(json.dumps(snapshot,ensure_ascii=False),run['id']))
                for identity in affected: self._invalidate_chat(db,identity)
            else:
                if included:
                    version=db.execute('SELECT id FROM source_versions WHERE source_id=? AND revoked_at IS NULL ORDER BY revision DESC LIMIT 1',(source_id,)).fetchone()
                    if not version: raise ValueError('Источник удалён')
                    db.execute('INSERT OR REPLACE INTO chat_sources VALUES(?,?,?)',(chat_id,source_id,version[0]))
                else: db.execute('DELETE FROM chat_sources WHERE chat_id=? AND source_id=?',(chat_id,source_id))
                self._invalidate_chat(db,chat_id)

    def source_original(self,project_id,source_id):
        self.project(project_id)
        with self.db() as db: row=db.execute('SELECT s.title,v.original FROM sources s JOIN source_versions v ON v.source_id=s.id WHERE s.project_id=? AND s.id=? AND v.revoked_at IS NULL AND v.original IS NOT NULL ORDER BY v.revision DESC LIMIT 1',(project_id,source_id)).fetchone()
        if not row or row['original'] is None: raise KeyError('Оригинал не сохранён или удалён')
        return row['title'],bytes(row['original'])

    def message_count(self,chat_id):
        with self.db() as db: return db.execute('SELECT count(*) FROM messages WHERE chat_id=?',(chat_id,)).fetchone()[0]

    def shared_memory(self,project_id):
        self.project(project_id)
        with self.db() as db: row=db.execute('SELECT * FROM shared_memories WHERE project_id=?',(project_id,)).fetchone()
        return {**dict(row),'memory':json.loads(row['memory'])} if row else {'memory':{},'revision':0,'promoted_from':None}

    def save_shared_memory(self,project_id,memory,revision,*,promoted_from=None):
        self.project(project_id)
        if promoted_from: self.chat(project_id,promoted_from)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT revision FROM shared_memories WHERE project_id=?',(project_id,)).fetchone()
            if (row[0] if row else 0)!=revision: raise ValueError('Общая память изменилась. Обновите страницу.')
            db.execute('INSERT OR REPLACE INTO shared_memories VALUES(?,?,?,?,?)',(project_id,json.dumps(memory,ensure_ascii=False),revision+1,promoted_from,now()))
        return self.shared_memory(project_id)

    def validate_snapshot_sources(self,project_id,snapshot):
        if snapshot.get('namespace')!=self.namespace: raise ValueError('Снимок относится к другому приложению')
        for source in snapshot.get('context',{}).get('source_revisions',[]):
            with self.db() as db: found=db.execute('SELECT 1 FROM source_versions v JOIN sources s ON s.id=v.source_id WHERE v.id=? AND s.project_id=? AND v.revoked_at IS NULL',(source['version_id'],project_id)).fetchone()
            if not found: raise ValueError('Источник исходного запроса удалён. Повтор со старым контекстом запрещён; отправьте новый запрос.')

    def revise_source(self,project_id,source_id,content,*,chat_id=None):
        chat_id=chat_id or self.default_chat(project_id)['id']; self.chat(project_id,chat_id)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            source=db.execute('SELECT * FROM sources WHERE project_id=? AND id=?',(project_id,source_id)).fetchone()
            if not source: raise KeyError('Источник не найден')
            previous=db.execute('SELECT max(revision) FROM source_versions WHERE source_id=? AND revoked_at IS NULL',(source_id,)).fetchone()[0]
            if previous is None: raise ValueError('Удалённый источник нельзя редактировать')
            identity=uuid4().hex
            db.execute('INSERT INTO source_versions(id,source_id,revision,content,digest,extraction,created_at) VALUES(?,?,?,?,?,?,?)',(identity,source_id,previous+1,content,digest(content),'manual-markdown-v1',now()))
            db.execute('UPDATE sources SET content=? WHERE id=?',(content,source_id))
            # Existing chats remain pinned until their owner selects the new revision.
            db.execute('INSERT OR REPLACE INTO chat_sources VALUES(?,?,?)',(chat_id,source_id,identity)); self._invalidate_chat(db,chat_id)
        return identity

    def source_versions(self,project_id,source_id,*,include_content=False):
        self.project(project_id)
        with self.db() as db: return [dict(r) for r in db.execute('SELECT v.id,v.revision,v.digest,v.extraction,v.created_at,v.revoked_at,v.original_digest,v.content IS NOT NULL AS readable'+(',v.content' if include_content else '')+' FROM source_versions v JOIN sources s ON s.id=v.source_id WHERE s.project_id=? AND s.id=? ORDER BY revision DESC',(project_id,source_id))]

    def presets(self,project_id):
        self.project(project_id)
        with self.db() as db: rows=[dict(r) for r in db.execute('SELECT * FROM source_presets WHERE project_id=? ORDER BY title,id',(project_id,))]
        for r in rows: r['sources']=json.loads(r['sources'])
        return rows

    def save_preset(self,project_id,title,source_ids):
        allowed={s['id'] for s in self.sources(project_id)}
        if not set(source_ids).issubset(allowed): raise ValueError('Источник удалён или относится к другому проекту')
        identity=uuid4().hex
        with self.db() as db: db.execute('INSERT INTO source_presets VALUES(?,?,?,?)',(identity,project_id,title,json.dumps(source_ids)))
        return identity

    def apply_preset(self,project_id,chat_id,preset_id):
        self.chat(project_id,chat_id)
        preset=next((p for p in self.presets(project_id) if p['id']==preset_id),None)
        if not preset: raise KeyError('Набор источников не найден')
        allowed={s['id'] for s in self.sources(project_id,chat_id=chat_id)}
        if not set(preset['sources']).issubset(allowed): raise ValueError('Набор содержит удалённые источники. Создайте новый набор.')
        with self.db() as db:
            db.execute('DELETE FROM chat_sources WHERE chat_id=?',(chat_id,))
            for identity in preset['sources']:
                version=db.execute('SELECT id FROM source_versions WHERE source_id=? AND revoked_at IS NULL ORDER BY revision DESC LIMIT 1',(identity,)).fetchone()[0]
                db.execute('INSERT INTO chat_sources VALUES(?,?,?)',(chat_id,identity,version))
            self._invalidate_chat(db,chat_id)

    def save_memory(self,project_id,memory,expected_revision,context_revision=None,*,chat_id=None):
        chat_id=chat_id or self.default_chat(project_id)['id']; self.chat(project_id,chat_id)
        with self.db() as db:
            query="UPDATE chats SET memory=?,memory_revision=memory_revision+1,memory_status='ready',updated_at=? WHERE id=? AND memory_revision=?"
            params=[json.dumps(memory,ensure_ascii=False),now(),chat_id,expected_revision]
            if context_revision is not None: query+=' AND context_revision=?'; params.append(context_revision)
            if db.execute(query,params).rowcount!=1: raise ValueError('Память или источники изменились. Обновите страницу.')
        return self.chat(project_id,chat_id)

    def messages(self,project_id,chat_id=None): return self.snapshot(project_id,chat_id=chat_id,full_history=True)['messages']

    def add_message(self,project_id,role,content,model='',status='completed',attachments=(),*,chat_id=None):
        chat_id=chat_id or self.default_chat(project_id)['id']; self.chat(project_id,chat_id); identity=uuid4().hex
        with self.db() as db: db.execute('INSERT INTO messages(id,project_id,role,content,model,status,attachments,created_at,chat_id) VALUES(?,?,?,?,?,?,?,?,?)',(identity,project_id,role,content,model,status,json.dumps(list(attachments)),now(),chat_id))
        return identity

    def recent_context(self,project_id,budget,skip_message=None,*,chat_id=None,with_refs=False):
        chat_id=chat_id or self.default_chat(project_id)['id']; self.chat(project_id,chat_id); rows,used=[],0
        with self.db() as db:
            for row in db.execute("SELECT id,role,content FROM messages WHERE chat_id=? AND status='completed' AND content!='' AND id!=? ORDER BY rowid DESC LIMIT 200",(chat_id,skip_message or '')):
                if used+len(row['content'])>budget: break
                rows.append(dict(row)); used+=len(row['content'])
        rows.reverse()
        while rows and rows[0]['role']!='user': rows.pop(0)
        return rows if with_refs else [{k:v for k,v in r.items() if k!='id'} for r in rows]

    def request_run(self,project_id,request_key,*,chat_id=None):
        chat_id=chat_id or self.default_chat(project_id)['id']; self.chat(project_id,chat_id)
        with self.db() as db: row=db.execute('SELECT * FROM runs WHERE project_id=? AND chat_id=? AND request_key IN (?,?)',(project_id,chat_id,request_key,chat_id+':'+str(request_key))).fetchone()
        return self._run(row) if row else None

    @staticmethod
    def _run(row):
        result=dict(row); result['snapshot']=json.loads(result['snapshot']); return result

    def run(self,project_id,run_id,*,chat_id=None):
        self.project(project_id)
        with self.db() as db: row=db.execute('SELECT * FROM runs WHERE project_id=? AND id=?',(project_id,run_id)).fetchone()
        if not row or chat_id and row['chat_id']!=chat_id: raise KeyError('Запрос не найден в этом чате')
        return self._run(row)

    def runs(self,project_id,chat_id=None,*,full=False):
        chat_id=chat_id or self.default_chat(project_id)['id']; self.chat(project_id,chat_id)
        with self.db() as db: return [self._run(r) for r in db.execute('SELECT * FROM runs WHERE chat_id=? ORDER BY created_at DESC,rowid DESC'+('' if full else ' LIMIT 10'),(chat_id,))]

    def reserve_turn(self,project_id,kind,text,model,attachments,request_key=None,retry=None,*,chat_id=None,snapshot=None):
        chat_id=chat_id or self.default_chat(project_id)['id']; chat=self.chat(project_id,chat_id)
        if chat['archived']: raise ValueError('Восстановите чат из архива перед отправкой')
        identity,user_id,message_id=uuid4().hex,None,None
        request_key=(chat_id+':'+request_key) if request_key else None
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if snapshot and not retry:
                current=db.execute('SELECT profile_revision,memory_revision,context_revision FROM chats WHERE id=?',(chat_id,)).fetchone()
                context=snapshot['context']
                shared=db.execute('SELECT revision FROM shared_memories WHERE project_id=?',(project_id,)).fetchone()
                if (current['profile_revision']!=snapshot['profile_revision'] or current['memory_revision']!=context['memory_revision'] or current['context_revision']!=context['context_revision'] or (shared[0] if shared else 0)!=context['shared_memory_revision']):
                    raise ValueError('Контекст или параметры изменились во время отправки. Обновите чат и повторите.')
            if db.execute("SELECT 1 FROM runs WHERE chat_id=? AND status IN ('queued','running')",(chat_id,)).fetchone(): raise ValueError('В чате уже выполняется запрос')
            if retry and db.execute('SELECT 1 FROM runs WHERE retry_of=?',(retry['id'],)).fetchone(): raise ValueError('Этот запрос уже повторён')
            if kind=='chat':
                user_id=retry['user_message_id'] if retry else uuid4().hex; message_id=uuid4().hex
                if not retry: db.execute('INSERT INTO messages(id,project_id,role,content,model,status,attachments,created_at,chat_id) VALUES(?,?,?,?,?,?,?,?,?)',(user_id,project_id,'user',text,model,'completed',json.dumps(list(attachments)),now(),chat_id))
                db.execute('INSERT INTO messages(id,project_id,role,content,model,status,attachments,created_at,chat_id) VALUES(?,?,?,?,?,?,?,?,?)',(message_id,project_id,'assistant','',model,'running','[]',now(),chat_id))
            db.execute('INSERT INTO runs(id,project_id,kind,status,message_id,error,created_at,request_key,user_message_id,chat_id,snapshot,retry_of) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(identity,project_id,kind,'running',message_id,'',now(),request_key,user_id,chat_id,json.dumps(snapshot or {},ensure_ascii=False),retry['id'] if retry else None))
            db.execute('UPDATE chats SET updated_at=? WHERE id=?',(now(),chat_id))
        return identity,message_id

    def memory_progress(self,run_id,project_id,status,*,chat_id=None):
        chat_id=chat_id or self.default_chat(project_id)['id']
        with self.db() as db: db.execute("UPDATE chats SET memory_status=? WHERE id=? AND EXISTS(SELECT 1 FROM runs WHERE id=? AND status='running' AND chat_id=?)",(status,chat_id,run_id,chat_id))

    def snapshot(self,project_id,before=None,after=None,limit=50,full_history=False,*,chat_id=None):
        chat_id=chat_id or self.default_chat(project_id)['id']
        # Read the completion fence before content: a worker may finish between
        # these reads, but a completed receipt must not accompany older memory.
        runs=self.runs(project_id,chat_id,full=full_history)
        chat=self.chat(project_id,chat_id); project=self.project(project_id)
        if before is not None and after is not None or not 1<=limit<=100: raise ValueError('Некорректное окно истории')
        with self.db() as db:
            db.execute('BEGIN'); where,params='m.chat_id=?',[chat_id]
            if before is not None: where+=' AND m.rowid<?'; params.append(before)
            if after is not None: where+=' AND m.rowid>?'; params.append(after)
            order='ASC' if after is not None or full_history else 'DESC'
            query="SELECT m.*,m.rowid AS sequence,coalesce(t.web_search,0) AS web_search,coalesce(t.citations,'[]') AS citations FROM messages m LEFT JOIN message_metadata t ON t.message_id=m.id WHERE "+where+' ORDER BY m.rowid '+order
            if not full_history: query+=' LIMIT ?'; params.append(limit)
            messages=[dict(r) for r in db.execute(query,params)]
            if order=='DESC': messages.reverse()
            total=db.execute('SELECT count(*) FROM messages WHERE chat_id=?',(chat_id,)).fetchone()[0]
            first,last=(messages[0]['sequence'],messages[-1]['sequence']) if messages else (None,None)
            older=bool(first and db.execute('SELECT 1 FROM messages WHERE chat_id=? AND rowid<? LIMIT 1',(chat_id,first)).fetchone())
            newer=bool(last and db.execute('SELECT 1 FROM messages WHERE chat_id=? AND rowid>? LIMIT 1',(chat_id,last)).fetchone())
        for message in messages:
            for key in ('attachments','citations'): message[key]=json.loads(message[key])
            message['content_revision']=digest(message['content']); message['web_search']=bool(message['web_search'])
        return {'project':project,'chat':chat,'sources':self.sources(project_id,chat_id=chat_id),'messages':messages,'runs':runs,'history':{'total':total,'first':first,'last':last,'has_older':older,'has_newer':newer,'limit':limit},'history_authority':'application_legacy'}


class LegacyHistoryAdapter:
    """M1 history port; no Core storage or dual-writer claim."""
    authority='application_legacy'
    def __init__(self,store): self.store=store
    def recent(self,project_id,chat_id,budget,skip_message=None): return self.store.recent_context(project_id,budget,skip_message,chat_id=chat_id,with_refs=True)
    def page(self,project_id,chat_id,**window): return self.store.snapshot(project_id,chat_id=chat_id,**window)
