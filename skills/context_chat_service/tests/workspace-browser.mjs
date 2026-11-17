// Reusable M1 browser cohort. Only uniquely named synthetic projects are mutated.
// Rich-answer/history fixtures affect read responses; no live inference is sent.
import { chromium } from '../../../../../../e2e/stand/browser/node_modules/playwright/index.mjs';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';

const hub=process.env.CONTEXT_CHAT_HUB || 'http://127.0.0.1:8777';
const origin=process.env.CONTEXT_CHAT_CLIENT_ORIGIN || 'http://127.0.0.1:8199';
const target=process.env.CONTEXT_CHAT_WEBSPACE || 'context-chat-m1-beta';
const token=process.env.ADAOS_REVIEW_TOKEN;
if(!token)throw new Error('Owner-admitted node credential required');
const prefix='/api/services/context_chat_service/ui/api/';
const out=process.env.CONTEXT_CHAT_E2E_OUT || 'e2e/artifacts/context-chat/20261018-m1';
await fs.mkdir(out,{recursive:true});
const browser=await chromium.launch({headless:true});
const context=await browser.newContext({viewport:{width:1440,height:1000},locale:'ru-RU'});
const report={checks:[],errors:[],created:[],liveInferenceSent:false,remoteRootOwnerSessionVerified:false,fixtures:['assistant Markdown/history GET for one synthetic chat','unrelated update auto-prompt flag']};
const check=name=>{report.checks.push(name);console.log(name);};
const api=async(method,path,data)=>{
  const response=await context.request.fetch(hub+prefix+path,{method,data,headers:{'X-AdaOS-Token':token,'X-AdaOS-Timeout-Ms':'120000'},timeout:120000});
  assert.equal(response.status(),200,method+' '+path+' returned '+response.status());
  return response.json();
};
let page,projectId,chatA,chatB,fixtureMode='rich';
const stamp='[M1 synthetic '+Date.now()+']';
const markdown='## Проверка диплома\n\n**Важный вывод** и *пояснение*.\n\n- Первый пункт\n- Второй пункт\n\n| Критерий | Оценка |\n|---|---|\n| Структура | Хорошо |\n\n```python\nresult = 2 + 2\n```\n\nФормула \\(x^2+y^2\\).\n\n\\[\n\\frac{a}{b}\n\\]\n\n[Источник](https://example.org/review)';
try{
  assert.equal((await api('GET','state')).protocol,'adaos.project-conversation.v2','Selected runtime must expose M1 before the browser cohort');
  try{await context.grantPermissions(['clipboard-read','clipboard-write'],{origin});}catch{}
  await context.addInitScript(({hub,token,target})=>{
    window.__ADAOS_DEBUG__=true;window.__ADAOS_BASE__=hub;window.__ADAOS_TOKEN__=token;
    for(const[key,value]of Object.entries({adaos_device_id:'context-chat-m1-review',adaos_webspace_id:target,adaos_lang:'ru',adaos_hub_base:hub,adaos_local_hub_base:hub,adaos_try_local_hub:'1',adaos_hub_token:token,adaos_local_subnet_id:'sn_6acf0c01',adaos_selected_zone:'lo',adaos_last_used_zone:'lo'}))localStorage.setItem(key,value);
  },{hub,token,target});
  await context.route(/\/api\/component-updates(?:\?|$)/,async route=>{
    if(route.request().method()!=='GET')return route.continue();
    const response=await route.fetch(),data=await response.json();
    await route.fulfill({response,json:{...data,items:(data.items||[]).map(item=>({...item,auto_prompt:false}))}});
  });
  await context.route(/\/api\/services\/context_chat_service\/ui\/api\//,async route=>{
    const request=route.request(),url=new URL(request.url());
    if(request.method()==='POST'&&(/\/(messages|retry|memory\/rebuild|auth\/verify)$/.test(url.pathname))){
      report.liveInferenceSent=true;await route.abort('blockedbyclient');return;
    }
    if(chatA&&request.method()==='GET'&&url.pathname===prefix+'projects/'+projectId+'/chats/'+chatA){
      const response=await route.fetch(),data=await response.json();
      if(fixtureMode==='rich'){
        data.messages=[{id:'synthetic-rich-answer',role:'assistant',content:markdown,content_revision:'synthetic-v1',model:'synthetic-presentation',status:'completed',attachments:[],created_at:new Date().toISOString(),citations:[]}];
        data.history={total:1,first:1,last:1,has_older:false,has_newer:false,limit:50};
      }else{
        const before=Number(url.searchParams.get('before')),after=Number(url.searchParams.get('after'));
        const last=before?before-1:after?Math.min(5000,after+50):5000,first=last-49;
        data.messages=Array.from({length:50},(_,index)=>({id:'history-'+(first+index),sequence:first+index,role:index%2?'assistant':'user',content:'Synthetic bounded history '+(first+index),status:'completed',model:'synthetic',attachments:[],created_at:new Date().toISOString()}));
        data.history={total:5000,first,last,has_older:first>1,has_newer:last<5000,limit:50};
      }
      await route.fulfill({response,json:data});return;
    }
    await route.continue();
  });
  page=await context.newPage();page.setDefaultTimeout(60000);
  page.on('pageerror',error=>report.errors.push(error.name+': '+error.message.replace(/https?:\/\/[^\s)]+/g,'[url]')));
  await page.goto(origin+'/?zone=lo&subnet_id=sn_6acf0c01&try_local_hub=1&scenario_id=context_chat&webspace='+target+'&webspace_id='+target,{waitUntil:'domcontentloaded',timeout:120000});
  await page.getByTestId('new-project').waitFor({timeout:120000});
  assert.equal(await page.locator('ada-service-frame-widget iframe').count(),0);
  check('native_declarative_widgets_without_service_iframe');
  await page.getByTestId('new-project').click();await page.getByTestId('project-name').fill(stamp);
  const[creation]=await Promise.all([page.waitForResponse(r=>new URL(r.url()).pathname===prefix+'projects'&&r.request().method()==='POST'),page.getByRole('button',{name:'Сохранить проект',exact:true}).click()]);
  assert.equal(creation.status(),200);projectId=(await creation.json()).id;report.created.push(projectId);
  await page.locator('[data-project="'+projectId+'"]').waitFor();
  check('real_project_create_with_empty_optional_fields');
  const createChat=async title=>{
    await page.getByRole('button',{name:'Создать чат',exact:true}).click();await page.locator('input[name=chatTitle]').fill(title);
    const[response]=await Promise.all([page.waitForResponse(r=>new URL(r.url()).pathname===prefix+'projects/'+projectId+'/chats'&&r.request().method()==='POST'),page.getByRole('button',{name:'Сохранить',exact:true}).click()]);
    assert.equal(response.status(),200);return(await response.json()).id;
  };
  chatA=await createChat('Студент А');chatB=await createChat('Студент Б');
  await page.locator('[data-chat="'+chatA+'"]').click();
  await page.waitForFunction(()=>document.querySelector('ada-project-conversation-widget h1')?.textContent==='Студент А');
  const models=(await api('GET','models')).models;
  const model=models.find(m=>m.capabilities?.efforts?.length&&m.available!==false)||models.find(m=>m.available!==false);
  assert.ok(model,'Connected account must expose a catalog; no provider invocation is used');
  const picker=page.locator('ada-project-conversation-widget ada-chat-profile-picker');
  await picker.locator('select').first().selectOption(model.id);
  await picker.locator('[aria-busy="false"]').waitFor();
  let current=await api('GET','projects/'+projectId+'/chats/'+chatA);
  for(let attempt=0;current.chat.profile.model!==model.id&&attempt<30;attempt++){await page.waitForTimeout(200);current=await api('GET','projects/'+projectId+'/chats/'+chatA);}
  assert.equal(current.chat.profile.model,model.id);
  const efforts=model.capabilities?.efforts||[];
  if(efforts.length){await picker.locator('select').nth(1).selectOption(efforts.at(-1));}
  await picker.locator('[aria-busy="false"]').waitFor();
  current=await api('GET','projects/'+projectId+'/chats/'+chatA);
  if(efforts.length)assert.equal(current.chat.profile.effort,efforts.at(-1));
  check('per_chat_model_effort_saved_without_inference');
  if(model.capabilities?.web_search!==false){
    await page.getByRole('button',{name:'Добавить файлы и инструменты',exact:true}).click();
    await page.getByRole('menuitemcheckbox',{name:/Веб-поиск/}).click();await page.keyboard.press('Escape');
    for(let attempt=0;attempt<30;attempt++){current=await api('GET','projects/'+projectId+'/chats/'+chatA);if(current.chat.profile.tools.includes('web_search'))break;await page.waitForTimeout(200);}
    assert.ok(current.chat.profile.tools.includes('web_search'));
    check('internet_tool_policy_is_saved_on_the_chat');
  }
  await page.getByTestId('draft').fill('Личный черновик А');
  await page.locator('[data-chat="'+chatB+'"]').click();await page.getByTestId('draft').fill('Личный черновик Б');
  await page.locator('[data-chat="'+chatA+'"]').click();assert.equal(await page.getByTestId('draft').inputValue(),'Личный черновик А');
  check('chat_drafts_do_not_cross_selection');
  const transfer=await page.evaluateHandle(()=>{const data=new DataTransfer();data.items.add(new File(['# Synthetic Student A\n\nPrivate to chat A.'],'student-a.md',{type:'text/markdown'}));return data;});
  await page.locator('main.conversation-workspace').dispatchEvent('drop',{dataTransfer:transfer});
  for(let attempt=0;attempt<30;attempt++){current=await api('GET','projects/'+projectId+'/chats/'+chatA);if(current.sources.some(s=>s.title==='student-a.md'))break;await page.waitForTimeout(200);}
  assert.ok(current.sources.some(s=>s.title==='student-a.md'&&s.included));
  const rubric=await api('POST','projects/'+projectId+'/chats/'+chatA+'/sources',{title:'Общая рубрика',content:'Synthetic common grading rubric.'});
  await api('PATCH','projects/'+projectId+'/chats/'+chatB+'/sources/'+rubric.id,{included:true});
  const other=await api('GET','projects/'+projectId+'/chats/'+chatB);
  assert.ok(other.sources.some(s=>s.title==='student-a.md'&&!s.included));
  assert.deepEqual(other.context.source_revisions.map(s=>s.id),[rubric.id]);
  check('drop_upload_stays_in_origin_chat_and_rubric_is_explicitly_shared');
  await page.locator('ada-chat-message-content h2').waitFor();
  assert.equal(await page.locator('ada-chat-message-content table').count(),1);
  assert.equal(await page.locator('ada-chat-message-content pre code').count(),1);
  assert.equal(await page.locator('ada-chat-message-content .katex-display').count(),1);
  await page.evaluate(()=>document.fonts.ready);
  const positions=await page.locator('ada-chat-message-content').evaluate(host=>{
    const inline=host.querySelector('.katex:not(.katex-display .katex)');
    const base=inline.querySelector('.katex-html .mathnormal').getBoundingClientRect();
    const exponent=inline.querySelector('.msupsub .mord').getBoundingClientRect();
    const fraction=host.querySelector('.katex-display');
    const letters=[...fraction.querySelectorAll('.katex-html .mathnormal')];
    return {baseY:base.y,exponentY:exponent.y,aY:letters.find(e=>e.textContent==='a').getBoundingClientRect().y,bY:letters.find(e=>e.textContent==='b').getBoundingClientRect().y,lineWidth:fraction.querySelector('.frac-line').getBoundingClientRect().width};
  });
  assert.ok(positions.exponentY<positions.baseY,'Superscript must appear above the base');
  assert.ok(positions.aY<positions.bY&&positions.lineWidth>0,'Fraction must retain vertical layout and its rule');
  check('shared_markdown_tables_code_inline_and_display_math');
  await page.locator('ada-chat-message-actions').getByRole('button',{name:'Копировать',exact:true}).click();
  // The Windows system clipboard normalizes line endings to CRLF.
  assert.equal((await page.evaluate(()=>navigator.clipboard.readText())).replace(/\r\n/g,'\n'),markdown);
  check('copy_keeps_exact_original_markdown');
  await page.evaluate(()=>{window.__originalClipboardWrite=navigator.clipboard.writeText.bind(navigator.clipboard);navigator.clipboard.writeText=async()=>{throw new DOMException('denied','NotAllowedError');};});
  await page.locator('ada-chat-message-actions').getByRole('button',{name:'Копировать',exact:true}).click();
  await page.locator('ada-chat-message-actions [role=status]').filter({hasText:'недоступен'}).waitFor();
  await page.evaluate(()=>{navigator.clipboard.writeText=window.__originalClipboardWrite;});
  check('clipboard_denial_is_actionable');
  await page.locator('ada-chat-message-actions').getByRole('button',{name:'Без форматирования',exact:true}).click();
  const plain=await page.evaluate(()=>navigator.clipboard.readText());
  assert.ok(plain.includes('Проверка диплома')&&!plain.includes('## ')&&!plain.includes('**Важный'));
  check('plain_text_copy_preserves_readable_answer_and_formula_source');
  await page.locator('ada-project-conversation-widget').screenshot({path:out+'/conversation-wide.png'});
  fixtureMode='history';await page.reload({waitUntil:'domcontentloaded'});await page.locator('.message').first().waitFor();
  assert.equal(await page.locator('article.message').count(),50);
  await page.getByRole('button',{name:'↑ Предыдущие сообщения',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('article.message')?.textContent.includes('4901'));
  assert.equal(await page.locator('article.message').count(),50);assert.equal(await page.getByTestId('draft').inputValue(),'Личный черновик А');
  check('five_thousand_message_projection_stays_bounded_and_keeps_draft');
  await page.setViewportSize({width:390,height:844});
  await page.waitForTimeout(300);
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2));
  await page.getByRole('button',{name:'Открыть проекты',exact:true}).click();
  await page.locator('[data-chat="'+chatB+'"]').click();
  await page.waitForFunction(()=>document.querySelector('ada-project-conversation-widget h1')?.textContent==='Студент Б');
  await page.getByTestId('draft').waitFor();assert.equal(await page.getByTestId('draft').inputValue(),'Личный черновик Б');
  check('compact_keyboard_accessible_project_chat_navigation');
  await page.locator('ada-project-conversation-widget').screenshot({path:out+'/conversation-compact.png'});
  await page.setViewportSize({width:1440,height:1000});
  await page.getByRole('button',{name:'Переименовать',exact:true}).click();await page.locator('input[name=chatTitle]').fill('Студент Б — проверка');await page.getByRole('button',{name:'Сохранить',exact:true}).click();
  await page.getByRole('button',{name:'В архив',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('.composer-note')?.textContent.includes('архиве'));
  await page.getByRole('button',{name:'Восстановить',exact:true}).click();
  check('chat_rename_archive_restore_are_real_owner_operations');
  const exported=await api('GET','projects/'+projectId+'/workspace-export');
  assert.equal(exported.schema,'context_chat.project.v2');assert.equal(exported.chats.length,3);
  assert.ok(exported.chats.every(c=>c.messages.length===0),'Presentation fixtures must not enter canonical history');
  check('full_export_has_all_real_chats_and_no_presentation_fixture');
  assert.equal(report.liveInferenceSent,false);assert.deepEqual(report.errors,[]);report.passed=true;
}catch(error){report.passed=false;report.failure=String(error.message).replace(/https?:\/\/[^\s)]+/g,'[url]').slice(0,1600);if(page)await page.screenshot({path:out+'/browser-failure.png'}).catch(()=>{});}
finally{
  try{for(const id of report.created)await api('DELETE','projects/'+id+'?confirmed=true');report.syntheticProjectsRemoved=true;}catch(error){report.cleanupFailure=error.name;report.passed=false;}
  await browser.close();await fs.writeFile(out+'/browser.json',JSON.stringify(report,null,2));
}
console.log(JSON.stringify(report,null,2));if(!report.passed)process.exitCode=1;
