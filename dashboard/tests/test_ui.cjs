// Browser regressions for pointer/keyboard interactions during native sampling.
const {test, before, after} = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '../static');
let server, browser, origin;

before(async () => {
  server = http.createServer((req, res) => {
    if (req.url === '/test') {
      res.setHeader('Content-Type', 'text/html');
      res.end('<iframe id="ui" src="/luci-static/resources/hitwh-mwan/index.html" style="width:100%;height:950px;border:0"></iframe>');
      return;
    }
    const name = req.url.split('/').pop();
    const types = {'index.html':'text/html','app.js':'text/javascript','transport.js':'text/javascript','styles.css':'text/css'};
    if (!types[name]) {res.writeHead(404); res.end(); return;}
    res.setHeader('Content-Type', types[name]); res.end(fs.readFileSync(path.join(root, name)));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  origin = `http://127.0.0.1:${server.address().port}`;
  browser = await chromium.launch({headless:true, ...(process.env.UI_TEST_BROWSER ? {channel:process.env.UI_TEST_BROWSER} : {})});
});
after(async () => {await browser?.close(); if (server) await new Promise(resolve => server.close(resolve));});

async function openUI(t) {
  const page = await browser.newPage({viewport:{width:1440,height:1000}});
  t.after(() => page.close());
  await page.addInitScript(() => {
    if (location.pathname !== '/test') return;
    window.mock = {calls:[], submitted:[], pending:false, nextResult:null, delayGets:false, pendingReads:[],
      saved:{wan2:{username:'fake-user',password:'fake-secret',mac:'02:00:00:00:00:02'}},
      paths:[2,3].map(n => ({
      interface:'wan'+n, device:'macwan'+n, mac:'02:00:00:00:00:0'+n,
      ip:'192.0.2.'+n, state:'inactive', is_main:false, credentials_saved:false
    }))};
    let clock=100, target, action;
    window.addEventListener('message', e => {
      const iframe=document.querySelector('iframe');
      if (e.origin!==location.origin || e.source!==iframe.contentWindow || e.data.type!=='hitwh-ready') return;
      const channel=new MessageChannel();
      channel.port1.onmessage=({data}) => {
        let result;
        if (data.method==='snapshot') {
          clock+=2;
          result={raw:{timestamp:1700000000+clock,clock,cpu:[clock,0,0,clock*9,0,0,0,0],
            memory_used_percent:18,load:[0.1,0.2,0.3],conntrack:{count:100,max:10000},
            paths:mock.paths.map(p=>({...p,rx_counter:clock*100000,tx_counter:clock*1000}))},settings:{}};
        } else if (data.method==='credential_choices') {
          result={ok:true,choices:[{interface:'wan2',username:'fake-user',interfaces:['wan2']}]};
        } else if (data.method==='credentials') {
          const saved=mock.saved[data.args.interface];
          result={ok:true,configured:!!saved,username:saved?.username||'',password:saved?.password||'',mac:saved?.mac||''};
          if (mock.delayGets && saved) {
            mock.pendingReads.push(()=>channel.port1.postMessage({id:data.id,result})); return;
          }
        } else if (data.method==='start') {
          action=data.args.action;
          mock.calls.push({action:data.args.action,interface:data.args.interface});
          mock.submitted.push(data.args);
          target=mock.paths.find(p=>p.interface===data.args.interface);
          result={ok:true,job:'FAKE01'};
        } else if (data.method==='job') {
          if (mock.pending) result={ok:true,state:'running'};
          else if (action==='upgrade') result={ok:true,state:'done',upgrade:{code:'no_update',current:'1.0.0-6',latest:'1.0.0-6'}};
          else if (action==='credentials-save') {
            const values=mock.submitted.at(-1);
            mock.saved[values.interface]={username:values.username,password:values.password,mac:target.mac};
            result={ok:true,state:'done',message:'操作完成'};
          } else {
            const attempted=target.state==='active'?0:1;
            result=mock.nextResult || {ok:true,state:'done',details:[],attempted,recovered:attempted,remaining:0};
            if (result.ok) target.state='active';
          }
        }
        channel.port1.postMessage({id:data.id,result});
      };
      iframe.contentWindow.postMessage({type:'hitwh-connected'},location.origin,[channel.port2]);
    });
  });
  await page.goto(origin+'/test');
  const ui=page.frameLocator('#ui');
  await ui.locator('.path-refresh').first().waitFor();
  const child=page.frames().find(f=>f.url().includes('/luci-static/resources/hitwh-mwan/'));
  return {page,ui,child,button:ui.locator('.path-refresh[data-interface="wan2"]'),
    feedback:ui.locator('.path-row[data-interface="wan2"] .path-feedback')};
}

test('a sample between pointer down and up preserves exactly one targeted refresh', async t => {
  const {page,ui,child,button,feedback}=await openUI(t);
  await button.evaluate(b=>window.savedButton=b);
  await button.scrollIntoViewIfNeeded();
  const bounds=await button.boundingBox();
  await page.mouse.move(bounds.x+bounds.width/2,bounds.y+bounds.height/2);
  await page.mouse.down();
  await child.evaluate(()=>poll());
  const sameButton=await button.evaluate(b=>b===window.savedButton);
  await page.mouse.up();
  assert(sameButton, 'sampling replaced a pressed button');
  await page.waitForFunction(()=>mock.calls.length===1);
  await feedback.locator('visible=true').waitFor();
  await child.waitForFunction(()=>document.querySelector('.path-feedback').dataset.state==='success');
  assert.deepEqual(await page.evaluate(()=>mock.calls),[{action:'refresh',interface:'wan2'}]);
  assert.match(await feedback.textContent(), /已恢复 1\/1/);
  assert.equal(await ui.locator('.path-row[data-interface="wan3"] .path-feedback').isVisible(),false);
});

test('the selected row shows pending state and duplicate clicks do not resubmit', async t => {
  const {page,child,button,feedback}=await openUI(t);
  await page.evaluate(()=>mock.pending=true);
  await button.click();
  await page.waitForFunction(()=>mock.calls.length===1);
  assert.equal(await button.textContent(),'刷新中…');
  assert(await button.isDisabled());
  await child.evaluate(()=>poll());
  assert.match(await feedback.textContent(),/正在检查/);
  await button.evaluate(b=>b.dispatchEvent(new MouseEvent('click',{bubbles:true})));
  assert.equal(await page.evaluate(()=>mock.calls.length),1);
  await page.evaluate(()=>mock.pending=false);
  await child.waitForFunction(()=>document.querySelector('.path-feedback').dataset.state==='success');
  await child.waitForFunction(()=>!document.querySelector('.path-refresh').disabled);
  assert.equal(await button.textContent(),'刷新');
});

test('a refresh failure stays visible on its row after subsequent sampling', async t => {
  const {page,child,button,feedback}=await openUI(t);
  await page.evaluate(()=>mock.nextResult={ok:false,state:'done',attempted:1,recovered:0,remaining:1,
    details:[{interface:'wan2',code:'missing_credentials'}]});
  await button.click();
  await child.waitForFunction(()=>document.querySelector('.path-feedback').dataset.state==='error');
  await child.evaluate(()=>poll());
  assert(await feedback.isVisible());
  assert.match(await feedback.textContent(),/未配置凭据/);
  assert.equal(await page.evaluate(()=>mock.calls.length),1);
});

test('sampling preserves keyboard focus and explains an already online path', async t => {
  const {page,child,button,feedback}=await openUI(t);
  await page.evaluate(()=>mock.paths[0].state='active');
  await child.evaluate(()=>poll());
  await button.focus();
  await child.evaluate(()=>poll());
  assert(await button.evaluate(b=>document.activeElement===b));
  await button.press('Enter');
  await child.waitForFunction(()=>document.querySelector('.path-feedback').dataset.state==='success');
  assert.match(await feedback.textContent(),/已在线，无需认证/);
  assert.deepEqual(await page.evaluate(()=>mock.calls),[{action:'refresh',interface:'wan2'}]);
});

test('a credential choice fetches only the selected secret and saves to the target without login', async t => {
  const {page,ui}=await openUI(t);
  await ui.locator('.credentials-button[data-interface="wan3"]').click();
  await ui.locator('#credential-source').waitFor({state:'visible'});
  await ui.locator('#credential-source').selectOption('wan2');
  const child=page.frames().find(f=>f.url().includes('/luci-static/resources/hitwh-mwan/'));
  await child.waitForFunction(()=>document.querySelector('#credential-password').value==='fake-secret');
  assert.equal(await ui.locator('#credential-password').getAttribute('type'),'password');
  assert.match(await ui.locator('#credential-source option').last().textContent(),/fake-user.*wan2/);
  assert(!await ui.locator('#credential-source').evaluate(s=>s.innerHTML.includes('fake-secret')));
  assert.deepEqual(await page.evaluate(()=>mock.calls),[]);
  await ui.locator('#credentials-save').click();
  await ui.locator('#credentials-dialog').waitFor({state:'hidden'});
  assert.deepEqual(await page.evaluate(()=>mock.calls),[{action:'credentials-save',interface:'wan3'}]);
  assert.equal(await page.evaluate(()=>mock.submitted[0].mac),undefined);
  assert.equal(await ui.locator('#credential-password').inputValue(),'');
});

test('random add can reuse credentials and closing the dialog discards a late secret response', async t => {
  const {page,ui,child}=await openUI(t);
  await ui.locator('#add-button').click();
  await ui.locator('#add-source').selectOption('wan2');
  await child.waitForFunction(()=>document.querySelector('#add-password').value==='fake-secret');
  assert.equal(await ui.locator('#add-username').inputValue(),'fake-user');
  assert.equal(await ui.locator('#add-password').getAttribute('type'),'password');
  assert.deepEqual(await page.evaluate(()=>mock.calls),[]);
  await ui.locator('#add-dialog [data-close]').first().click();
  await ui.locator('#add-button').click();
  await page.evaluate(()=>mock.delayGets=true);
  await ui.locator('#add-source').selectOption('wan2');
  await page.waitForFunction(()=>mock.pendingReads.length===1);
  await ui.locator('#add-dialog [data-close]').first().click();
  await page.evaluate(()=>mock.pendingReads.splice(0).forEach(resolve=>resolve()));
  await child.waitForFunction(()=>document.querySelector('#add-source').options.length===1);
  assert.equal(await ui.locator('#add-username').inputValue(),'');
  assert.equal(await ui.locator('#add-password').inputValue(),'');
  assert.equal(await ui.locator('#add-source option').count(),1);
  assert.equal(await child.evaluate(()=>localStorage.length+sessionStorage.length),0);
});

test('checking for updates starts only an upgrade job and displays its version result', async t => {
  const {page,ui,child}=await openUI(t);
  await ui.locator('#upgrade-button').click();
  await child.waitForFunction(()=>document.querySelector('#upgrade-status').textContent.includes('没有可安装'));
  assert.deepEqual(await page.evaluate(()=>mock.calls),[{action:'upgrade',interface:undefined}]);
  assert.match(await ui.locator('#upgrade-status').textContent(),/1\.0\.0-6/);
});
