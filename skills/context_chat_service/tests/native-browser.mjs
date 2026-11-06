import { createRequire } from 'node:module';
import fs from 'node:fs/promises';
import path from 'node:path';
const require=createRequire(path.resolve('e2e/stand/browser/package.json'));
const {chromium}=require('playwright');
const expect=require('@playwright/test').expect.configure({timeout:60000});
const token=process.env.ADAOS_REVIEW_TOKEN;
if(!token)throw new Error('Local node credential is required');
const hub='http://127.0.0.1:8777', prefix='/api/services/context_chat_service/ui/api/';
const target=process.env.ADAOS_NATIVE_TARGET || 'context-chat-builder-dev';
const out=path.resolve(process.env.ADAOS_E2E_OUTPUT || 'e2e/artifacts/context-chat/native');
await fs.mkdir(out,{recursive:true});
const report={target,checks:[],errors:[],responses:[],created:[],passed:false,live_account_tested:false,
  external_navigation_fixture:'OpenAI pages and auth/start only; real local data transport',soak_ms:0};
const browser=await chromium.launch({headless:true});
const context=await browser.newContext({viewport:{width:1440,height:1000},locale:'ru-RU'});
// Unrelated application release notifications must not replace this workflow's dialogs.
// Keep their real data, changing only the browser's automatic prompt flag.
await context.route(/\/api\/component-updates(?:\?|$)/,async route=>{
  if(route.request().method()!=='GET')return route.continue();
  const response=await route.fetch(),data=await response.json();
  await route.fulfill({response,json:{...data,items:(data.items||[]).map(item=>({...item,auto_prompt:false}))}});
});
await context.addInitScript(({hub,token,target})=>{
  window.__ADAOS_DEBUG__=true;window.__ADAOS_BASE__=hub;window.__ADAOS_TOKEN__=token;
  for(const [key,value] of Object.entries({adaos_device_id:'context-chat-builder-review',adaos_webspace_id:target,
    adaos_lang:'ru',adaos_hub_base:hub,adaos_local_hub_base:hub,adaos_try_local_hub:'1',adaos_hub_token:token,
    adaos_local_subnet_id:'sn_6acf0c01',adaos_selected_zone:'lo',adaos_last_used_zone:'lo'}))localStorage.setItem(key,value);
},{hub,token,target});
const page=await context.newPage();page.setDefaultTimeout(60000);
report.requests=[];report.requestFailures=[];
page.on('request',request=>{const url=new URL(request.url());if(url.pathname.startsWith(prefix))report.requests.push({path:url.pathname,method:request.method(),at:Date.now()});});
page.on('requestfailed',request=>{const url=new URL(request.url());if(url.pathname.startsWith(prefix))report.requestFailures.push({path:url.pathname,method:request.method(),error:request.failure()?.errorText});});
report.bootResponses=[];
page.on('pageerror',error=>report.errors.push(error.name+': '+error.message.replace(/https?:\/\/[^\s)]+/g,'[url]')));
page.on('dialog',dialog=>dialog.accept());
page.on('response',response=>{
  const url=new URL(response.url());
  if(url.pathname.startsWith(prefix))report.responses.push({path:url.pathname,status:response.status()});
  else if(url.pathname.startsWith('/api/')&&report.bootResponses.length<150)report.bootResponses.push({path:url.pathname,status:response.status()});
});
const check=name=>{report.checks.push(name);console.log(name);};
const api=async(method,route,data)=>{
  const response=await context.request.fetch(hub+prefix+route,{method,data,headers:{'X-AdaOS-Token':token},timeout:90000});
  if(!response.ok())throw new Error(`Local application request ${method} ${route.split('?')[0]} returned ${response.status()}`);
  return response.json();
};
const ready=async()=>{
  await page.locator('ada-project-list-widget [data-testid=new-project]').waitFor({timeout:180000});
  await expect(page.locator('ada-project-list-widget')).not.toContainText('Загружаем проекты');
  await expect(page.locator('ada-project-conversation-widget')).not.toContainText('Загружаем ваше пространство');
};
try{
  const url=new URL('http://127.0.0.1:8100/');
  for(const [key,value]of Object.entries({zone:'lo',subnet_id:'sn_6acf0c01',webspace_id:target,webspace:target,
    scenario_id:'context_chat',try_local_hub:'1'}))url.searchParams.set(key,value);
  await page.goto(url.href,{waitUntil:'domcontentloaded',timeout:90000});await ready();
  expect(await page.locator('ada-service-frame-widget').count()).toBe(0);
  check('native_declarative_widgets_load_without_iframe');

  let title='[Native beta E2E] '+Date.now();
  await page.getByTestId('new-project').click();
  await page.getByTestId('project-name').fill(title);
  await page.locator('textarea[name=context]').fill('Synthetic initial communication context.');
  const [creation]=await Promise.all([
    page.waitForResponse(response=>new URL(response.url()).pathname===prefix+'projects'&&response.request().method()==='POST'),
    page.getByRole('button',{name:'Сохранить проект',exact:true}).click(),
  ]);
  const project=await creation.json();report.created.push({id:project.id,title});
  await expect(page.locator('ada-project-conversation-widget h1')).toHaveText(title);
  await expect(page.locator('ada-conversation-context-widget')).toBeVisible();
  check('create_project_with_initial_context_and_shared_selection');

  await page.getByRole('button',{name:'＋ Добавить контекст',exact:true}).click();
  await page.getByTestId('source-title').fill('Synthetic communication notes');
  await page.getByTestId('source-text').fill('Synthetic preference: concise replies.');
  await page.getByRole('button',{name:'Добавить текст',exact:true}).click();
  await expect(page.locator('.source-card')).toHaveCount(2);
  const sourceCheck=page.getByRole('checkbox',{name:'Включить источник Synthetic communication notes'});
  await sourceCheck.uncheck();await expect(sourceCheck).not.toBeChecked();
  await sourceCheck.check();await expect(sourceCheck).toBeChecked();
  check('source_crud_and_context_inclusion');

  await page.getByRole('tab',{name:'Память',exact:true}).click();
  await page.getByRole('button',{name:'Редактировать память',exact:true}).click();
  await page.locator('textarea[name=summary]').fill('Synthetic memory summary');
  await page.locator('textarea[name=preferences]').fill('Synthetic concise preference');
  await page.getByRole('button',{name:'Сохранить память',exact:true}).click();
  await expect(page.locator('.memory-section').first()).toContainText('Synthetic memory summary');
  expect((await api('GET','projects/'+project.id)).project.memory.preferences).toBe('Synthetic concise preference');
  check('editable_memory_uses_service_revision');

  const draft='Synthetic unsent draft survives native remount.';
  await page.getByTestId('draft').fill(draft);
  await page.locator('ada-project-conversation-widget input[type=file]').setInputFiles({name:'synthetic-notes.txt',mimeType:'text/plain',buffer:Buffer.from('Synthetic attachment context.')});
  await expect(page.locator('.draft-file').filter({hasText:'synthetic-notes.txt'})).toBeVisible();
  await expect(page.getByRole('button',{name:'Отправить сообщение',exact:true})).toBeDisabled();
  check('file_attachment_joins_context_and_send_requires_a_selected_model');
  if(process.env.ADAOS_NATIVE_XLSX_FIXTURE){
    await page.locator('ada-project-conversation-widget input[type=file]').setInputFiles(process.env.ADAOS_NATIVE_XLSX_FIXTURE);
    await expect(page.locator('.draft-file').filter({hasText:'synthetic-budget.xlsx'})).toBeVisible();
    const sources=await api('GET','projects/'+project.id);
    const sheet=sources.sources.find(source=>source.title==='synthetic-budget.xlsx');
    expect((await api('GET','projects/'+project.id+'/sources/'+sheet.id)).content).toContain('Доход');
    check('xlsx_above_two_megabytes_passes_real_gateway_and_extracts_sheets');
  }
  if(process.env.ADAOS_NATIVE_PDF_FIXTURE){
    await page.locator('ada-project-conversation-widget input[type=file]').setInputFiles(process.env.ADAOS_NATIVE_PDF_FIXTURE);
    await expect(page.locator('.draft-file').filter({hasText:'synthetic-thesis.pdf'})).toBeVisible();
    const sources=await api('GET','projects/'+project.id);
    const pdf=sources.sources.find(source=>source.title==='synthetic-thesis.pdf');
    const text=(await api('GET','projects/'+project.id+'/sources/'+pdf.id)).content;
    expect(text).toContain('[Страница 1]');expect(text).toContain('Synthetic thesis content.');
    check('seven_megabyte_pdf_with_images_extracts_text_through_real_gateway');
  }
  await page.locator('.conversation-workspace').evaluate(element=>{
    const dataTransfer=new DataTransfer();dataTransfer.items.add(new File(['Synthetic drop context.'],'synthetic-drop.txt',{type:'text/plain'}));
    element.dispatchEvent(new DragEvent('dragenter',{dataTransfer,bubbles:true,cancelable:true}));
  });
  await expect(page.locator('.drop-zone')).toContainText('Отпустите файлы');
  await page.locator('.conversation-workspace').evaluate(element=>{
    const dataTransfer=new DataTransfer();dataTransfer.items.add(new File(['Synthetic drop context.'],'synthetic-drop.txt',{type:'text/plain'}));
    element.dispatchEvent(new DragEvent('drop',{dataTransfer,bubbles:true,cancelable:true}));
  });
  await expect(page.locator('.draft-file').filter({hasText:'synthetic-drop.txt'})).toBeVisible();
  await expect(page.locator('.drop-zone')).toHaveCount(0);
  check('drop_over_chat_uploads_and_attaches_without_navigation');
  await page.getByRole('button',{name:'Добавить файлы и инструменты',exact:true}).click();
  await page.getByRole('menuitemcheckbox',{name:/Веб-поиск/}).click();
  await expect(page.getByRole('button',{name:'Выключить веб-поиск',exact:true})).toBeVisible();
  check('composer_tools_menu_enables_visible_search_chip');
  await page.reload({waitUntil:'domcontentloaded',timeout:90000});await ready();
  await expect(page.getByTestId('draft')).toHaveValue(draft);
  await expect(page.locator('.draft-file').filter({hasText:'synthetic-notes.txt'})).toBeVisible();
  await expect(page.getByRole('button',{name:'Выключить веб-поиск',exact:true})).toBeVisible();
  await page.getByRole('button',{name:'Выключить веб-поиск',exact:true}).click();
  check('draft_attachments_and_selection_survive_reload');

  // Isolate UI pagination from inference; real SQL cursor behavior has backend tests.
  const historyPath=hub+prefix+'projects/'+project.id;
  const historyMatcher=url=>url.origin+url.pathname===historyPath;
  const historyFixture=async route=>{
    if(route.request().method()!=='GET')return route.continue();
    const response=await route.fetch(),snapshot=await response.json(),url=new URL(route.request().url());
    const before=Number(url.searchParams.get('before')||0),after=Number(url.searchParams.get('after')||0);
    const last=before?before-1:after?Math.min(123,after+50):123,first=Math.max(1,last-49);
    const messages=Array.from({length:last-first+1},(_,i)=>({id:'history-fixture-'+(first+i),sequence:first+i,role:(first+i)%2?'user':'assistant',content:'Synthetic history '+(first+i),model:'fixture',status:'completed',attachments:[],created_at:'2026-10-18T00:00:00Z'}));
    await route.fulfill({response,json:{...snapshot,messages,history:{total:123,first,last,has_older:first>1,has_newer:last<123,limit:50}}});
  };
  await page.route(historyMatcher,historyFixture);
  await expect(page.locator('[data-message-id]')).toHaveCount(50);
  await page.getByRole('button',{name:'↑ Предыдущие сообщения',exact:true}).click();
  await expect(page.locator('[data-message-id]').first()).toContainText('Synthetic history 24');
  await page.waitForTimeout(3500);
  await expect(page.locator('[data-message-id]').first()).toContainText('Synthetic history 24');
  await expect(page.locator('[data-message-id]')).toHaveCount(50);
  await page.getByRole('button',{name:'К последним',exact:true}).click();
  await expect(page.locator('[data-message-id]').last()).toContainText('Synthetic history 123');
  await page.unroute(historyMatcher,historyFixture);
  check('history_window_stays_bounded_and_polling_preserves_reading_position_fixture');

  let outage=true;
  await page.route('**/api/services/context_chat_service/ui/api/**',route=>outage
    ?route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:{code:'service_unavailable'}})}) :route.continue());
  await expect(page.locator('ada-project-conversation-widget [role=alert]')).toContainText('Приложение на вашем узле AdaOS временно недоступно');
  await expect(page.locator('ada-project-conversation-widget h1')).toHaveText(title);
  await expect(page.getByTestId('draft')).toHaveValue(draft);
  outage=false;
  await page.getByRole('button',{name:'Повторить подключение',exact:true}).click();
  await expect(page.locator('ada-project-conversation-widget [role=alert]')).toHaveCount(0);
  await page.unroute('**/api/services/context_chat_service/ui/api/**');
  check('temporary_node_failure_preserves_content_and_recovers');

  await page.evaluate(()=>{document.body.classList.remove('dark');document.body.setAttribute('tuiTheme','light');});
  await expect.poll(()=>page.locator('.conversation-workspace').evaluate(element=>getComputedStyle(element).backgroundColor)).toBe('rgb(255, 255, 255)');
  await page.screenshot({path:path.join(out,'native-light.png'),fullPage:true});
  await page.evaluate(()=>{document.body.classList.add('dark');document.body.setAttribute('tuiTheme','dark');});
  await expect.poll(()=>page.locator('.conversation-workspace').evaluate(element=>getComputedStyle(element).backgroundColor)).toBe('rgb(2, 6, 23)');
  await page.screenshot({path:path.join(out,'native-dark.png'),fullPage:true});
  check('active_light_and_dark_theme_inheritance');

  await page.getByTestId('account-button').click();
  await context.route('https://chatgpt.com/settings/usage',route=>route.fulfill({status:200,headers:{'Cross-Origin-Opener-Policy':'same-origin'},contentType:'text/html',body:'<title>Usage fixture</title><p>Usage fixture</p>'}));
  let popupPromise=page.waitForEvent('popup');await page.getByRole('link',{name:'Управление расходом ↗'}).click();
  let popup=await popupPromise;await expect(popup.locator('body')).toContainText('Usage fixture');await popup.close();
  check('usage_tab_can_navigate_to_coop_page');
  await context.route('**/api/services/context_chat_service/ui/api/auth/start',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({url:'https://auth.openai.com/api/accounts/authorize?native_navigation_fixture=1'})}));
  await context.route('https://auth.openai.com/**',route=>route.fulfill({status:200,headers:{'Cross-Origin-Opener-Policy':'same-origin'},contentType:'text/html',body:'<title>Provider login fixture</title><p>Provider login fixture</p>'}));
  popupPromise=page.waitForEvent('popup');await page.getByTestId('connect-account').click();popup=await popupPromise;
  await expect(popup.locator('body')).toContainText('Provider login fixture');await popup.close();
  check('continue_with_chatgpt_opens_a_coop_provider_page');
  await page.evaluate(()=>{window.__nativeOpenBackup=window.open;window.open=()=>null;});
  await page.getByTestId('connect-account').click();
  await expect(page.locator('[role=dialog] [role=alert]')).toContainText('Браузер заблокировал');
  await page.evaluate(()=>{window.open=window.__nativeOpenBackup;delete window.__nativeOpenBackup;});
  await page.locator('[role=dialog]').getByRole('button',{name:'Закрыть окно',exact:true}).click();
  await context.unroute('**/api/services/context_chat_service/ui/api/auth/start');await context.unroute('https://auth.openai.com/**');
  check('blocked_popup_has_visible_recovery_instruction');

  const downloadPromise=page.waitForEvent('download');
  await page.getByRole('button',{name:'Настройки проекта',exact:true}).click();
  await page.getByRole('button',{name:'Экспорт проекта',exact:true}).click();
  const download=await downloadPromise;
  const exportPath=path.join(out,'synthetic-project-export.json');await download.saveAs(exportPath);
  const exported=JSON.parse(await fs.readFile(exportPath,'utf8'));
  expect(exported.schema).toBe('context_chat.project.v1');expect(exported.sources.length).toBe(4+(process.env.ADAOS_NATIVE_XLSX_FIXTURE?1:0)+(process.env.ADAOS_NATIVE_PDF_FIXTURE?1:0));
  expect(JSON.stringify(exported)).not.toContain('access_token');
  await page.locator('[role=dialog]').getByRole('button',{name:'Закрыть окно',exact:true}).click();
  check('native_project_export_excludes_credentials');

  await page.getByRole('button',{name:'Настройки проекта',exact:true}).click();
  const renamed=title+' renamed';
  await page.getByTestId('project-name').fill(renamed);
  await page.locator('textarea[name=description]').fill('Synthetic edited description.');
  await page.getByRole('button',{name:'Сохранить проект',exact:true}).click();
  await expect(page.locator('ada-project-conversation-widget h1')).toHaveText(renamed);
  title=renamed;report.created[0].title=renamed;
  expect((await api('GET','projects/'+project.id)).project.description).toBe('Synthetic edited description.');
  check('edit_project_title_and_description');

  await page.setViewportSize({width:390,height:844});
  await page.getByRole('button',{name:'Открыть проекты',exact:true}).click();
  await expect(page.locator('.sidebar')).toBeVisible();
  await page.locator('button[data-project="'+project.id+'"]').click();
  await expect(page.locator('.sidebar')).not.toBeVisible();
  const contextButton=page.locator('ada-project-conversation-widget .topbar-actions button').filter({hasText:'Контекст'});
  if(await contextButton.getAttribute('aria-expanded')!=='true')await contextButton.click();
  await expect(page.locator('.context-panel--open')).toBeVisible();
  await page.getByRole('button',{name:'Закрыть контекст',exact:true}).click();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
  await page.screenshot({path:path.join(out,'native-mobile.png'),fullPage:true});
  check('mobile_projects_drawer_and_no_horizontal_overflow');
  await page.setViewportSize({width:1440,height:1000});
  const anonymous=await browser.newContext();
  expect((await anonymous.request.get(hub+prefix+'projects/'+project.id)).status()).toBe(401);
  await anonymous.close();check('anonymous_client_cannot_read_project_data');

  const duration=Number(process.env.ADAOS_NATIVE_SOAK_MS ?? 320000), started=Date.now();
  while(Date.now()-started<duration){
    await page.waitForTimeout(Math.min(30000,duration-(Date.now()-started)));
    await expect(page.getByTestId('draft')).toHaveValue(draft);
    await expect(page.locator('ada-project-conversation-widget h1')).toHaveText(title);
    await expect(page.locator('ada-project-conversation-widget [role=alert]')).toHaveCount(0);
  }
  report.soak_ms=Date.now()-started;
  if(duration>=310000)check('native_session_survives_beyond_old_five_minute_cookie_limit');
  await page.getByRole('button',{name:'Настройки проекта',exact:true}).click();
  await page.getByRole('button',{name:'Удалить проект',exact:true}).click();
  const [deletion]=await Promise.all([
    page.waitForResponse(response=>new URL(response.url()).pathname===prefix+'projects/'+project.id&&response.request().method()==='DELETE'),
    page.getByRole('button',{name:'Подтвердить удаление',exact:true}).click(),
  ]);
  expect(deletion.status()).toBe(200);report.deleted=[project.id];
  await expect(page.locator('ada-project-conversation-widget h1')).toHaveText('Context Chat');
  check('delete_project_through_confirmation_dialog');
  report.passed=report.errors.length===0;
}catch(error){
  report.failure=error.message.replace(/https?:\/\/[^\s)]+/g,'[url]').slice(0,1800);
  report.applicationErrors=await page.locator('ada-project-list-widget [role=alert],ada-project-conversation-widget [role=alert]').allTextContents().catch(()=>[]);
  report.boot=await page.evaluate(()=>{
    const state=window.__ADAOS_DEBUG_STATE__?.(),sync=state?.sync;
    return {keys:Object.keys(state||{}),syncKeys:Object.keys(sync||{}),ready:sync?.materializationReady,
      materialization:sync?.materialization?{currentScenario:sync.materialization.currentScenario,readinessState:sync.materialization.readinessState,missingBranches:sync.materialization.missingBranches}:null};
  }).catch(()=>null);
  await page.screenshot({path:path.join(out,'failure.png'),fullPage:true}).catch(()=>{});
}
finally{
  report.cleaned=[];
  for(const row of report.created){try{if(!report.deleted?.includes(row.id)){const current=await api('GET','projects/'+row.id);if(current.project.title!==row.title)throw new Error('Fixture ownership changed');await api('DELETE','projects/'+row.id+'?confirmed=true');}report.cleaned.push(row.id);}catch(error){report.errors.push('Owned synthetic project cleanup failed');}}
  report.passed=report.passed&&report.errors.length===0;
  await fs.writeFile(path.join(out,'report.json'),JSON.stringify(report,null,2),'utf8');
  await browser.close();
}
console.log(JSON.stringify({passed:report.passed,checks:report.checks,errors:report.errors,failure:report.failure,soak_ms:report.soak_ms},null,2));
if(!report.passed)process.exitCode=1;
