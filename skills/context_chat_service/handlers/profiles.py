"""Chat-owned profile validation using the Core provider catalog."""
import time
from adaos.sdk.ai import describe_model as descriptor

def validate_profile(profile, models, binding, expected_capability=None):
    model=next((m for m in models if m['id']==profile.get('model')),None)
    if not model or model.get('available') is False:
        raise ValueError('Модель недоступна для подключённого аккаунта. Обновите список моделей.')
    if 'capabilities' not in model: model=descriptor(model)
    if expected_capability is not None and expected_capability!=model['capability_revision']:
        raise ValueError('Возможности модели изменились. Обновите настройки чата.')
    if model.get('expires_at',time.time()+1)<=time.time():
        raise ValueError('Список возможностей устарел. Обновите список моделей.')
    effort_mode=profile.get('effort_mode','provider_default')
    effort=profile.get('effort')
    if effort_mode not in ('provider_default','explicit'):
        raise ValueError('Некорректный режим reasoning')
    if effort_mode=='explicit':
        if effort not in (model['capabilities'].get('efforts') or []):
            raise ValueError('Эта сила reasoning не подтверждена для выбранной модели.')
    else: effort=None
    tools=profile.get('tools',[])
    if not isinstance(tools,list) or len(tools)!=len(set(tools)) or any(v!='web_search' for v in tools):
        raise ValueError('Неподдерживаемый список инструментов')
    if tools and model['capabilities'].get('web_search') is False:
        raise ValueError('Выбранная модель не поддерживает веб-поиск.')
    previous=profile.get('account_binding')
    if previous is not None and previous!=binding:
        raise ValueError('Аккаунт изменился. Явно сохраните настройки чата для текущего аккаунта.')
    return {'model':model['id'],'effort_mode':effort_mode,'effort':effort,'tools':tools,'account_binding':binding,
            'capability_revision':model['capability_revision'],'capability_source':model['capabilities']['source'],
            'default_effort_reported':model['capabilities'].get('default')}
