const frames=[...document.querySelectorAll('iframe')];let reel=false,playing=false;
function resize(){frames.forEach(f=>{f.style.height=(reel?1920:1350)+'px';f.style.transform=`scale(${f.parentElement.clientWidth/1080})`;f.parentElement.style.aspectRatio=reel?'9/16':'4/5'})}
window.addEventListener('resize',resize);frames.forEach(f=>f.addEventListener('load',resize));resize();
function each(fn){frames.forEach(f=>{const api=f.contentWindow.OpenVegasCampaign;if(api)api.ready.then(()=>fn(api))})}
document.querySelector('#play').onclick=e=>{playing=!playing;each(a=>playing?a.play():a.pause());e.target.textContent=playing?'Pause':'Play authored frames';e.target.setAttribute('aria-pressed',String(playing))};
document.querySelector('#reset').onclick=()=>{each(a=>a.seek(0));playing=false;const b=document.querySelector('#play');b.textContent='Play authored frames';b.setAttribute('aria-pressed','false')};
document.querySelector('#format').onclick=e=>{reel=!reel;each(a=>a.setFormat(reel?'reel':'slide'));e.target.setAttribute('aria-pressed',String(reel));resize()};
document.querySelector('#theme').onclick=()=>each(a=>a.setTheme(a.snapshot().theme==='dark'?'light':'dark'));
