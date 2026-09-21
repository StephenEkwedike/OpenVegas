import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {root} from './serve.mjs';

for(const pack of ['pixel-courier','beat-maker','visor-explorer'])test(`Unmodified authored pack: ${pack}`,async()=>{
  const base=path.join(root,'assets',pack);
  const manifest=JSON.parse(await fs.readFile(path.join(base,'manifest.json'),'utf8'));
  const png=await fs.readFile(path.join(base,'sheet.png'));
  assert.equal(crypto.createHash('sha256').update(png).digest('hex'),manifest.sha256);
  assert.equal(png.readUInt32BE(16)%manifest.frame.width,0);
  assert.equal(png.readUInt32BE(20)%manifest.frame.height,0);
  assert.equal(manifest.animations.complete.frames.length*manifest.animations.complete.frame_ms,4800);
});
test('Exact local fonts and licenses exist',async()=>{
  for(const font of ['space-grotesk-400.ttf','space-grotesk-700.ttf','ibm-plex-mono-400.ttf','Space-Grotesk-OFL.txt','IBM-Plex-Mono-OFL.txt'])assert((await fs.stat(path.join(root,'assets/fonts',font))).size>1000);
});
test('Editable pages have no remote runtime requests',async()=>{
  for(const file of ['index.html','slide-01.html','slide-02.html','slide-03.html','campaign.css','campaign.js']){
    const source=await fs.readFile(path.join(root,file),'utf8');
    assert(!/(?:src|href)=["']https?:|url\(https?:|fetch\(["']https?:/.test(source));
  }
});
test('Deterministic authored animation, not procedural motion',async()=>{
  const js=await fs.readFile(path.join(root,'campaign.js'),'utf8');
  assert(js.includes('OpenVegasCampaign'));assert(js.includes('drawImage'));assert(!js.includes('Math.random'));
  assert(!(await fs.readFile(path.join(root,'campaign.css'),'utf8')).includes('@keyframes'));
});
for(const format of ['slide','reel'])test(`Native ${format} exports`,async()=>{
  for(let i=1;i<=3;i++){
    const bytes=await fs.readFile(path.join(root,'exports',`${format}-0${i}.png`));
    assert.equal(bytes.readUInt32BE(16),1080);assert.equal(bytes.readUInt32BE(20),format==='slide'?1350:1920);
  }
});
