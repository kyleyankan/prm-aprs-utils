const { chromium } = require('playwright');
const FAKE = `window.L=(function(){
 function layer(ll){const m={ll,r:0,addTo(){return m},on(){return m},setLatLng(x){m.ll=x;return m},setRadius(r){m.r=r;return m},getLatLng(){return {lat:m.ll[0],lng:m.ll[1]}}};return m}
 return {map(){return {on(){},setView(){return this},removeLayer(){}}},tileLayer(){return {addTo(){}}},
  layerGroup(){return {addTo(){return this},clearLayers(){}}},marker:layer,circle:(ll,o)=>{const c=layer(ll);return c},circleMarker:layer}})();`;
(async()=>{
 const b=await chromium.launch(); const p=await b.newPage({viewport:{width:1400,height:900}}); const errs=[];
 p.on('pageerror',e=>errs.push(String(e))); p.on('console',m=>{if(m.type()==='error')errs.push(m.text())});
 p.on('dialog',d=>d.accept());
 await p.route('**/unpkg.com/**',r=>r.request().url().endsWith('.js')?r.fulfill({contentType:'text/javascript',body:FAKE}):r.fulfill({contentType:'text/css',body:''}));
 await p.goto('http://127.0.0.1:8778/');
 await p.waitForFunction(()=>document.getElementById('pinfo').textContent.includes('px'));
 console.log('first preview:',await p.textContent('#pinfo'),'|',await p.inputValue('#url'));
 console.log('status:',await p.textContent('#status'));
 await p.click('#fmt button[data-v=mono]'); await p.selectOption('#size','272x792'); await p.fill('#lat','39.95'); await p.fill('#lon','-75.10'); await p.dispatchEvent('#lon','change');
 await p.$eval('#miles',e=>{e.value='12';e.dispatchEvent(new Event('input'))});
 await p.waitForFunction(()=>document.getElementById('pinfo').textContent.includes('272 × 792'));
 const u=await p.inputValue('#url'); console.log('mono url:',u);
 if(u.includes('roads=3')||u.includes('symbols=1')||!u.includes('/eink.png?')||!u.includes('w=272')||!u.includes('miles=12')) throw new Error('bad url');
 const img=await p.$eval('#preview',e=>[e.naturalWidth,e.naturalHeight]); console.log('preview img',img);
 console.log('outside warning shown:',await p.isVisible('#outside'));
 await p.fill('#pname','kitchen-eink'); await p.click('button:has-text("Save")');
 await p.waitForSelector('#plist .p b'); console.log('presets:',await p.$$eval('#plist .p',e=>e.map(x=>x.innerText.replace(/\s+/g,' '))));
 await p.waitForFunction(()=>document.getElementById('status').textContent.includes('2 area'),null,{timeout:8000});
 console.log('status after save:',await p.textContent('#status'),'| warning now:',await p.isVisible('#outside'));
 await p.click('#fmt button[data-v=colour]'); await p.selectOption('#size','800x480');
 await p.click('#plist button:has-text("Load")'); 
 console.log('after load:',await p.inputValue('#url'));
 if(!(await p.inputValue('#url')).includes('/eink.png?')) throw new Error('preset not loaded');
 await p.screenshot({path:'ui/builder.png'});
 await p.click('#plist button:has-text("Delete")'); await p.waitForSelector('#plist .hint');
 await p.click('#theme'); await p.screenshot({path:'ui/builder-dark.png'});
 console.log('errors',JSON.stringify(errs)); await b.close(); if(errs.length) process.exit(1);
})().catch(e=>{console.error('FAIL',e);process.exit(1)});
