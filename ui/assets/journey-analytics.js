// Separate opt-in: existing session/replay choices do not authorize cross-visit IDs.
import {apiFetch, bootstrapBrowserSession} from './page-auth.js?v=20260330';
export const KEY = 'ov_journey_consent_v1';
const TTL = 90 * 86400000;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const denied = () => navigator.globalPrivacyControl === true || ['1','yes'].includes(navigator.doNotTrack) || window.doNotTrack === '1';
export function readChoice(storage, now=Date.now()) {
  try {
    const value = JSON.parse(storage.getItem(KEY));
    if(value?.enabled === false) return value;
    if(value?.enabled === true && UUID.test(value.id) && Number.isFinite(value.created)
       && value.created<=now && now-value.created<TTL) return value;
  } catch { /* unavailable storage means no tracking */ }
  return null;
}
let mounted=false;
function currentChoice() {
  try { return readChoice(localStorage); } catch { return null; }
}
export async function recordJourneyRegistration() {
  if(denied()) return;
  const choice=currentChoice();
  if(choice?.enabled !== true) return;
  try {
    await apiFetch('/analytics/journey/registration', {method:'POST', cache:'no-store',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({browser_id:choice.id,consent:true}), signal:AbortSignal.timeout(2000)});
  } catch { /* optional measurement must not block authentication */ }
}
export async function startJourneyAnalytics() {
  if(mounted || denied() || !window.location.pathname.startsWith('/ui')) return;
  // Admin pages never receive tracking or controls.
  if(['/ui/business','/ui/business.html'].includes(location.pathname)) return;
  mounted=true;
  try {
    const config=await fetch('/analytics/journey/config',{cache:'no-store',credentials:'omit',signal:AbortSignal.timeout(2000)}).then(r=>r.json());
    if(config.enabled!==true) return;
  } catch {return;}
  const controls=document.createElement('details');controls.className='ph-no-capture';
  controls.style.cssText='margin:1rem;padding:1rem;border:1px solid var(--border,#777);max-width:42rem';
  controls.innerHTML='<summary>Signup and return measurement</summary><p>Optional: remember a random browser ID for 90 days to measure visits, completed signup and next-day returns in OpenVegas and PostHog. Signup is linked to your account only inside OpenVegas. No email, prompts, terminal content, card details or replay is sent. This does not enable the other analytics options.</p><button type="button" class="btn">Allow measurement</button> <button type="button" class="btn">Turn off</button><p role="status"></p>';
  const [allow,off]=controls.querySelectorAll('button'),status=controls.querySelector('[role=status]');
  const render=()=>{status.textContent=currentChoice()?.enabled===true?'Measurement on for this browser.':'Measurement off.';};
  async function visit() {
    const choice=currentChoice();
    if(denied()||choice?.enabled!==true) return;
    try {
      await fetch('/analytics/journey/visit',{method:'POST',credentials:'omit',cache:'no-store',referrerPolicy:'no-referrer',
        headers:{'Content-Type':'application/json'}, body:JSON.stringify({browser_id:choice.id,consent:true}),signal:AbortSignal.timeout(2000)});
      if(await bootstrapBrowserSession()) await recordJourneyRegistration();
    } catch { /* best effort */ }
  }
  allow.onclick=()=>{try {if(currentChoice()?.enabled!==true) localStorage.setItem(KEY,JSON.stringify({enabled:true,id:crypto.randomUUID(),created:Date.now()}));render();void visit();} catch {status.textContent='Browser storage unavailable; measurement stays off.';}};
  off.onclick=()=>{try {localStorage.setItem(KEY,JSON.stringify({enabled:false}));} catch {} render();};
  document.body.append(controls);render();void visit();
}
