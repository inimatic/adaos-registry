const $ = id => document.getElementById(id);
const local = localStorage;
const session = sessionStorage;
const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
let state = {projects: [], account: {}, auto_memory: true}, selected = local.getItem('context-chat.project') || '', detail = null, models = [], tab = 'sources', panelOpen = false;
let projectsKey = '', messagesKey = '', contextKey = '', modelsKey = '', polling = false, wasConnected = false;
let refreshVersion=0, accountGeneration=null;
let cachedDrafts=[];
try {cachedDrafts=JSON.parse(session.getItem('context-chat.drafts') || '[]');} catch {}
const drafts = new Map(Array.isArray(cachedDrafts)?cachedDrafts:[]), sending = new Set();
const saveDrafts = () => {try {session.setItem('context-chat.drafts',JSON.stringify([...drafts]));} catch {}};
const draft = () => {if (!drafts.has(selected)) drafts.set(selected, {text:'', files:[]}); return drafts.get(selected);};
function changedDraft() {delete draft().requestKey;saveDrafts();}

async function api(path, method='GET', data) {
  let response;
  try {response = await fetch('api/' + path, {method, headers: data === undefined ? {} : {'Content-Type':'application/json'}, body: data === undefined ? undefined : JSON.stringify(data)});}
  catch {throw new Error('Сервис недоступен. Проверьте подключение к локальному агенту.');}
  if (response.status === 401 || response.status === 403) throw new Error('Сессия интерфейса истекла. Обновите вкладку AdaOS, чтобы продолжить.');
  const result = await response.json().catch(() => ({}));
  if (!response.ok) {const failure=new Error(typeof result.detail === 'string' ? result.detail : 'Не удалось выполнить действие. Попробуйте снова.');failure.status=response.status;throw failure;}
  return result;
}
function error(value) {
  $('error-text').textContent = value.message || value; $('error').hidden = false;
  if ($('modal').open) {
    let alert=$('modal-error');
    if(!alert){alert=document.createElement('p');alert.id='modal-error';alert.className='error-banner';alert.setAttribute('role','alert');$('modal-body').prepend(alert);}
    alert.textContent=value.message || value;
  }
}
function notify(value) {$('toast').textContent = value; $('toast').hidden = false; setTimeout(() => {$('toast').hidden=true;}, 3300);}
async function act(task) {try {await task();} catch (value) {error(value);}}
function busy() {return Boolean(detail && (detail.runs.some(run => ['queued','running'].includes(run.status)) || detail.project.memory_status === 'running'));}
function closeSidebar() {$('sidebar').classList.remove('open'); $('scrim').hidden = true;}
function openModal(title, html, setup) {$('modal-title').textContent=title; $('modal-body').innerHTML=html; $('modal').showModal(); setup?.();}
function closeModal() {$('modal').close();}
function field(id, title, value='', multiline=false, help='') {return `<label class="field"><span>${title}</span>${multiline ? `<textarea id="${id}" rows="4">${escape(value)}</textarea>` : `<input id="${id}" value="${escape(value)}" maxlength="160">`}${help ? `<small>${help}</small>` : ''}</label>`;}

