import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {root,serve} from '../serve.mjs';
import {campaigns,slides,assetNotice,integrationNotice,modelBoundary} from '../content.mjs';
const read=file=>fs.readFile(path.join(root,file),'utf8');
test('Four coherent campaigns with exactly three unique layouts each',()=>{
 assert.equal(campaigns.length,4);assert.equal(slides.length,12);
 assert.equal(new Set(slides.map(s=>s.layout)).size,12);
 for(const c of campaigns){assert.equal(slides.filter(s=>s.campaign===c.id).length,3);assert(c.status.length>40)}
});
for(const s of slides)test(`${s.id}: semantic content, caption, alt text, and local assets`,async()=>{
 const html=await read(s.id+'.html');assert(html.includes(s.title));assert(html.includes(s.lede));assert(html.includes('<h1>'));
 assert(html.includes('figcaption'));assert(html.includes('role="img"'));assert(s.alt.length>80);assert(s.caption.length>100);
 assert(html.includes(s.alt.replaceAll('&','&amp;').replaceAll('"','&quot;').replaceAll('<','&lt;')));
 assert(!/(?:src|href)=["'](?:https?:)?\/\//i.test(html));
 assert(!/\$\s*\d|\b(?:unlimited|guaranteed|3x|reset)\b/i.test([s.title,s.lede,s.footer,s.risk||''].join(' ')));
});
for(const pack of ['pixel-courier','beat-maker','visor-explorer'])test(`Authored original pack integrity: ${pack}`,async()=>{
 const m=JSON.parse(await read(`assets/${pack}/manifest.json`));const b=await fs.readFile(path.join(root,'assets',pack,'sheet.png'));
 assert.equal(crypto.createHash('sha256').update(b).digest('hex'),m.sha256);
 assert.equal(b.readUInt32BE(16)%m.frame.width,0);assert.equal(b.readUInt32BE(20)%m.frame.height,0);
 for(const clip of Object.values(m.animations)){assert(clip.frames.length);assert(clip.frame_ms>0);for(const f of clip.frames)assert(f>=0&&f<b.readUInt32BE(16)/m.frame.width*(b.readUInt32BE(20)/m.frame.height))}
 assert.equal(m.animations.complete.loop,false);
});
test('Bundled brand fonts and original licenses',async()=>{
 for(const file of ['space-grotesk-400.ttf','space-grotesk-700.ttf','ibm-plex-mono-400.ttf','Space-Grotesk-OFL.txt','IBM-Plex-Mono-OFL.txt'])assert((await fs.stat(path.join(root,'assets/fonts',file))).size>1000);
 const css=await read('campaign.css');for(const color of ['#f6f7fb','#151922','#11788a','#1d8c3a','#040404','#f5f5f5','#44c6dd','#7cdf7c'])assert(css.includes(color));
});
test('No remote runtime URL or procedural character synthesis',async()=>{
 for(const file of ['campaign.css','campaign.js','preview.js','index.html'])assert(!/(?:src|href)=["']https?:|url\(["']?https?:|fetch\(["']https?:/.test(await read(file)));
 const js=await read('campaign.js');assert(js.includes('drawImage'));assert(!js.includes('Math.random'));assert(!js.includes('fillRect'));assert(!js.includes('strokeRect'));
 assert(js.includes('prefers-reduced-motion'));assert(js.includes('setFormat'));assert(js.includes('resolveSports'));assert(!(await read('campaign.css')).includes('@keyframes'));
});
test('Sports registry cannot silently alias missing finished art',async()=>{
 const registry=JSON.parse(await read('assets/sports/slots.json'));assert.equal(registry.schema_version,1);
 for(const slide of slides.filter(s=>s.campaign==='sports')){
  const slot=registry.slots[slide.sportsSlot];assert(slot);assert(slot.status);
  assert(slot.pack,'Final humans-only sports art is required');
  if(slot.pack){
   assert.match(slot.pack,/^sports\/[a-z0-9-]+$/);
   const m=JSON.parse(await read(`assets/${slot.pack}/manifest.json`));const png=await fs.readFile(path.join(root,'assets',slot.pack,'sheet.png'));
   assert.equal(crypto.createHash('sha256').update(png).digest('hex'),m.sha256);assert.equal(m.animations.complete.loop,false);
   const duration=m.animations.complete.frames.length*m.animations.complete.frame_ms;assert.equal(duration,5400);
   assert.equal(m.frame.width,160);assert.equal(m.frame.height,120);assert.equal(new Set(m.animations.complete.frames).size,12);
   const provenance=JSON.parse(await read(`assets/${slot.pack}/provenance.json`));assert.equal(provenance.purchasable,false);assert.match(provenance.approval,/pending/);
   await assert.rejects(fs.stat(path.join(root,'assets',slot.pack,'source.png')),e=>e.code==='ENOENT');
   const html=await read(slide.id+'.html');assert.equal((html.match(/data-pose=/g)||[]).length,4);
  }
 }
});
test('Source captions preserve product boundaries and pane requirement',async()=>{
 const content=await read('content.mjs');for(const phrase of ['same-window UX','conditionally approved','backend','provider','lose credits','process completion'])assert(content.toLowerCase().includes(phrase.toLowerCase()));
});
test('Published captions enforce actual integration limits and current mask provenance',async()=>{
 for(const slide of slides){
  assert(slide.caption.includes(assetNotice));
  if(['companions','sports'].includes(slide.campaign))assert(slide.caption.includes(integrationNotice));
  if(slide.campaign==='models')assert(slide.caption.includes(modelBoundary));
 }
 assert(integrationNotice.includes('Gemini hooks are observation-only: no live waiting or completion animation'));
 assert(integrationNotice.includes('Claude hooks are activity-only and never trigger completion celebrations'));
 assert(integrationNotice.includes('explicit structured integration'));
 for(const slug of ['skyline-dunk','bicycle-finish','three-point-glow']){
  const p=JSON.parse(await read(`assets/sports/${slug}/provenance.json`));
  if(p.highlight_reference_sha256){
   assert(p.highlight_reference.includes('transparent white highlights only'));
  }else{
   assert.equal(p.revision,'0.1.1');
   assert.match(p.prepared_source_sha256,/^[a-f0-9]{64}$/);
   assert(p.highlight_reference.startsWith('Not used;'));
  }
  assert.equal(p.purchasable,false);
  const files=await fs.readdir(path.join(root,'assets/sports',slug));
  assert(!files.some(f=>f.includes('reference')||f==='source.png'));
 }
});
