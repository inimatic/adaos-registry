"""Version 2 chat workspace protocol, mounted by the owner-admitted service."""
from __future__ import annotations
import base64
import copy
from urllib.parse import quote
from uuid import uuid4
from fastapi import Body, Query
from fastapi.responses import Response
from .profiles import validate_profile


def install(app,store,account,runtime,prefix,bounded,text_file,max_upload,max_context):
    root=prefix+'/api/projects/{project_id}'
    chat=root+'/chats/{chat_id}'

    @app.patch(root+'/metadata')
    def metadata(project_id:str,body:dict=Body(...)):
        values={}
        for key,label,limit in (('title','Название',160),('description','Описание',2000)):
            if key in body: values[key]=bounded(body[key],label,limit,key=='title')
        if 'default_model' in body:
            model=bounded(body['default_model'],'Модель по умолчанию',160)
            if model and model!=store.project_metadata(project_id)['model'] and model not in {m['id'] for m in account.models() if m.get('available',True)}: raise ValueError('Модель по умолчанию недоступна')
            values['model']=model
        if type(body.get('expected_revision')) is not int or not values: raise ValueError('Не указана версия или параметры проекта')
        with runtime.lock: return store.update_metadata(project_id,values,body['expected_revision'])

    @app.get(root+'/chats')
    def chats(project_id:str,q:str='',archived:bool=False,offset:int=Query(0,ge=0),limit:int=Query(50,ge=1,le=100)):
        return store.chats(project_id,query=q[:160],archived=archived,offset=offset,limit=limit)

    @app.post(root+'/chats')
    def create_chat(project_id:str,body:dict=Body(...)):
        result=store.create_chat(project_id,bounded(body.get('title'),'Название чата',160,True))
        status=account.status()
        if status.get('plan_enabled'):
            # Seeding a new chat must not perform a catalog/network request
            # after the create transaction has committed.
            profile={**result['profile'],'account_binding':status.get('binding_ref')}
            result=store.save_profile(project_id,result['id'],profile,result['profile_revision'])
        return result

    @app.get(chat)
    def detail(project_id:str,chat_id:str,before:int|None=Query(None,ge=1),after:int|None=Query(None,ge=0),limit:int=Query(50,ge=1,le=100)):
        result=runtime.history.page(project_id,chat_id,before=before,after=after,limit=limit)
        result['project']=store.project_metadata(project_id)
        result['shared_memory']=store.shared_memory(project_id)
        result['presets']=store.presets(project_id)
        try:
            _,_,result['context']=runtime.context_snapshot(project_id,chat_id)
        except ValueError as exc: result['context']={'error':str(exc),'limit_characters':max_context}
        # Full inference inputs are an immutable private record, not a polling payload.
        for run in result['runs']:
            run['snapshot']={k:v for k,v in run['snapshot'].items() if k not in ('inputs','instructions','question','runtime_owner')}
        return result

    @app.patch(chat)
    def update_chat(project_id:str,chat_id:str,body:dict=Body(...)):
        title=bounded(body['title'],'Название чата',160,True) if 'title' in body else None
        archived=body.get('archived')
        if archived is not None and not isinstance(archived,bool): raise ValueError('Некорректный статус архива')
        with runtime.lock: return store.update_chat(project_id,chat_id,title=title,archived=archived)

    @app.delete(chat)
    def remove_chat(project_id:str,chat_id:str,confirmed:bool=False):
        if not confirmed: raise ValueError('Подтвердите удаление чата и его истории')
        with runtime.lock:
            runtime.cancel_project(project_id,chat_id=chat_id); store.delete_chat(project_id,chat_id)
        return {'ok':True,'library_retained':True}

    @app.put(chat+'/profile')
    def profile(project_id:str,chat_id:str,body:dict=Body(...)):
        revision=body.get('expected_revision')
        if type(revision) is not int: raise ValueError('Не указана версия настроек')
        if not account.status().get('plan_enabled'): raise ValueError('Подключите аккаунт')
        binding=account.status().get('binding_ref')
        requested=body.get('profile')
        if not isinstance(requested,dict): raise ValueError('Некорректные настройки')
        # Explicit save acknowledges the current binding; browser cannot name an account.
        requested={**requested,'account_binding':binding}
        validated=validate_profile(requested,account.models(),binding,body.get('capability_revision'))
        with runtime.lock: return store.save_profile(project_id,chat_id,validated,revision)

    @app.post(chat+'/messages')
    def send(project_id:str,chat_id:str,body:dict=Body(...)):
        text=bounded(body.get('text',''),'Сообщение',30000,True)
        attachments=body.get('attachments',[])
        if not isinstance(attachments,list) or len(attachments)>10 or not all(isinstance(s,str) for s in attachments): raise ValueError('Некорректные вложения')
        if 'web_search' in body or 'model' in body or 'effort' in body: raise ValueError('Сначала сохраните параметры чата')
        request_key=bounded(body.get('request_key',uuid4().hex),'Идентификатор',100,True)
        expected=body.get('profile_revision')
        with runtime.lock:
            if expected is not None and expected!=store.chat(project_id,chat_id)['profile_revision']: raise ValueError('Настройки чата изменились. Обновите их перед отправкой.')
            return {'run_id':runtime.launch(project_id,'chat',text,attachments,request_key,chat_id=chat_id)}

    @app.post(chat+'/retry')
    def retry(project_id:str,chat_id:str,body:dict=Body(...)):
        return {'run_id':runtime.launch(project_id,'chat',request_key=bounded(body.get('request_key',uuid4().hex),'Идентификатор',100,True),retry_id=bounded(body.get('run_id'),'Запрос',100,True),chat_id=chat_id)}

    @app.post(chat+'/stop')
    def stop(project_id:str,chat_id:str):
        runtime.cancel_project(project_id,chat_id=chat_id); return {'ok':True}

    @app.post(chat+'/sources')
    def upload(project_id:str,chat_id:str,body:dict=Body(...)):
        title=bounded(body.get('title'),'Источник',240,True); original=None
        if 'file_base64' in body:
            encoded=body['file_base64']
            if not isinstance(encoded,str) or len(encoded)>max_upload*4//3+8: raise ValueError('Максимальный размер файла — 10 МБ')
            content=text_file(title,encoded); kind='file'; original=base64.b64decode(encoded,validate=True)
        else: content=bounded(body.get('content',''),'Контекст',max_context,True); kind='text'
        with runtime.lock:
            identity=store.add_source(project_id,title,kind,content,chat_id=chat_id,original=original,extraction='document-markdown-v1' if original else 'text-v1')
        return {'id':identity,'title':title,'kind':kind,'original_retained':bool(original is not None and store.setting('retain_originals',False))}

    @app.patch(chat+'/sources/{source_id}')
    def source_membership(project_id:str,chat_id:str,source_id:str,body:dict=Body(...)):
        if not isinstance(body.get('included'),bool): raise ValueError('Ожидалось значение переключателя')
        with runtime.lock: store.change_source(project_id,source_id,included=body['included'],chat_id=chat_id)
        return {'ok':True}

    @app.get(chat+'/sources/{source_id}')
    def source_content(project_id:str,chat_id:str,source_id:str):
        result=next((s for s in store.sources(project_id,True,chat_id) if s['id']==source_id),None)
        if not result: raise KeyError('Источник удалён или недоступен')
        return result

    @app.put(chat+'/sources/{source_id}')
    def revise_source(project_id:str,chat_id:str,source_id:str,body:dict=Body(...)):
        content=bounded(body.get('content'),'Текст источника',max_context,True)
        with runtime.lock: identity=store.revise_source(project_id,source_id,content,chat_id=chat_id)
        return {'version_id':identity}

    @app.get(root+'/sources/{source_id}/versions')
    def versions(project_id:str,source_id:str): return {'items':store.source_versions(project_id,source_id)}

    @app.get(root+'/sources/{source_id}/original')
    def download(project_id:str,source_id:str):
        filename,data=store.source_original(project_id,source_id)
        return Response(data,media_type='application/octet-stream',headers={'Content-Disposition':"attachment; filename*=UTF-8''"+quote(filename,safe='')})

    @app.put(chat+'/memory')
    def memory(project_id:str,chat_id:str,body:dict=Body(...)):
        value={k:bounded(body.get(k,''),k,12000) for k in ('summary','preferences','agreements')}
        value['source_ids']=[s['id'] for s in store.sources(project_id,chat_id=chat_id) if s['included']]
        if type(body.get('expected_revision')) is not int: raise ValueError('Не указана версия памяти')
        return store.save_memory(project_id,value,body['expected_revision'],chat_id=chat_id)

    @app.post(chat+'/memory/rebuild')
    def rebuild(project_id:str,chat_id:str): return {'run_id':runtime.launch(project_id,'memory',chat_id=chat_id)}

    @app.put(root+'/shared-memory')
    def shared_memory(project_id:str,body:dict=Body(...)):
        if body.get('confirmed_shared') is not True: raise ValueError('Подтвердите доступность этой памяти всем чатам проекта')
        if type(body.get('expected_revision')) is not int: raise ValueError('Не указана версия общей памяти')
        memory={k:bounded(body.get(k,''),k,12000) for k in ('summary','preferences','agreements')}
        return store.save_shared_memory(project_id,memory,body['expected_revision'],promoted_from=body.get('promoted_from'))

    @app.post(root+'/source-presets')
    def preset(project_id:str,body:dict=Body(...)):
        ids=body.get('source_ids',[])
        if not isinstance(ids,list) or not all(isinstance(v,str) for v in ids): raise ValueError('Некорректный набор')
        return {'id':store.save_preset(project_id,bounded(body.get('title'),'Название набора',160,True),ids)}

    @app.post(chat+'/source-presets/{preset_id}')
    def apply_preset(project_id:str,chat_id:str,preset_id:str):
        with runtime.lock: store.apply_preset(project_id,chat_id,preset_id)
        return {'ok':True}

    @app.get(root+'/workspace-export')
    def export(project_id:str,include_originals:bool=False):
        with runtime.lock:
            with store.db() as db: identities=[r[0] for r in db.execute('SELECT id FROM chats WHERE project_id=? ORDER BY created_at,id',(project_id,))]
            chats=[store.snapshot(project_id,chat_id=identity,full_history=True) for identity in identities]
            for item in chats:
                for run in item['runs']:
                    for key in ('inputs','instructions','runtime_owner'): run['snapshot'].pop(key,None)
            sources=store.sources(project_id,True)
            for source in sources:
                source['versions']=store.source_versions(project_id,source['id'],include_content=True)
                if include_originals and source['original_retained']:
                    _,raw=store.source_original(project_id,source['id']); source['original_base64']=base64.b64encode(raw).decode()
            return {'schema':'context_chat.project.v2','project':store.project(project_id),'chats':chats,'sources':sources,'shared_memory':store.shared_memory(project_id),'originals_included':include_originals}