async function refresh() {
  const target=selected, version=++refreshVersion;
  const [snapshot, projectData]=await Promise.all([api('state'),target?api('projects/'+target).catch(value=>{if(value.status===404)return null;throw value;}):Promise.resolve(null)]);
  if(version!==refreshVersion || target!==selected)return;
  state=snapshot;
  detail=projectData;
  const connected = !!state.account.plan_enabled;
  if (connected !== wasConnected || accountGeneration!==state.account.generation || (connected && !models.length)) {
    try {models = connected ? (await api('models')).models : [];}
    catch (failure) {models=[];error(failure);}
    wasConnected = connected;
    accountGeneration=state.account.generation;
    modelsKey = '';
  }
  if (selected && !state.projects.some(project => project.id === selected)) selected='';
  render();
}
async function selectProject(id) {
  selected=id; local.setItem('context-chat.project',id); detail=null; messagesKey=''; contextKey=''; modelsKey='';
  closeSidebar(); $('draft').value=draft().text;
  try {const project=await api('projects/'+id);if(selected!==id)return;detail=project;render();} catch(value) {error(value);}
}
function render() {
  const query = $('search').value.toLocaleLowerCase();
  const key = JSON.stringify([state.projects,selected,query]);
  if (key !== projectsKey) {
    projectsKey=key;
    const rows = state.projects.filter(project => (project.title+' '+project.description).toLocaleLowerCase().includes(query));
    $('projects').innerHTML=rows.map(project => `<button class="project-item ${project.id===selected?'active':''}" data-project="${project.id}" aria-current="${project.id===selected?'page':'false'}"><span class="project-symbol">▱</span><span><strong>${escape(project.title)}</strong><small>${escape(project.description || 'Личное пространство')}</small></span></button>`).join('') || `<p class="sidebar-empty">${state.projects.length?'Ничего не найдено.':'Здесь появятся ваши проекты.<br>Начните с одного разговора.'}</p>`;
    $('project-count').textContent=state.projects.length;
  }
  $('account-label').textContent=state.account.connected ? (state.account.email || 'Аккаунт ChatGPT') : (state.account.pending?'Вход в браузере…':'ChatGPT не подключён');
  $('account-caption').textContent=state.account.plan_enabled?'Используется личный план':'Подключить личный аккаунт';
  renderAccountProof();
  $('project-title').textContent=detail?.project.title || 'Context Chat';
  $('edit-project').disabled=!detail;
  $('attach').disabled=!detail;
  $('draft').disabled=!detail;
  $('context-toggle').disabled=!detail;
  $('source-count').textContent=detail?.sources.filter(source=>source.included).length || 0;
  $('tab-count').textContent=detail?.sources.length || 0;
  const optionsKey=JSON.stringify([models,detail?.project.model]);
  if (optionsKey !== modelsKey) {
    modelsKey=optionsKey;
    $('model').innerHTML=`<option value="">${state.account.plan_enabled?'Выберите модель':'Подключите ChatGPT'}</option>`+models.map(model=>`<option value="${escape(model.id)}">${escape(model.label)}</option>`).join('');
    $('model').value=detail?.project.model || '';
  }
  $('model').disabled=!detail || !state.account.plan_enabled || busy();
  $('send').hidden=busy(); $('stop').hidden=!busy();
  const modelAvailable=models.some(model=>model.id===detail?.project.model);
  $('send').disabled=!detail || !state.account.plan_enabled || !modelAvailable || !draft().text.trim() || busy() || sending.has(selected);
  $('send').title=!state.account.plan_enabled?'Подключите ChatGPT':!detail?.project.model?'Выберите модель':'Отправить сообщение';
  $('composer-note').textContent=!detail?'Создайте проект, чтобы начать разговор.':!state.account.plan_enabled?'Подключите личный ChatGPT, чтобы начать. Контекст можно добавить уже сейчас.':!detail.project.model?'Выберите доступную модель над разговором.':busy()?(detail.project.memory_status==='running'?'Обновляем память проекта…':'Модель отвечает. Вы можете остановить ответ.'):'Ответ использует историю, память и включённые источники этого проекта.';
  renderMessages(); renderFiles(); renderContext();
}
function renderMessages() {
  const key=JSON.stringify([selected,detail?.messages,detail?.runs]);
  if (key===messagesKey) return;
  messagesKey=key;
  const host=$('messages'), nearBottom=host.scrollHeight-host.scrollTop-host.clientHeight<140;
  if (!detail || !detail.messages.length) {
    host.innerHTML=`<div class="empty"><div class="empty-icon">c·</div><span class="eyebrow">${detail?'НОВЫЙ РАЗГОВОР':'ЛИЧНОЕ ПРОСТРАНСТВО'}</span><h2>${detail?'Начнём с вашего контекста':'Разговор, который<br>помнит главное'}</h2><p>${detail?'Добавьте важную переписку, заметки или документы. Или просто начните разговор — память будет расти вместе с ним.':'Создавайте проекты для разных задач. Собирайте контекст и общайтесь с ChatGPT, сохраняя предысторию и ваши предпочтения.'}</p><div class="starter-grid"><button class="starter" data-empty-action="${detail?'source':'create'}">${detail?'▤ Добавить контекст':'＋ Создать проект'}<small>${detail?'Текст, переписка или файл':'Начать с чистого листа'}</small></button><button class="starter" data-empty-action="${detail?'account':'create-context'}">${detail?'◎ Подключить ChatGPT':'▤ Начать с контекста'}<small>${detail?'Использовать личный аккаунт':'Взять опыт из переписки'}</small></button></div></div>`;
  } else {
    host.innerHTML=detail.messages.map(message=>`<article class="message ${message.role}"><div class="message-head">${message.role==='user'?'Вы':'Context Chat'}<small>${escape(message.model)}</small><small>${new Date(message.created_at).toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit'})}</small></div><div class="message-body">${escape(message.content || (message.status==='running'?'…':''))}</div>${message.attachments.map(id=>`<span class="attachment-badge">▤ ${escape(detail.sources.find(source=>source.id===id)?.title || 'Источник удалён')}</span>`).join('')}${['failed','interrupted','cancelled'].includes(message.status)?`<div class="status">${{failed:'Запрос не завершён',interrupted:'Ответ прерван',cancelled:'Остановлено'}[message.status]}</div>`:''}</article>`).join('');
    const last=detail.runs[0];
    if (last && ['failed','interrupted','cancelled'].includes(last.status)) host.innerHTML+=`<div class="error-banner"><span>${escape(last.error || 'Ответ остановлен. Можно повторить последний запрос.')}</span>${last.kind==='chat'&&last.user_message_id?'<button class="quiet" id="retry">Повторить ответ</button>':''}</div>`;
    if (last?.status==='completed' && last.error) host.innerHTML+=`<div class="status">${escape(last.error)}</div>`;
  }
  if (nearBottom || !detail?.messages.length) host.scrollTop=host.scrollHeight;
}
function renderFiles() {
  $('draft-files').innerHTML=draft().files.map(file=>`<span class="draft-file">▤ ${escape(file.title)}<button data-remove-file="${file.id}" aria-label="Убрать ${escape(file.title)}">×</button></span>`).join('');
}
function renderContext() {
  $('context-panel').hidden=!panelOpen || !detail;
  $('context-toggle').setAttribute('aria-expanded',String(panelOpen));
  $('sources-tab').setAttribute('aria-selected',String(tab==='sources'));
  $('memory-tab').setAttribute('aria-selected',String(tab==='memory'));
  if (!detail) return;
  const key=JSON.stringify([selected,detail.sources,detail.project.memory,detail.project.memory_status,detail.project.memory_revision,tab,state.auto_memory,state.account.plan_enabled,busy()]);
  if (key===contextKey) return;
  contextKey=key;
  if (tab==='sources') {
    $('context-content').innerHTML=`<p class="context-description">Добавьте материалы, которые помогут понять предысторию. В чат попадут только включённые источники этого проекта.</p><button class="outline full" id="add-source">＋ Добавить источник</button>${detail.sources.map(source=>`<article class="source-card ${source.included?'':'excluded'}"><button data-view-source="${source.id}" style="text-align:left;padding:0"><strong>▤ ${escape(source.title)}</strong></button><small>${source.kind==='file'?'Документ':'Текст / переписка'} · ${source.characters.toLocaleString('ru-RU')} символов · Сохранён</small><div class="source-bottom"><label><input type="checkbox" data-include-source="${source.id}" ${source.included?'checked':''}> В контексте</label><button data-delete-source="${source.id}">Удалить</button></div></article>`).join('') || '<p class="context-description" style="margin-top:24px">Источников пока нет. Добавьте текст или экспорт переписки, либо прикрепите файл в чате.</p>'}`;
  } else {
    const memory=detail.project.memory;
    $('context-content').innerHTML=`<p class="context-description">То, что полезно помнить между сообщениями. Вы можете проверить и исправить эти сведения.</p>${[['summary','Предыстория'],['preferences','Предпочтения'],['agreements','Договорённости']].map(([key,label])=>`<section class="memory-block"><h3>${label}</h3><p class="${memory[key]?'':'memory-empty'}">${escape(memory[key] || 'Пока не заполнено')}</p></section>`).join('')}<div class="memory-actions"><button class="outline" id="edit-memory">Редактировать</button><button class="outline" id="rebuild-memory" ${busy() || !state.account.plan_enabled || !detail.project.model?'disabled':''}>Обновить с моделью</button></div><div class="memory-state">${{empty:'Память ещё не создана',stale:'Память требует обновления',running:'Обновляем память…',ready:'Память актуальна'}[detail.project.memory_status] || ''} · Версия ${detail.project.memory_revision}</div><label class="auto-memory"><input type="checkbox" id="auto-memory" ${state.auto_memory?'checked':''}>Обновлять после разговора</label><p class="usage-note">Обновление памяти — отдельный запрос к выбранной модели с расходом ресурсов личного плана.</p>${memory.source_ids?.length?`<p class="usage-note">Источники: ${memory.source_ids.map(id=>escape(detail.sources.find(source=>source.id===id)?.title || 'Удалён')).join(', ')}</p>`:''}`;
  }
}

