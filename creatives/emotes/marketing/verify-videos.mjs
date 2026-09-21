import fs from 'node:fs/promises';
import path from 'node:path';
import {spawnSync} from 'node:child_process';
import assert from 'node:assert/strict';
import {root} from './serve.mjs';
import {campaigns} from './content.mjs';
const output=path.join(root,'exports'),report=[];
for(const c of campaigns){
 const file=`openvegas-${c.id}-reel.mp4`,input=path.join(output,file);
 const probe=spawnSync('ffprobe',['-v','error','-show_streams','-show_format','-of','json',input],{encoding:'utf8'});
 assert.equal(probe.status,0,probe.stderr);const data=JSON.parse(probe.stdout);const v=data.streams.find(s=>s.codec_type==='video');
 assert.equal(v.width,1080);assert.equal(v.height,1920);assert.equal(v.codec_name,'h264');assert.equal(v.pix_fmt,'yuv420p');assert.equal(v.r_frame_rate,'15/1');assert.equal(Number(data.format.duration),18);
 const times=c.id==='sports'?[2.2,7.7,14.2,17.7]:[9];const frames=[];
 for(const t of times){
  const frame=`${c.id}-video-keyframe-${String(t).replace('.','_')}.png`;
  const r=spawnSync('ffmpeg',['-y','-v','error','-ss',String(t),'-i',input,'-frames:v','1',path.join(output,frame)],{encoding:'utf8'});
  assert.equal(r.status,0,r.stderr);frames.push({timeSeconds:t,file:frame});
 }
 report.push({file,probe:data,keyframes:frames});console.log(`Verified ${file}; extracted ${frames.length} review frames.`);
}
await fs.writeFile(path.join(output,'video-probe.json'),JSON.stringify(report,null,2)+'\n');
