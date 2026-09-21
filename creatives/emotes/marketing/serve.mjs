import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

export const root=path.dirname(fileURLToPath(import.meta.url));
export function serve(port=0){
  const server=http.createServer((req,res)=>{
    let pathname;
    try{
      pathname=decodeURIComponent(new URL(req.url,'http://localhost').pathname);
      if(pathname.includes('\0'))throw new URIError('Null byte in request path');
    }catch{
      res.writeHead(400,{'Content-Type':'text/plain'});res.end('Bad Request');return;
    }
    const target=path.resolve(root,'.'+(pathname==='/'?'/index.html':pathname));
    if(!target.startsWith(root+path.sep)){res.writeHead(403);res.end();return}
    const types={'.html':'text/html','.js':'text/javascript','.mjs':'text/javascript','.css':'text/css','.json':'application/json','.png':'image/png','.gif':'image/gif','.mp4':'video/mp4','.ttf':'font/ttf','.md':'text/plain'};
    fs.readFile(target,(error,data)=>{if(error){res.writeHead(404);res.end();return}res.setHeader('Content-Type',types[path.extname(target)]||'application/octet-stream');res.setHeader('Cache-Control','no-store');res.end(data)});
  });
  return new Promise((resolve,reject)=>{server.on('error',reject);server.listen(port,'127.0.0.1',()=>resolve(server))});
}
if(process.argv[1]===fileURLToPath(import.meta.url)){
  const server=await serve(Number(process.argv[2]||8765));
  console.log(`Local preview: http://127.0.0.1:${server.address().port}/`);
}