function projectModal(edit=false, withContext=false) {
  const project=edit?detail?.project:null;
  openModal(edit?'Настройки проекта':'Новый проект',`<form id="project-form">${field('project-name','Название',project?.title || '')}${field('project-description','Описание',project?.description || '',true)}${edit?'<button type="button" class="outline" id="export-project">Экспорт проекта</button><p class="usage-note">JSON с историей, источниками и памятью. Файл содержит личные данные проекта.</p>':field('initial-context','Начальный контекст · необязательно','',true,'Вставьте переписку, заметки или фрагмент ленты. Контекст можно добавить и позже.')}<div class="modal-actions">${edit?'<button type="button" class="danger" id="delete-project">Удалить проект</button>':''}<button class="primary" type="submit">${edit?'Сохранить':'Создать проект'}</button></div></form>`,()=>{
    $('project-form').onsubmit=event=>{event.preventDefault(); act(async()=>{
      const data={title:$('project-name').value,description:$('project-description').value};
      const id=project?.id;
      if (edit) {const saved=await api('projects/'+id,'PATCH',data);state.projects=state.projects.map(row=>row.id===id?saved:row);if(detail?.project.id===id)detail.project=saved;}
      else {data.context=$('initial-context').value;const created=await api('projects','POST',data);selected=created.id;local.setItem('context-chat.project',selected);state.projects=[created,...state.projects];detail={project:created,messages:[],sources:[],runs:[]};}
      closeModal(); projectsKey=''; messagesKey=''; contextKey=''; $('draft').value=draft().text;render();await refresh(); if (withContext) {panelOpen=true;renderContext();}
    });};
    if (edit) $('delete-project').onclick=()=>act(async()=>{
      if (!confirm(`Удалить проект «${project.title}» вместе с его перепиской, источниками и памятью?`)) return;
      await api('projects/'+project.id+'?confirmed=true','DELETE'); drafts.delete(project.id);saveDrafts(); selected='';local.removeItem('context-chat.project'); closeModal(); await refresh(); notify('Проект удалён');
    });
    if (edit) $('export-project').onclick=()=>act(async()=>{
      const data=await api('projects/'+project.id+'/export');
      const blob=new Blob([JSON.stringify(data,null,2)],{type:'application/json'}), url=URL.createObjectURL(blob), link=document.createElement('a');
      link.href=url;link.download='context-chat-'+project.id+'.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);notify('Экспорт проекта сохранён');
    });
  });
  $('project-name').focus();
}
function accountModal() {
  openModal('Общее подключение ChatGPT', '<p class="account-explanation">Подключением ChatGPT управляет Core. Откройте приложение Subscription для входа, проверки доступа, переноса существующей сессии или отключения. История, документы и память остаются в Chat.</p><p class="usage-note">Вход открывается на компьютере, где работает узел AdaOS. Расходуется личный план; автоматического платного fallback нет.</p><a href="https://chatgpt.com/settings/usage" target="_blank" rel="noopener noreferrer">Управление расходом в ChatGPT ↗</a>');
}
function renderAccountProof() {
  if (!$('connection-proof')) return;
  const account=state.account, probe=account.probe || {};
  $('connection-proof').textContent=probe.status==='running'?'Проверяем реальный ответ модели…':probe.status==='failed'?probe.error:account.inference_verified?`Доступ подтверждён реальным ответом · ${account.verified_model} · ${new Date(account.verified_at).toLocaleString('ru-RU')}`:account.needs_reauth?'Сессия истекла. Подключите аккаунт повторно.':'Реальный ответ модели ещё не проверен.';
  if ($('verify-account')) $('verify-account').disabled=probe.status==='running';
}
function sourceModal() {
  if (!detail) return;
  const project=selected;
  openModal('Добавить контекст',`<p class="context-description">Переписка, экспорт ленты, заметки или документ. Источник будет доступен только в этом проекте.</p><form id="source-form">${field('source-title','Название источника')}${field('source-text','Текст или переписка','',true)}<div class="modal-actions"><button type="button" class="outline" id="source-file">Загрузить файл</button><button type="submit" class="primary">Добавить текст</button></div><p class="usage-note">TXT, Markdown, JSON, CSV, HTML, DOCX, XLSX · до 10 МБ на файл</p></form>`,()=>{
    $('source-form').onsubmit=event=>{event.preventDefault();act(async()=>{await api('projects/'+project+'/sources','POST',{title:$('source-title').value,content:$('source-text').value});closeModal();await refresh();notify('Источник добавлен');});};
    $('source-file').onclick=()=>{closeModal();$('files').dataset.mode='source';$('files').click();};
  });
}
function memoryModal() {
  const project=detail.project;
  openModal('Память проекта',`<form id="memory-form">${field('memory-summary','Предыстория',project.memory.summary || '',true)}${field('memory-preferences','Предпочтения',project.memory.preferences || '',true)}${field('memory-agreements','Договорённости',project.memory.agreements || '',true)}<div class="modal-actions"><button type="submit" class="primary">Сохранить память</button></div></form>`,()=>{
    $('memory-form').onsubmit=event=>{event.preventDefault();act(async()=>{await api('projects/'+project.id+'/memory','PUT',{summary:$('memory-summary').value,preferences:$('memory-preferences').value,agreements:$('memory-agreements').value,expected_revision:project.memory_revision});closeModal();await refresh();notify('Память сохранена');});};
  });
}
async function send() {
  if ($('send').disabled) return;
  const id=selected, value=draft();sending.add(id); $('send').disabled=true;
  value.requestKey ||= crypto.randomUUID();saveDrafts();
  try {await api('projects/'+id+'/messages','POST',{text:value.text,attachments:value.files.map(file=>file.id),request_key:value.requestKey});value.text='';value.files=[];delete value.requestKey;saveDrafts();if(selected===id)$('draft').value='';await refresh();$('messages').scrollTop=$('messages').scrollHeight;}
  catch(value) {error(value);render();}
  finally {sending.delete(id);render();}
}
async function uploadFiles(files, mode='attachment') {
  if (!selected) return;
  const project=selected;
  if (files.length>10) throw new Error('За раз можно добавить до 10 файлов');
  $('attach').disabled=true;
  try {
    for (const file of files) {
      if (file.size>10*1024*1024) throw new Error('Максимальный размер файла — 10 МБ');
      const bytes=new Uint8Array(await file.arrayBuffer()); let binary='';
      for(let start=0;start<bytes.length;start+=16384) binary+=String.fromCharCode(...bytes.subarray(start,start+16384));
      const source=await api('projects/'+project+'/sources','POST',{title:file.name,file_base64:btoa(binary)});
       if(mode==='attachment') {if(!drafts.has(project))drafts.set(project,{text:'',files:[]});drafts.get(project).files.push(source);delete drafts.get(project).requestKey;saveDrafts();}
    }
    if (mode==='source') {panelOpen=true;tab='sources';}
    await refresh();notify(files.length===1?'Файл добавлен в контекст':'Файлы добавлены в контекст');
  } finally {$('files').value='';$('attach').disabled=!detail;}
}

