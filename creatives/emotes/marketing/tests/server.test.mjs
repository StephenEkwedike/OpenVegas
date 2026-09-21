import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import {serve} from '../serve.mjs';

async function withServer(run){
  const server=await serve();
  try{await run(server.address().port)}
  finally{await new Promise((resolve,reject)=>server.close(error=>error?reject(error):resolve()))}
}
function request(port,pathname){
  return new Promise((resolve,reject)=>{
    const req=http.get({hostname:'127.0.0.1',port,path:pathname,agent:false},res=>{
      let body='';
      res.on('data',chunk=>{if(body.length<16384)body+=chunk.toString()});
      res.on('error',reject);
      res.on('end',()=>resolve({status:res.statusCode,type:res.headers['content-type'],body}));
    });
    req.on('error',reject);
    req.setTimeout(5000,()=>req.destroy(Error('Local test request timed out')));
  });
}

test('Malformed URLs return 400 and the same server remains usable',async()=>{
  await withServer(async port=>{
    for(const pathname of ['/%','/%ZZ','/%E0%A4%A','/%C3%28','/%00']){
      const invalid=await request(port,pathname);
      assert.equal(invalid.status,400,pathname);
      assert.equal(invalid.type,'text/plain');
      assert.equal(invalid.body,'Bad Request');
      const valid=await request(port,'/');
      assert.equal(valid.status,200,'Server must survive malformed input');
      assert.match(valid.body,/Four Campaigns/);
    }
  });
});

for(const [pathname,type] of [
  ['/exports/openvegas-sports-reel.mp4','video/mp4'],
  ['/assets/sports/skyline-dunk/complete-light.gif','image/gif'],
  ['/content.mjs','text/javascript'],
])test(`Correct MIME type for ${pathname}`,async()=>{
  await withServer(async port=>{
    const response=await request(port,pathname);
    assert.equal(response.status,200);
    assert.equal(response.type,type);
  });
});
