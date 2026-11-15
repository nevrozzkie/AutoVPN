const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync('app/static/pwa/push.js', 'utf8');
const settle = async () => { for (let i=0;i<12;i++) await new Promise(setImmediate); };
function page(options={}) {
  let click, permissionCalls=0, subscribeCalls=0, enabled=!!options.enabled;
  const status={textContent:''}, button={hidden:true, addEventListener:(_,fn)=>click=fn};
  const root={dataset:{pushBase:'/client/example/push'},querySelector:selector=>selector==='[data-push-toggle]'?button:status};
  const sub={endpoint:'https://fcm.googleapis.com/test',toJSON(){return {endpoint:this.endpoint,keys:{}}},unsubscribe:async()=>{if(options.unsubscribeFails)throw Error('failure');return true;}};
  const manager={getSubscription:async()=>options.existing?sub:null,subscribe:async()=>{subscribeCalls++;return sub;}};
  const context={document:{querySelector:()=>root}, window:{isSecureContext:true,PushManager:{},Notification:{},matchMedia:()=>({matches:!options.iosBrowser})},
    navigator:{userAgent:options.iosBrowser?'iPhone':'Test',serviceWorker:{register:async()=>({}),ready:Promise.resolve({pushManager:manager})}},
    Notification:{permission:options.permission||'default',requestPermission:async()=>{permissionCalls++;if(options.permissionFails)throw Error('denied API');return options.answer||'granted';}},
    AbortController, setTimeout,clearTimeout, Uint8Array, atob,
    fetch:async(url,init)=>{if(options.offline)throw Error('offline');
      if(url.endsWith('/status'))return {ok:true,json:async()=>({enabled})};
      if(url.endsWith('/unsubscribe'))enabled=false;
      else if(init.method==='POST')enabled=true;
      return {ok:true,json:async()=>({publicKey:'BA'+'A'.repeat(85)})};
    }};
  if(options.unsupported) delete context.window.PushManager;
  vm.runInNewContext(source,context);
  return {button,status,click:()=>click(),counts:()=>({permissionCalls,subscribeCalls,enabled})};
}
test('opening the page never requests permission or subscribes',async()=>{
  const p=page();await settle();assert.equal(p.button.textContent,'Включить уведомления');
  assert.deepEqual(p.counts(),{permissionCalls:0,subscribeCalls:0,enabled:false});
  await p.click();assert.deepEqual(p.counts(),{permissionCalls:1,subscribeCalls:1,enabled:true});
  assert.equal(p.button.textContent,'Выключить уведомления');
  await p.click();assert.equal(p.counts().enabled,false);
});
test('permission denied or requestPermission rejecting does not escape handler',async()=>{
  for(const options of [{answer:'denied'},{permissionFails:true}]){
    const p=page(options);await settle();await p.click();
    assert.equal(p.counts().enabled,false);assert.equal(p.counts().subscribeCalls,0);assert.equal(p.button.disabled,false);
  }
});
test('unsupported browser and iPhone outside home screen remain usable',async()=>{
  for(const options of [{unsupported:true},{iosBrowser:true}]){
    const p=page(options);await settle();assert.equal(p.button.hidden,true);assert.equal(p.counts().permissionCalls,0);assert.ok(p.status.textContent);
  }
});
test('network failure is contained with explicit retry and no permission prompt',async()=>{
  const p=page({offline:true});await settle();assert.equal(p.button.hidden,false);
  await p.click();assert.equal(p.button.disabled,false);assert.equal(p.counts().permissionCalls,0);
});
test('failed local unsubscribe cannot silently opt in on next page load',async()=>{
  const p=page({existing:true,enabled:true,permission:'granted',unsubscribeFails:true});await settle();
  assert.equal(p.button.textContent,'Выключить уведомления');await p.click();assert.equal(p.counts().enabled,false);
  const reloaded=page({existing:true,enabled:false,permission:'granted'});await settle();
  assert.equal(reloaded.button.textContent,'Включить уведомления');assert.equal(reloaded.counts().subscribeCalls,0);assert.equal(reloaded.counts().enabled,false);
});
const workerSource=fs.readFileSync('app/static/pwa/service-worker.js','utf8');
test('service worker shows a neutral notification and rejects external click URLs',async()=>{
  const events={},shown=[],opened=[];
  const self={location:{origin:'https://panel.test'},addEventListener:(name,fn)=>events[name]=fn,
    registration:{showNotification:async(...args)=>shown.push(args)},clients:{matchAll:async()=>[],openWindow:async(url)=>opened.push(url)}};
  vm.runInNewContext(workerSource,{self,URL,Response});
  let pending;events.push({data:{json:()=>({title:'Уведомление',body:'Айпи был сменён. Не забудьте обновить конфиги',tag:'notice-1',url:'/client/example'})},waitUntil:p=>pending=p});
  await pending;assert.equal(shown[0][0],'Уведомление');assert.equal(shown[0][1].renotify,false);
  events.notificationclick({notification:{close(){},data:{url:'/client/example'}},waitUntil:p=>pending=p});
  await pending;assert.deepEqual(opened,['https://panel.test/client/example']);
  events.notificationclick({notification:{close(){},data:{url:'https://evil.test'}},waitUntil:p=>pending=p});
  assert.equal(opened.length,1);
});