$('new-project').onclick=()=>projectModal(); $('edit-project').onclick=()=>projectModal(true);
$('search').oninput=render; $('account-button').onclick=accountModal;
$('model').onchange=()=>act(async()=>{await api('projects/'+selected,'PATCH',{model:$('model').value});await refresh();});
$('draft').oninput=()=>{draft().text=$('draft').value;changedDraft();$('draft').style.height='auto';$('draft').style.height=Math.min(190,$('draft').scrollHeight)+'px';render();};
$('draft').onkeydown=event=>{if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault();send();}};
$('send').onclick=send; $('stop').onclick=()=>act(async()=>{await api('projects/'+selected+'/stop','POST');await refresh();});
$('context-toggle').onclick=()=>{panelOpen=!panelOpen;renderContext();}; $('close-context').onclick=()=>{panelOpen=false;renderContext();};
$('sources-tab').onclick=()=>{tab='sources';renderContext();}; $('memory-tab').onclick=()=>{tab='memory';renderContext();};
$('close-modal').onclick=closeModal; $('dismiss-error').onclick=()=>{$('error').hidden=true;};
$('attach').onclick=()=>{$('files').dataset.mode='attachment';$('files').click();};
$('files').onchange=()=>act(()=>uploadFiles([...$('files').files],$('files').dataset.mode));
$('open-projects').onclick=()=>{$('sidebar').classList.add('open');$('scrim').hidden=false;};$('scrim').onclick=closeSidebar;
document.addEventListener('click',event=>{
  const project=event.target.closest('[data-project]'); if(project)selectProject(project.dataset.project);
  const empty=event.target.closest('[data-empty-action]'); if(empty) {const action=empty.dataset.emptyAction; if(action.startsWith('create'))projectModal(false,action==='create-context');if(action==='source')sourceModal();if(action==='account')accountModal();}
  if(event.target.closest('#add-source'))sourceModal(); if(event.target.closest('#edit-memory'))memoryModal();
  if(event.target.closest('#rebuild-memory'))act(async()=>{await api('projects/'+selected+'/memory/rebuild','POST');await refresh();});
  const remove=event.target.closest('[data-remove-file]'); if(remove)act(async()=>{const id=selected;await api('projects/'+id+'/sources/'+remove.dataset.removeFile,'DELETE');const value=drafts.get(id);value.files=value.files.filter(file=>file.id!==remove.dataset.removeFile);delete value.requestKey;saveDrafts();await refresh();});
  const source=event.target.closest('[data-delete-source]'); if(source)act(async()=>{const id=selected;if(!confirm('Удалить источник? Связанные выводы памяти будут сброшены, история сообщений сохранится.'))return;await api('projects/'+id+'/sources/'+source.dataset.deleteSource,'DELETE');const value=drafts.get(id);if(value){value.files=value.files.filter(file=>file.id!==source.dataset.deleteSource);delete value.requestKey;saveDrafts();}await refresh();});
  const view=event.target.closest('[data-view-source]');if(view)act(async()=>{const data=await api('projects/'+selected+'/sources/'+view.dataset.viewSource);openModal(data.title,'<label class="field"><textarea id="source-view" rows="16" readonly></textarea></label>',()=>{$('source-view').value=data.content;});});
  if(event.target.closest('#retry'))act(async()=>{const id=selected,run=detail.runs[0];await api('projects/'+id+'/retry','POST',{run_id:run.id,request_key:'retry-'+run.id});await refresh();});
});
document.addEventListener('change',event=>{
  if(event.target.matches('[data-include-source]'))act(async()=>{await api('projects/'+selected+'/sources/'+event.target.dataset.includeSource,'PATCH',{included:event.target.checked});await refresh();});
  if(event.target.id==='auto-memory')act(async()=>{await api('settings','POST',{auto_memory:event.target.checked});await refresh();});
});
$('draft').value=draft().text;
await act(refresh);
setInterval(async()=>{if(polling||document.hidden)return;polling=true;try{await refresh();}catch(value){error(value);}finally{polling=false;}},1800);
