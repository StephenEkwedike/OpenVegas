/* Local Chrome CDP pipe + optional installed FFmpeg. No npm dependencies. */
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {spawn} from 'node:child_process';
import assert from 'node:assert/strict';
import {root,serve} from './serve.mjs';
import {campaigns,slides} from './content.mjs';
const chrome=process.env.CHROME_PATH||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const output=path.join(root,'exports');await fs.mkdir(output,{recursive:true});
const profile=await fs.mkdtemp(path.join(os.tmpdir(),'openvegas-marketing-private-'));
const server=await serve();const base=`http://127.0.0.1:${server.address().port}`;
const browser=spawn(chrome,['--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check','--disable-background-networking','--disable-component-update','--disable-sync','--metrics-recording-only','--hide-scrollbars',`--user-data-dir=${profile}`,'--remote-debugging-pipe'],{stdio:['ignore','ignore','pipe','pipe','pipe']});
let id=0,buffer='',stderr='';const pending=new Map(), requests=new Set();
browser.stderr.on('data',b=>stderr=(stderr+b).slice(-4000));
browser.on('error',e=>{for(const {reject} of pending.values())reject(e)});
browser.stdio[4].on('data',chunk=>{
 buffer+=chunk.toString();let boundary;
 while((boundary=buffer.indexOf('\0'))>=0){const raw=buffer.slice(0,boundary);buffer=buffer.slice(boundary+1);if(!raw)continue;const message=JSON.parse(raw);
 if(message.method==='Network.requestWillBeSent')requests.add(message.params.request.url);
 if(message.method==='Fetch.requestPaused'){
  const {requestId,request}=message.params;
  void call(request.url.startsWith(base+'/')?'Fetch.continueRequest':'Fetch.failRequest',{requestId,...(request.url.startsWith(base+'/')?{}:{errorReason:'BlockedByClient'})},message.sessionId);
 }
 const job=pending.get(message.id);if(job){pending.delete(message.id);clearTimeout(job.timer);message.error?job.reject(Error(JSON.stringify(message.error))):job.resolve(message.result)}}
});
function call(method,params={},sessionId){return new Promise((resolve,reject)=>{const key=++id;const timer=setTimeout(()=>{pending.delete(key);reject(Error(`CDP timeout ${method}: ${stderr}`))},20000);pending.set(key,{resolve,reject,timer});browser.stdio[3].write(JSON.stringify({id:key,method,params,...(sessionId?{sessionId}:{})})+'\0')})}
let session;
async function evaluate(expression){const result=await call('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true},session);if(result.exceptionDetails)throw Error(JSON.stringify(result.exceptionDetails));return result.result.value}
async function size(width,height,mobile=false){await call('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile},session)}
async function open(slide,format='slide'){
 const height=format==='reel'?1920:1350;await size(1080,height);
 const url=`${base}/${slide.id}.html?format=${format}`;
 await call('Page.navigate',{url},session);
 await evaluate(`new Promise((resolve,reject)=>{const start=Date.now();const check=()=>{if(location.href===${JSON.stringify(url)}&&window.OpenVegasCampaign)window.OpenVegasCampaign.ready.then(resolve,reject);else if(Date.now()-start>10000)reject(Error('Page did not initialize'));else setTimeout(check,20)};check()})`);
 return height;
}
async function seek(ms){return evaluate(`window.OpenVegasCampaign.seek(${ms})`)}
async function capture(height,width=1080,y=0){await evaluate('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))');const {data}=await call('Page.captureScreenshot',{format:'png',captureBeyondViewport:true,clip:{x:0,y,width,height,scale:1}},session);return Buffer.from(data,'base64')}
async function layoutCheck(height){return evaluate(`(()=>{
 const copy=[...document.querySelectorAll('.brand,.edition,.headline,.bottom,.scene span,.stamp,.route-label,.station,.path-label,.choice-label')];
 const overflow=copy.filter(n=>{const r=n.getBoundingClientRect();return r.left<22||r.right>1058||r.top<35||r.bottom>${height-35}}).map(n=>({class:n.className,text:n.textContent}));
 const clipped=[...document.querySelectorAll('h1,.lede,.bottom,.edition,.route-label,.station')].filter(n=>n.scrollWidth>n.clientWidth+2).map(n=>n.className);
 const h=document.querySelector('.headline').getBoundingClientRect(),f=document.querySelector('.bottom').getBoundingClientRect();
 return {overflow,clipped,headlineBottom:h.bottom,footerTop:f.top,fonts:{sans:document.fonts.check('700 40px "Space Grotesk"'),regular:document.fonts.check('400 34px "Space Grotesk"'),mono:document.fonts.check('400 24px "IBM Plex Mono"')},color:getComputedStyle(document.querySelector('h1')).color};})()`)}
const report={generatedAt:new Date().toISOString(),dimensions:[],checks:[],sportsSlots:[],videos:[],releaseReady:false};
const videoArg=process.argv.find(a=>a==='--video'||a.startsWith('--video='));
const videoCampaigns=videoArg?.includes('=')?videoArg.split('=')[1].split(','):campaigns.map(c=>c.id);
const videos=Boolean(videoArg);
try{report.videos=JSON.parse(await fs.readFile(path.join(output,'verification.json'),'utf8')).videos||[]}catch{}
try{
 await call('Browser.getVersion');const {targetId}=await call('Target.createTarget',{url:'about:blank'});
 session=(await call('Target.attachToTarget',{targetId,flatten:true})).sessionId;
 await call('Page.enable',{},session);await call('Runtime.enable',{},session);await call('Network.enable',{},session);
 await call('Fetch.enable',{patterns:[{urlPattern:'*'}]},session);
 for(const format of ['slide','reel'])for(const slide of slides){
  const height=await open(slide,format);
  const initial=await evaluate('window.OpenVegasCampaign.snapshot()');
  const t=slide.id==='sports-01'?6250:slide.id==='sports-02'?5700:slide.id==='sports-03'?8200:420;
  const before=await seek(t);const png1=await capture(height);await seek(1120);await seek(t);const png2=await capture(height);
  if(!png1.equals(png2)){
   await fs.writeFile(path.join(output,'debug-before.png'),png1);
   await fs.writeFile(path.join(output,'debug-after.png'),png2);
   console.log({before,after:await evaluate('window.OpenVegasCampaign.snapshot()')});
  }
  assert(png1.equals(png2),`${slide.id}: non-deterministic seek`);
  assert.equal(png2.readUInt32BE(16),1080);assert.equal(png2.readUInt32BE(20),height);
  for(const theme of ['light','dark']){
   await evaluate(`window.OpenVegasCampaign.setTheme('${theme}')`);
   const check=await layoutCheck(height);
   assert(Object.values(check.fonts).every(Boolean),'Exact fonts missing');
   assert.deepEqual(check.overflow,[],`${slide.id} ${format} ${theme}: copy outside safe bounds`);
   assert.deepEqual(check.clipped,[],`${slide.id}: text overflow`);
   assert.equal(check.color,theme==='dark'?'rgb(245, 245, 245)':'rgb(21, 25, 34)');
  }
  await fs.writeFile(path.join(output,`${slide.id}-${format}.png`),png2);
  report.dimensions.push({file:`${slide.id}-${format}.png`,width:1080,height,timeMs:t});
  if(format==='slide')report.sportsSlots.push(...before.sportsSlots);
  console.log(`Rendered ${slide.id}-${format}.png`);
 }
 // Inspect the authored completion boundary, not just the cover pose.
 await open(slides.find(s=>s.id==='sports-02'),'reel');
 const completeEnd=4000+(await evaluate('window.OpenVegasCampaign.snapshot()')).sportsSlots[0].completeDurationMs;
 for(const ms of [3999,4000,4900,completeEnd-1,completeEnd,completeEnd+200]){
  const state=await seek(ms);await fs.writeFile(path.join(output,`sports-keyframe-${ms}.png`),await capture(1920));
  (report.lifecycle??=[]).push(state);
 }
 // Contact sheets use the actual rendered native PNGs, not alternate mockups.
 for(const format of ['slide','reel']){
  const tall=format==='reel',thumbH=tall?640:450;
  let sheet=`<!doctype html><html><head><meta charset="utf-8"><link rel="stylesheet" href="../campaign.css"><style>body{width:1200px;background:#f6f7fb;padding:36px;color:#151922}h1{font-size:40px;letter-spacing:-1.7px;line-height:1.15;margin-bottom:12px}p{font:16px/1.5 "IBM Plex Mono"}section{margin-top:24px}h2{font-size:23px;margin:0 0 10px}.row{display:flex;gap:18px}.row img{width:360px;height:${thumbH}px;display:block;border:1px solid #d3d9e5}small{font:12px "IBM Plex Mono";display:block;margin-top:6px}</style></head><body><h1>[ OPENVEGAS ] / Four Campaigns</h1><p>${tall?'1080 x 1920':'1080 x 1350'} native exports / REVIEW ONLY<br>Original humans-only sports art. Final art, terminal, and claims review pending.</p>`;
  for(const c of campaigns)sheet+=`<section><h2>${c.name}</h2><div class="row">${slides.filter(s=>s.campaign===c.id).map(s=>`<div><img src="${s.id}-${format}.png" alt="${s.id}"><small>${s.id}</small></div>`).join('')}</div></section>`;
  sheet+='</body></html>';const file=`contact-sheet-${format}.html`;await fs.writeFile(path.join(output,file),sheet);
  await size(1200,1000);await call('Page.navigate',{url:base+'/exports/'+file},session);
  await evaluate(`new Promise(resolve=>{const check=()=>{if(location.pathname.endsWith('${file}')&&document.images.length===12)Promise.all([...document.images].map(i=>i.decode())).then(()=>document.fonts.ready).then(resolve);else setTimeout(check,30)};check()})`);
  const height=await evaluate('document.body.scrollHeight');await fs.writeFile(path.join(output,`contact-sheet-${format}.png`),await capture(height,1200));
 }
 // Mobile preview checks run against the real gallery.
 await size(390,844,true);await call('Page.navigate',{url:base+'/'},session);
 await evaluate(`new Promise(resolve=>{const check=()=>{if(location.pathname==='/'&&document.querySelectorAll('iframe').length===12&&[...document.querySelectorAll('iframe')].every(f=>f.contentWindow.OpenVegasCampaign))Promise.all([...document.querySelectorAll('iframe')].map(f=>f.contentWindow.OpenVegasCampaign.ready)).then(resolve);else setTimeout(check,30)};check()})`);
 assert.equal(await evaluate('document.documentElement.scrollWidth<=390'),true,'Mobile gallery overflow');
 await fs.writeFile(path.join(output,'mobile-preview.png'),await capture(844,390));
 await evaluate("window.scrollTo(0,document.querySelector('.viewport').getBoundingClientRect().top+window.scrollY-16)");
 await fs.writeFile(path.join(output,'mobile-composition.png'),await capture(844,390,await evaluate('window.scrollY')));
 await evaluate("document.querySelector('#format').click()");
 await evaluate('new Promise(resolve=>requestAnimationFrame(resolve))');
 assert.equal(await evaluate("[...document.querySelectorAll('iframe')].every(f=>f.contentWindow.OpenVegasCampaign.snapshot().height===1920)"),true,'Preview format controls');
 assert.equal(await evaluate('document.documentElement.scrollWidth<=390'),true,'Mobile reel gallery overflow');
 await evaluate("window.scrollTo(0,document.querySelector('.viewport').getBoundingClientRect().top+window.scrollY-16)");
 await fs.writeFile(path.join(output,'mobile-reel-composition.png'),await capture(844,390,await evaluate('window.scrollY')));
 if(videos){
  for(const campaign of campaigns.filter(c=>videoCampaigns.includes(c.id))){
   const fps=15,total=270,frames=path.join(output,`frames-${campaign.id}`);await fs.mkdir(frames,{recursive:true});const scenes=slides.filter(s=>s.campaign===campaign.id);
   for(let i=0;i<total;i++){
    const n=Math.floor(i/(fps*6)),local=i%(fps*6);if(local===0)await open(scenes[n],'reel');
    // Each sports segment contains the full 5.4-second completion plus its idle tail.
    await seek(local*1000/fps+(campaign.id==='sports'?4000:0));
    await fs.writeFile(path.join(frames,`${String(i).padStart(5,'0')}.png`),await capture(1920));
   }
   const file=`openvegas-${campaign.id}-reel.mp4`;
   await new Promise((resolve,reject)=>{const ffmpeg=spawn(process.env.FFMPEG_PATH||'ffmpeg',['-y','-v','error','-framerate',String(fps),'-i',path.join(frames,'%05d.png'),'-c:v','libx264','-preset','fast','-crf','19','-pix_fmt','yuv420p','-movflags','+faststart',path.join(output,file)],{stdio:'inherit'});ffmpeg.on('error',reject);ffmpeg.on('exit',code=>code===0?resolve():reject(Error(`FFmpeg failed ${code}`)))});
   report.videos=report.videos.filter(v=>v.file!==file);
   report.videos.push({file,width:1080,height:1920,fps:15,duration:18,codec:'H.264',audio:'silent'});
   await fs.rm(frames,{recursive:true,force:true});console.log(`Encoded ${file}`);
  }
 }
 const external=[...requests].filter(url=>!url.startsWith(base+'/'));assert.deepEqual(external,[],'External runtime request');
 report.checks=['24 native-size PNG exports','48 format/theme text-bound and exact-font checks','24 repeat-seek byte-identity checks','6 completion lifecycle keyframes','2 contact sheets from actual exports','390px mobile gallery: no horizontal overflow','all runtime requests local; external requests blocked'];
 await fs.writeFile(path.join(output,'verification.json'),JSON.stringify(report,null,2)+'\n');console.log(JSON.stringify(report));
}finally{
 try{await call('Browser.close')}catch{}browser.kill();server.close();
 await new Promise(resolve=>setTimeout(resolve,200));await fs.rm(profile,{recursive:true,force:true,maxRetries:3,retryDelay:100});
}
