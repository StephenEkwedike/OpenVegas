import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {spawnSync} from 'node:child_process';
import {root} from '../serve.mjs';
import {slides,campaigns} from '../content.mjs';
for(const format of ['slide','reel'])test(`All 12 ${format} PNGs are native dimensions`,async()=>{
 for(const s of slides){const b=await fs.readFile(path.join(root,'exports',`${s.id}-${format}.png`));assert.equal(b.subarray(1,4).toString(),'PNG');assert.equal(b.readUInt32BE(16),1080);assert.equal(b.readUInt32BE(20),format==='slide'?1350:1920)}
});
test('Browser verification completed for every format and theme',async()=>{
 const r=JSON.parse(await fs.readFile(path.join(root,'exports/verification.json'),'utf8'));
 assert.equal(r.dimensions.length,24);assert.equal(r.lifecycle.length,6);assert(r.checks.includes('48 format/theme text-bound and exact-font checks'));assert(r.checks.includes('24 repeat-seek byte-identity checks'));
 assert.equal(r.releaseReady,false);assert.equal(r.sportsSlots.length,3);
 assert(r.sportsSlots.every(s=>!s.placeholder&&s.completeDurationMs===5400));
 assert.deepEqual(r.lifecycle.map(s=>s.clips[0]),['waiting','complete','complete','complete','idle','idle']);
 assert.deepEqual(r.lifecycle.map(s=>s.frames.slice(1)),Array(6).fill([0,3,5,10]));
});
test('Both contact sheets and mobile preview exist',async()=>{
 for(const name of ['contact-sheet-slide.png','contact-sheet-reel.png','mobile-preview.png'])assert((await fs.stat(path.join(root,'exports',name))).size>10000);
});
for(const c of campaigns)test(`${c.id}: actual encoded reel properties`,async()=>{
 const file=path.join(root,'exports',`openvegas-${c.id}-reel.mp4`);
 const r=spawnSync('ffprobe',['-v','error','-select_streams','v:0','-show_entries','stream=codec_name,width,height,r_frame_rate,pix_fmt:format=duration','-of','json',file],{encoding:'utf8'});
 assert.equal(r.status,0,r.stderr);const p=JSON.parse(r.stdout);assert.equal(p.streams[0].codec_name,'h264');assert.equal(p.streams[0].width,1080);assert.equal(p.streams[0].height,1920);assert.equal(p.streams[0].r_frame_rate,'15/1');assert.equal(p.streams[0].pix_fmt,'yuv420p');assert.equal(Number(p.format.duration),18);
});
