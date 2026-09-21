/* One deterministic authored-frame timeline; no CSS animation or random motion. */
(() => {
  const art = document.querySelector('.art');
  if (!art) return;
  const query = new URLSearchParams(location.search);
  art.dataset.format = query.get('format') === 'reel' ? 'reel' : 'slide';
  if (['dark', 'light'].includes(query.get('theme'))) art.dataset.theme = query.get('theme');
  const sources = new Map();
  let running = false, raf = 0, elapsed = 0, origin = 0;
  const canvases = [...document.querySelectorAll('canvas[data-pack]')];
  const sportsSlots = [];
  async function resolveSports() {
    const targets = canvases.filter(c => c.dataset.sportsSlot);
    if (!targets.length) return;
    const response = await fetch('assets/sports/slots.json');
    if (!response.ok) throw Error('Sports slot registry missing');
    const registry = await response.json();
    for (const canvas of targets) {
      const slug = canvas.dataset.sportsSlot;
      const slot = registry.slots[slug];
      if (!slot) throw Error(`Unregistered sports slot: ${slug}`);
      if (!slot.pack) throw Error(`Missing final humans-only sports pack: ${slug}`);
      const pack = slot.pack;
      if (!/^(?:sports\/)?[a-z0-9-]+$/.test(pack)) throw Error('Sports asset must be a local pack');
      canvas.dataset.pack = pack;
      canvas.setAttribute('aria-label', `${slug} original humans-only authored sports animation`);
      sportsSlots.push({slug, pack, status:slot.status, placeholder:!slot.pack});
    }
  }
  async function load(pack) {
    const base = `assets/${pack}/`;
    const response = await fetch(base + 'manifest.json');
    if (!response.ok) throw Error(`Missing manifest: ${pack}`);
    const manifest = await response.json();
    if (manifest.schema_version !== 1 || manifest.sheet !== 'sheet.png') throw Error('Unexpected manifest');
    const sheetResponse = await fetch(base + manifest.sheet);
    if (!sheetResponse.ok) throw Error(`Missing sheet: ${pack}`);
    const bytes = await sheetResponse.arrayBuffer();
    const digest = [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))].map(b=>b.toString(16).padStart(2,'0')).join('');
    if (digest !== manifest.sha256) throw Error(`Authored sheet hash mismatch: ${pack}`);
    const image = new Image(); image.src = base + manifest.sheet; await image.decode();
    const {width, height} = manifest.frame;
    if (!Number.isInteger(width) || !Number.isInteger(height) || width <= 0 || height <= 0 || image.width % width || image.height % height) throw Error('Invalid sheet geometry');
    const count = image.width / width * (image.height / height);
    for (const name of ['idle','waiting','complete']) {
      const clip = manifest.animations[name];
      if (!clip || !clip.frames.length || clip.frame_ms <= 0 || clip.frames.some(f=>!Number.isInteger(f)||f<0||f>=count)) throw Error(`Invalid authored clip: ${pack}/${name}`);
    }
    if (manifest.animations.complete.loop) throw Error('Completion clip must be bounded');
    sources.set(pack, {manifest, image});
  }
  function draw(ms) {
    for (const canvas of canvases) {
      const {manifest: m, image} = sources.get(canvas.dataset.pack);
      const lifecycle = art.dataset.campaign === 'sports';
      const completeEnd = 4000 + m.animations.complete.frames.length * m.animations.complete.frame_ms;
      const complete = lifecycle && ms >= 4000 && ms < completeEnd;
      const idle = lifecycle && ms >= completeEnd;
      const clip = m.animations[complete ? 'complete' : idle ? 'idle' : 'waiting'];
      canvas.dataset.clip = complete ? 'complete' : idle ? 'idle' : 'waiting';
      if (canvas.dataset.sportsSlot) art.dataset.lifecycle = canvas.dataset.clip;
      const time = complete ? ms - 4000 : ms;
      const position = Math.floor(time / clip.frame_ms) + (complete ? 0 : Number(canvas.dataset.offset || 0));
      const frame = canvas.dataset.pose === undefined ? clip.frames[clip.loop ? position % clip.frames.length : Math.min(position, clip.frames.length - 1)] : Number(canvas.dataset.pose);
      if (!Number.isInteger(frame) || frame < 0 || frame >= image.width / m.frame.width * (image.height / m.frame.height)) throw Error('Invalid authored filmstrip pose');
      const {width, height} = m.frame, columns = image.width / width;
      if (canvas.width !== width) canvas.width = width;
      if (canvas.height !== height) canvas.height = height;
      const ctx = canvas.getContext('2d'); ctx.imageSmoothingEnabled = false;
      ctx.clearRect(0, 0, width, height);
      ctx.drawImage(image, (frame % columns) * width, Math.floor(frame / columns) * height,
                    width, height, 0, 0, width, height);
      canvas.dataset.frame = String(frame);
    }
    const label = document.querySelector('[data-lifecycle-label]');
    if (label) label.textContent = ms >= 8800 ? 'IDLE / READY WHEN YOU ARE' : ms >= 4000 ? 'COMPLETE / ONE VICTORY LAP' : 'ACTIVE / KEEPING YOU COMPANY';
    art.dataset.time = String(ms);
  }
  function pause() {running = false; cancelAnimationFrame(raf);}
  function seek(ms) { if (!Number.isFinite(ms) || ms < 0) throw Error('Time must be nonnegative milliseconds'); pause(); elapsed = ms; draw(ms); return snapshot(); }
  function snapshot() {return {timeMs: elapsed, width:1080, height:art.dataset.format === 'reel' ? 1920 : 1350, frames:canvases.map(c => Number(c.dataset.frame)), clips:canvases.map(c=>c.dataset.clip), packs:canvases.map(c=>c.dataset.pack), sportsSlots, theme:art.dataset.theme};}
  function play() { if(running) return; running=true; origin=performance.now()-elapsed; const tick=now=>{if(!running)return;elapsed=(now-origin)%10000;draw(elapsed);raf=requestAnimationFrame(tick)};raf=requestAnimationFrame(tick); }
  const ready = resolveSports().then(()=>Promise.all([...new Set(canvases.map(c=>c.dataset.pack))].map(load))).then(async()=>{
    for (const slot of sportsSlots) {
      const clip = sources.get(slot.pack).manifest.animations.complete;
      slot.completeDurationMs = clip.frames.length * clip.frame_ms;
    }
    await Promise.all([document.fonts.load('700 40px "Space Grotesk"'), document.fonts.load('400 34px "Space Grotesk"'), document.fonts.load('400 24px "IBM Plex Mono"')]);
    await document.fonts.ready;
    if(!document.fonts.check('700 40px "Space Grotesk"') || !document.fonts.check('400 24px "IBM Plex Mono"')) throw Error('Exact brand fonts unavailable');
    seek(Number(query.get('t') ?? art.dataset.coverMs ?? 420)); document.documentElement.dataset.ready='true';
    if(query.get('play') === '1' && !matchMedia('(prefers-reduced-motion: reduce)').matches) play();
    return snapshot();
  }).catch(error=>{document.documentElement.dataset.error=error.message;const node=document.createElement('p');node.className='error';node.textContent='Export blocked: '+error.message;document.body.append(node);throw error});
  window.OpenVegasCampaign={ready,seek,play,pause,snapshot,setFormat(format){art.dataset.format=format==='reel'?'reel':'slide';return snapshot()},setTheme(theme){if(['dark','light'].includes(theme))art.dataset.theme=theme;return snapshot()}};
})();
