const { chromium } = require('playwright');
const FAKE = `window.L=(function(){
 function layer(ll){const m={ll,addTo(){m.on_=1;return m},on(){return m},setLatLng(x){m.ll=x;return m},setRadius(){return m},getLatLng(){return {lat:m.ll[0],lng:m.ll[1]}}};return m}
 return {map(){return {on(){},setView(ll){window.__view=ll;return this},removeLayer(l){l.on_=0}}},tileLayer(){return {addTo(){}}},
  layerGroup(){return {addTo(){return this},clearLayers(){}}},marker:layer,circle:layer,circleMarker:(ll)=>{const m=layer(ll);window.__dot=m;return m}}})();`;
(async()=>{
 const b=await chromium.launch(); const p=await b.newPage({viewport:{width:1400,height:900}}); const errs=[];
 p.on('pageerror',e=>errs.push(String(e))); p.on('console',m=>{if(m.type()==='error')errs.push(m.text())});
 await p.route('**/unpkg.com/**',r=>r.request().url().endsWith('.js')?r.fulfill({contentType:'text/javascript',body:FAKE}):r.fulfill({contentType:'text/css',body:''}));
 await p.goto('http://127.0.0.1:8782/'); await p.waitForFunction(()=>document.getElementById('pinfo').textContent.includes('px'));
 console.log('status:',await p.textContent('#status'));
 await p.fill('#follow','k3xyz-9');
 await p.waitForFunction(()=>document.getElementById('followinfo').textContent.startsWith('Following'),null,{timeout:10000});
 console.log('info:',await p.textContent('#followinfo'));
 console.log('hint:',await p.textContent('#centrehint'));
 console.log('url:',await p.inputValue('#url'));
 console.log('dot on map:',await p.evaluate(()=>[window.__dot.on_,window.__dot.ll]),'| view:',await p.evaluate(()=>window.__view));
 if(!(await p.inputValue('#url')).includes('follow=K3XYZ-9')) throw new Error('no follow in url');
 await p.waitForFunction(()=>document.getElementById('pinfo').textContent.includes('px'));
 await p.fill('#follow','W1AW'); await p.waitForFunction(()=>document.getElementById('followinfo').textContent.includes('not been heard'),null,{timeout:10000});
 console.log('info:',await p.textContent('#followinfo'),'| dot:',await p.evaluate(()=>window.__dot.on_));
 await p.fill('#follow','bad call!'); await p.waitForTimeout(1200); console.log('info:',await p.textContent('#followinfo'),'| url has follow:',(await p.inputValue('#url')).includes('follow'));
 await p.fill('#follow','K3XYZ-9'); await p.waitForTimeout(1500);
 await p.fill('#pname','car'); await p.click('button:has-text("Save")'); await p.waitForSelector('#plist .p b');
 await p.fill('#follow',''); await p.waitForTimeout(1000);
 await p.click('#plist button:has-text("Load")'); console.log('after loading preset, follow =',await p.inputValue('#follow'));
 await p.waitForTimeout(1500); await p.screenshot({path:'builder.png'});
 console.log('errors',JSON.stringify(errs)); await b.close(); if(errs.length) process.exit(1);
})().catch(e=>{console.error('FAIL',e);process.exit(1)});
