// Use an existing Playwright installation; this check sends no real messages.
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE ?? 'playwright');
import { readFile } from 'node:fs/promises';
import assert from 'node:assert/strict';
const root = new URL('../public/', import.meta.url);
const browser = await chromium.launch({channel:'chrome', headless:true});
try {
const page = await browser.newPage({viewport:{width:390,height:844}});
let demoAsset;
let relayPolicy={enabled:true,scheduleEnabled:false,weekdays:[1,2,3,4,5],start:'09:30',end:'18:00',timezone:'Asia/Shanghai',active:true,updatedAt:1};
const errors=[];page.on('pageerror', e=>errors.push(e.message));
await page.route('http://localhost:4179/**', async route=>{
 const path=new URL(route.request().url()).pathname;
 if(path==='/api/v1/relay-policy') {if(route.request().method()==='PUT'){relayPolicy={...relayPolicy,...route.request().postDataJSON(),active:false,updatedAt:2};}return route.fulfill({contentType:'application/json',body:JSON.stringify(relayPolicy)});}
 if(path==='/api/v1/assets/demo-image') return route.fulfill({contentType:'application/json',body:JSON.stringify(demoAsset)});
 if(path==='/api/v1/assets/expired-image') return route.fulfill({status:404,body:'{}'});
 if(path==='/sw.js') return route.fulfill({contentType:'text/javascript',body:''});
 if(path.startsWith('/api/')) return route.fulfill({status:401,body:'{}'});
 try {await route.fulfill({contentType:path.endsWith('.js')?'text/javascript':path.endsWith('.css')?'text/css':'text/html',body:await readFile(new URL(path==='/'?'index.html':path.slice(1), root))});}catch {await route.fulfill({status:404,body:''});}
});
await page.goto('http://localhost:4179/');
assert.equal(await page.title(), 'Aurora Relay');
await page.waitForFunction(()=>document.querySelector('#status').children.length>0);
await page.evaluate(async()=>{
 const db=await new Promise((resolve,reject)=>{const r=indexedDB.open('aurora-relay-phase0');r.onsuccess=()=>resolve(r.result);r.onerror=reject;});
 await new Promise((resolve,reject)=>{const tx=db.transaction(['state','pairMessages'],'readwrite');tx.objectStore('state').put('demo','activePairId');for(let i=0;i<45;i++)tx.objectStore('pairMessages').put({id:`demo:${i}`,pairId:'demo',messageId:String(i),sender:'演示联系人',body:`演示消息 ${i+1}`,receivedAt:1788576000000+i*60000,deviceId:'demo-device',wechatUserId:0,replyCapable:true,conversationSendCapable:true,direction:i%2?'outgoing':'incoming'});tx.oncomplete=resolve;tx.onerror=reject;});
});
await page.reload();
await page.locator('.conversation-row').click();
await page.locator('.reply-composer input').fill('保留输入草稿');
const checkBottom=async()=>assert.ok(await page.evaluate(()=>Math.abs(document.querySelector('.reply-composer').getBoundingClientRect().bottom-visualViewport.height-visualViewport.offsetTop)<2));
await checkBottom();
await page.screenshot({path:'/tmp/relay-chat-mobile.png'});
await page.setViewportSize({width:390,height:420});await page.waitForTimeout(100);await checkBottom();
assert.ok(await page.locator('.bubble-list').evaluate(e=>e.scrollHeight-e.scrollTop-e.clientHeight<2));
await page.screenshot({path:'/tmp/relay-chat-compact.png'});
await page.evaluate(()=>window.dispatchEvent(new Event('focus')));
await page.waitForTimeout(100);
assert.equal(await page.locator('.reply-composer input').inputValue(),'保留输入草稿');
await page.locator('.bubble-list').evaluate(e=>e.scrollTop=0);
await page.waitForFunction(()=>document.querySelectorAll('.incoming-bubble,.outgoing-bubble').length>=40);
demoAsset = await page.evaluate(async()=>{
 const key=await crypto.subtle.generateKey({name:'AES-GCM',length:256},false,['encrypt','decrypt']);
 const iv=crypto.getRandomValues(new Uint8Array(12));
 const aad='AWR1|A2I_ASSET|demo-image|demo-device|46|1788578760000|0';
 const canvas=document.createElement('canvas');canvas.width=960;canvas.height=1280;const ctx=canvas.getContext('2d');ctx.fillStyle='#07c160';ctx.fillRect(0,0,960,1280);ctx.fillStyle='#fff';ctx.font='32px sans-serif';ctx.fillText('960 x 1280 original bytes',40,80);
 const blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/png'));
 const ct=await crypto.subtle.encrypt({name:'AES-GCM',iv,additionalData:new TextEncoder().encode(aad)},key,await blob.arrayBuffer());
 const encode=bytes=>btoa(String.fromCharCode(...new Uint8Array(bytes))).replaceAll('+','-').replaceAll('/','_').replaceAll('=','');
 const db=await new Promise(resolve=>{const r=indexedDB.open('aurora-relay-phase0');r.onsuccess=()=>resolve(r.result);});
 await new Promise((resolve,reject)=>{const tx=db.transaction(['state','pairMessages'],'readwrite');tx.objectStore('state').put(key,'pair:demo:messageKey');tx.objectStore('state').put('demo-ack','pair:demo:ackToken');
 tx.objectStore('pairMessages').put({id:'demo:46',pairId:'demo',messageId:'46',sender:'演示联系人',body:'[图片]',seq:46,createdAt:1788578760000,receivedAt:1788578760000,deviceId:'demo-device',wechatUserId:0,assets:[{id:'demo-image',kind:'image',width:960,height:1280},{id:'expired-image',kind:'image',width:100,height:80}]});tx.oncomplete=resolve;tx.onerror=reject;});
 return {id:'demo-image',mimeType:'image/png',sourceSha:Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',await blob.arrayBuffer()))).join(','),envelope:{alg:'A256GCM',kid:'phase1-asset',aad,iv:encode(iv),ct:encode(ct)}};
});
await page.reload();
await page.locator('.conversation-row').click();
await page.locator('.bubble-list').evaluate(e=>e.scrollTop=e.scrollHeight);
await page.locator('.open-image').first().click();
assert.ok(await page.locator('#imageDialog').isVisible());
assert.ok((await page.locator('#saveImage').getAttribute('href')).startsWith('blob:'));
await page.waitForFunction(()=>document.querySelector('#fullImage').complete && document.querySelector('#fullImage').naturalWidth===960 && document.querySelector('#fullImage').naturalHeight===1280);
assert.equal(await page.locator('#saveImage').evaluate(async e=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',await (await fetch(e.href)).arrayBuffer()))).join(',')),demoAsset.sourceSha);
await page.screenshot({path:'/tmp/relay-image-viewer.png'});
await page.getByRole('button',{name:'关闭',exact:true}).click();
await page.waitForFunction(()=>!document.querySelector('#fullImage').hasAttribute('src') && !document.querySelector('#saveImage').hasAttribute('href'));
await page.getByText('图片已过期或不可用',{exact:true}).waitFor();
await page.locator('.conversation-back').click();
await page.locator('.conversation-row').waitFor();
await page.getByRole('button',{name:'设置',exact:true}).click();
assert.ok(await page.locator('#settingsDialog').isVisible());
await page.locator('#relayScheduleEnabled').check();
await page.getByRole('button',{name:'保存转发设置',exact:true}).click();
await page.getByText('当前按计划暂停, 18:00 后恢复.',{exact:true}).waitFor();
await page.getByRole('button',{name:'完成',exact:true}).click();
await page.setViewportSize({width:1280,height:900});
await page.screenshot({path:'/tmp/relay-list-desktop.png'});
assert.deepEqual(errors,[]);console.log('PASS: identity, render, composer bottom at 390x844/390x420, draft refresh, older history, back, relay settings, desktop, encrypted image rendering/viewer/download, expired image status, no page errors');
} finally { await browser.close(); }
