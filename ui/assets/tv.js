import {money} from './currency.js';
import {apiFetch, bootstrapBrowserSession} from "/ui/assets/page-auth.js?v=20260330";
import {paintBureau} from '/ui/assets/tv-art-preview.js?v=20261029';

const el = (id) => document.getElementById(id);
const canvas = el("screen"), ctx = canvas.getContext("2d");
let catalog, episode, uploaded, playhead = 0, playing = false, lastTick = 0, raf;
let submissionKey, submissionText, pending = false, selectedChannel;
let uploadGeneration = 0, uploadController, uploadState = "empty";
const bureauArt=new Image();
bureauArt.onload=()=>{if(episode) draw();};
bureauArt.onerror=()=>{bureauArt.dataset.failed='true';if(episode) draw();};
bureauArt.src='/ui/assets/tv/bureau-approved.png';
const illustrated=()=>episode?.id==='bureau-pilot' && episode!==uploaded;
const reducedMotion=window.matchMedia('(prefers-reduced-motion: reduce)');
const time = (s) => `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

async function read(response) {
  if (!response.ok) {
    const messages = {401:"Sign in to submit or view your applications.",403:"This action requires verified admin access.",404:"TV preview is not enabled on this backend.",409:"This application changed or was already reviewed. Refresh before retrying.",413:"The file is too large. Use an animatic smaller than 1 MiB.",422:"Check the format, channel and required fields.",429:"TV is busy or a request limit was reached. Wait and try again; your draft is unchanged.",503:"Creator service is unavailable or intake is closed. Your local draft is still available."};
    throw new Error(messages[response.status] || "TV request failed. No success is assumed.");
  }
  return response.json();
}

function pose(shape, t) {
  if (!shape.keyframes.length) return shape;
  let left = shape.keyframes[0], right = shape.keyframes.at(-1);
  for (const point of shape.keyframes.slice(1)) {
    if (point.at >= t) {right = point; break;}
    left = point;
  }
  const span = right.at - left.at;
  const ratio = span ? Math.max(0, Math.min(1, (t - left.at) / span)) : 0;
  return {...shape, x:left.x + (right.x-left.x)*ratio, y:left.y + (right.y-left.y)*ratio,
    opacity:left.opacity + (right.opacity-left.opacity)*ratio};
}

function draw() {
  if (!episode) return;
  const scene = episode.scenes.find((s) => playhead >= s.start && playhead < s.end) || episode.scenes.at(-1);
  ctx.resetTransform(); ctx.clearRect(0,0,canvas.width,canvas.height);
  if(illustrated()) {
    paintBureau(ctx,bureauArt,playhead,canvas.width,canvas.height,reducedMotion.matches);
  } else {
  ctx.scale(canvas.width / episode.canvas.width, canvas.height / episode.canvas.height);
  ctx.fillStyle = scene.background; ctx.fillRect(0,0,320,180);
  for (const item of scene.shapes) {
    const s = pose(item, playhead - scene.start);
    ctx.save(); ctx.globalAlpha = s.opacity; ctx.fillStyle = s.fill; ctx.strokeStyle = s.stroke; ctx.lineWidth = s.stroke_width;
    if (s.kind === "text") {
      ctx.beginPath(); ctx.rect(s.x,s.y,s.width,s.height); ctx.clip();
      ctx.font = `${s.font_size}px "IBM Plex Mono",monospace`; ctx.textBaseline = "top"; ctx.fillText(s.text,s.x,s.y);
    } else {
      ctx.beginPath();
      if (s.kind === "ellipse") ctx.ellipse(s.x+s.width/2,s.y+s.height/2,s.width/2,s.height/2,0,0,Math.PI*2);
      else ctx.rect(s.x,s.y,s.width,s.height);
      ctx.fill(); if (s.stroke_width) ctx.stroke();
    }
    ctx.restore();
  }
  }
  const caption = episode.captions.find((c) => playhead >= c.start && playhead < c.end);
  el("captions").textContent = caption?.text || (playhead >= episode.duration_seconds ? "End of pilot. Restart or tune to another channel." : "");
  el("scene-description").textContent = illustrated() ? 'Approved painted story panels. Camera motion only; character acting, lip-sync and finished episode animation are still pending.' : scene.description;
  el("clock").textContent = `${time(playhead)} / ${time(episode.duration_seconds)}`;
}

function pause() {playing=false; cancelAnimationFrame(raf); el("play").textContent="Play"; el("play").dataset.ovProductAction="pause";}
function tick(now) {
  if (!playing || !episode) return;
  if (now-lastTick >= 1000/30) {
    playhead = Math.min(episode.duration_seconds,playhead+(now-lastTick)/1000);
    lastTick=now; draw();
    if (playhead >= episode.duration_seconds) {pause(); return;}
  }
  raf=requestAnimationFrame(tick);
}
function play() {
  if (!episode) return;
  if (playing) {pause();return;}
  if (playhead >= episode.duration_seconds) playhead=0;
  playing=true;lastTick=performance.now();el("play").textContent="Pause";el("play").dataset.ovProductAction="play";raf=requestAnimationFrame(tick);
}
function loadEpisode(value) {
  pause(); episode=value;playhead=0;
  el("play").disabled=!value;el("restart").disabled=!value;el("off-air").hidden=!!value;
  el("episode-kind").textContent=uploaded && value===uploaded ? "YOUR LOCAL ANIMATIC / NOT SUBMITTED" : "ORIGINAL PILOT / CAPTIONED ANIMATIC";
  el("episode-title").textContent=value?.title || "A channel waiting for its first show.";
  el("episode-description").textContent=value?.description || "Propose an original series in the creator studio. Nothing is streaming on this channel yet.";
  if(illustrated()) {
    el('episode-kind').textContent='APPROVED ART / ILLUSTRATED STORYBOARD';
    el('episode-description').textContent='The approved orange visitor and teal clerk, in their painted inter-universe office. A silent story-panel preview, not a finished animated episode.';
  }
  if(value) draw(); else {ctx.clearRect(0,0,canvas.width,canvas.height);el("captions").textContent="No show available. No $V spent.";el("scene-description").textContent="";el("clock").textContent="00:00 / 00:00";}
}
function selectChannel(id) {
  selectedChannel=id;
  const channel=catalog.channels.find((c)=>c.id===id);
  for(const button of el("channels").children) button.setAttribute("aria-pressed",String(button.dataset.id===id));
  el("station").textContent=`CH ${id} / ${channel.name.toUpperCase()}`;
  loadEpisode(catalog.episodes.find((e)=>channel.episode_ids.includes(e.id)));
}

function exportJSON(value,name) {
  const url=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)+"\n"],{type:"application/json"}));
  const link=document.createElement("a"); link.href=url;link.download=name;link.click();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
}
function pitch() {
  if(uploadState==="validating" || uploadState==="invalid"){
    el("pitch-status").textContent=uploadState==="validating"
      ? "Wait for animatic validation before exporting or submitting."
      : "Choose a valid animatic or remove it before exporting or submitting.";
    return null;
  }
  if(!el("pitch-form").reportValidity()) return null;
  const value={title:el("pitch-title").value.trim(),channel_id:el("pitch-channel").value,
    pitch:el("pitch-text").value.trim(),rights_confirmed:el("rights").checked,episode:uploaded || null};
  const serialized=JSON.stringify(value);
  if(serialized!==submissionText){submissionKey=crypto.randomUUID();submissionText=serialized;}
  return {idempotency_key:submissionKey,...value};
}

function updatePitchActions() {
  const blocked=uploadState==="validating" || uploadState==="invalid";
  el("export-pitch").disabled=pending || blocked;
  el("submit-pitch").disabled=pending || blocked || !catalog?.studio_intake_enabled;
  el("clear-storyboard").disabled=uploadState==="empty";
  el("storyboard").setAttribute("aria-busy",String(uploadState==="validating"));
}

function clearUploadedPreview() {
  const wasLocal=uploaded && episode===uploaded;
  uploaded=undefined;
  if(wasLocal){
    if(catalog && selectedChannel) selectChannel(selectedChannel);
    else loadEpisode(undefined);
  }
}

function expand(force) {
  const receiver=el("receiver");const active=force ?? !receiver.classList.contains("expanded");
  receiver.classList.toggle("expanded",active);el("expand").setAttribute("aria-expanded",String(active));
  el("expand").textContent=active ? "Contract" : "Expand";
  if(active){receiver.setAttribute("role","dialog");receiver.setAttribute("aria-modal","true");receiver.setAttribute("aria-label","Expanded TV player");}
  else{receiver.removeAttribute("role");receiver.removeAttribute("aria-modal");}
  el("expand").focus();
}

// A separate explicit account action; preview playback never enters billing.
let accessBusy=false, checkoutKey, monthlyActive=false;
function updateAccessControls() {
  el("tv-monthly-checkout").disabled=accessBusy || !catalog?.billing?.checkout_enabled ||
    !catalog?.billing?.delivery_ready || monthlyActive || !el("tv-monthly-consent").checked;
  el("tv-monthly-manage").disabled=accessBusy;
  el("tv-access-refresh").disabled=accessBusy;
}
async function accessRequest(path, body) {
  await bootstrapBrowserSession();
  const response=await apiFetch(path,{method:body?"POST":"GET",cache:"no-store",redirect:"error",
    ...(body?{headers:{"Content-Type":"application/json"},body:JSON.stringify(body)}:{})});
  if(!response.ok)throw new Error(response.status===401 || response.status===403
    ? "Sign in to the existing OpenVegas account to review TV access."
    : "TV billing is closed, unavailable, or needs review. No paid access is assumed; retry the same checkout after checking status.");
  return response.json();
}
el("tv-monthly-consent").addEventListener("change",updateAccessControls);
el("tv-access-refresh").addEventListener("click",async()=>{
  if(accessBusy)return;
  accessBusy=true;updateAccessControls();
  try{
    const access=await accessRequest("/tv/billing/status");
    monthlyActive=access.monthly_active===true;
    el("tv-access-status").textContent=monthlyActive
      ? `Verified unlimited TV pass through ${access.expires_at}. No PAYG debit while this pass grants access.`
      : "No verified active TV pass. No automatic purchase or PAYG fallback.";
    el("tv-access-price").textContent=`PAYG: ${money(access.hourly_v)}/hour (US$0.75/hour). Wallet: ${money(access.wallet_v)}. Confirm a bounded viewing budget before paid playback.`;
  }catch(error){monthlyActive=false;el("tv-access-status").textContent=error.message;}
  finally{accessBusy=false;updateAccessControls();}
});
el("tv-monthly-checkout").addEventListener("click",async()=>{
  if(accessBusy || !catalog?.billing?.checkout_enabled || !catalog?.billing?.delivery_ready ||
    monthlyActive || !el("tv-monthly-consent").checked)return;
  accessBusy=true;updateAccessControls();checkoutKey ||= crypto.randomUUID();
  try{
    const result=await accessRequest("/tv/billing/checkout",{idempotency_key:checkoutKey,confirmed:true});
    const url=new URL(result.url);
    if(url.protocol!=="https:" || url.hostname!=="checkout.stripe.com" || url.username || url.password || url.port)throw new Error("Unsafe checkout redirect refused.");
    window.location.assign(url.href);
  }catch(error){el("tv-access-status").textContent=error.message;}
  finally{accessBusy=false;updateAccessControls();}
});
el("tv-monthly-manage").addEventListener("click",async()=>{
  if(accessBusy)return;
  accessBusy=true;updateAccessControls();
  try{
    const result=await accessRequest("/tv/billing/portal",{idempotency_key:crypto.randomUUID(),confirmed:true});
    const url=new URL(result.url);
    if(url.protocol!=="https:" || url.hostname!=="billing.stripe.com" || url.username || url.password || url.port)throw new Error("Unsafe billing redirect refused.");
    window.location.assign(url.href);
  }catch(error){el("tv-access-status").textContent=error.message;}
  finally{accessBusy=false;updateAccessControls();}
});
el("expand").addEventListener("click",()=>expand());
el("studio-link").addEventListener("click",()=>{
  expand(false);
  el("studio").focus({preventScroll:true});
});
el("receiver").addEventListener("keydown",(event)=>{
  if(!el("receiver").classList.contains("expanded"))return;
  if(event.key==="Escape"){event.preventDefault();expand(false);}
  if(event.key==="Tab"){
    const controls=Array.from(el("receiver").querySelectorAll("button:not(:disabled),a")).filter((e)=>e.offsetParent!==null);
    const index=controls.indexOf(document.activeElement);
    if(event.shiftKey&&index<=0){event.preventDefault();controls.at(-1)?.focus();}
    if(!event.shiftKey&&index===controls.length-1){event.preventDefault();controls[0]?.focus();}
  }
});
el("play").addEventListener("click",play);
el("restart").addEventListener("click",()=>{pause();playhead=0;draw();});
document.addEventListener("visibilitychange",()=>{if(document.hidden)pause();});
el("template").addEventListener("click",()=>exportJSON(catalog.episodes[0],"openvegas-tv-starter.json"));
async function selectStoryboard(file) {
  const generation=++uploadGeneration;
  uploadController?.abort();
  uploadController=undefined;
  clearUploadedPreview();
  uploadState=file ? "validating" : "empty";
  el("pitch-status").textContent="";
  el("validation").textContent=file
    ? "Validating selected animatic. Export and submission are paused."
    : "No animatic attached. You can export or submit a pitch without one.";
  updatePitchActions();
  if(!file)return;
  const controller=new AbortController();
  uploadController=controller;
  try{
    if(file.size>1048576)throw new Error("Use a JSON file smaller than 1 MiB.");
    const text=await file.text();
    if(generation!==uploadGeneration)return;
    let value;try{value=JSON.parse(text);}catch{throw new Error("The file is not valid JSON.");}
    const result=await read(await fetch("/tv/studio/validate",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(value),redirect:"error",signal:controller.signal}));
    // Cancellation is best-effort; only the latest selection may commit state.
    if(generation!==uploadGeneration)return;
    uploaded=result.episode;el("pitch-channel").value=uploaded.channel_id;el("pitch-title").value=uploaded.title;
    el("station").textContent="LOCAL STUDIO / NOT PUBLISHED";loadEpisode(uploaded);
    uploadState="valid";
    el("validation").textContent="Validated. Press Play above to preview. This file has not been submitted or published.";
  }catch(error){
    if(generation!==uploadGeneration)return;
    clearUploadedPreview();uploadState="invalid";
    el("validation").textContent=`${error.message} Choose another file or remove this animatic.`;
  }finally{
    if(generation===uploadGeneration){uploadController=undefined;updatePitchActions();}
  }
}
el("storyboard").addEventListener("change",()=>selectStoryboard(el("storyboard").files[0]));
el("clear-storyboard").addEventListener("click",()=>{
  el("storyboard").value="";
  return selectStoryboard(undefined);
});
el("pitch-form").addEventListener("submit",(event)=>{event.preventDefault();if(pending)return;const value=pitch();if(value){exportJSON(value,"openvegas-tv-pitch.json");el("pitch-status").textContent="Draft exported. Not submitted and not published.";}});
el("submit-pitch").addEventListener("click",async()=>{
  if(pending || !catalog?.studio_intake_enabled)return;
  const value=pitch();if(!value)return;
  const generation=uploadGeneration;pending=true;updatePitchActions();
  try{
    await bootstrapBrowserSession();
    if(generation!==uploadGeneration){el("pitch-status").textContent="Animatic selection changed. Review it before submitting again.";return;}
    const result=await read(await apiFetch("/tv/studio/applications",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(value),redirect:"error",cache:"no-store"}));
    el("pitch-status").textContent=generation===uploadGeneration
      ? `Saved for review: ${result.id}. Not published. No credits charged.`
      : `Earlier draft saved for review: ${result.id}. Current selection not submitted. No credits charged.`;
  }catch(error){el("pitch-status").textContent=error.message;}
  finally{pending=false;updatePitchActions();}
});
el("my-applications").addEventListener("click",async()=>{
  el("application-list").replaceChildren();
  try{
    await bootstrapBrowserSession();const result=await read(await apiFetch("/tv/studio/applications",{cache:"no-store",redirect:"error"}));
    for(const application of result.applications){const item=document.createElement("li");item.textContent=`${application.title}: ${application.status}${application.review_note?" / "+application.review_note:""}`;el("application-list").append(item);}
    if(!result.applications.length)el("pitch-status").textContent="No applications saved for this account.";
  }catch(error){el("pitch-status").textContent=error.message;}
});

try {
  catalog=await read(await fetch("/tv/catalog",{cache:"no-store",redirect:"error"}));
  for(const channel of catalog.channels){
    const button=document.createElement("button");button.type="button";button.className="channel";button.dataset.id=channel.id;button.dataset.ovProductAction="select";button.setAttribute("aria-pressed","false");
    const number=document.createElement("span");number.className="channel-number";number.textContent=channel.id;
    const name=document.createElement("span");name.textContent=channel.name;
    const availability=document.createElement("small");availability.textContent=channel.available?"Pilot available":"No shows yet";
    button.append(number,name,availability);button.addEventListener("click",()=>selectChannel(channel.id));el("channels").append(button);
    const option=document.createElement("option");option.value=channel.id;option.textContent=`${channel.id} / ${channel.name}`;el("pitch-channel").append(option);
  }
  updateAccessControls();
  const ready=catalog.episodes.length;
  el("status").textContent=`Local preview: ${ready} playable animatic${ready===1?"":"s"}, ${catalog.channels.length} channel slots. Not a live broadcast. No $V spent.`;
  el("template").disabled=!ready;updatePitchActions();el("my-applications").disabled=!catalog.studio_intake_enabled;
  if(catalog.studio_intake_enabled)el("intake-status").textContent="Signed-in accounts can submit up to five applications per day. Admin review never auto-publishes a show.";
  selectChannel(catalog.channels[0].id);
}catch(error){el("status").textContent=error.message;el("storyboard").disabled=true;}
