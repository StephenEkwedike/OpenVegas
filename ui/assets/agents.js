import {money} from './currency.js';
import {apiFetch, bootstrapBrowserSession, getLoginHref} from './page-auth.js?v=20260330';
import {renderTopNav, installAssetGuard} from './site.js?v=20260330';
renderTopNav();
installAssetGuard();
const el=id=>document.getElementById(id);
const message=text=>{el('agentMessage').textContent=text;};
let generation=0;
let overviewTimer;
let organizations=[];
let fundingGeneration=0, checkoutKey=null;
function showOverview(data) {
  el('agentAccounts').replaceChildren();
  for(const agent of data.agents||[]) {
    const card=document.createElement('article');card.className='agent-account';
    const title=document.createElement('h3');title.textContent=agent.name;
    const balance=document.createElement('strong');balance.className='agent-balance';
    balance.textContent=agent.available_balance_v===null?'Balance unavailable':`${money(agent.available_balance_v)}`;
    const status=document.createElement('p');status.textContent=`${agent.status} / ${agent.open_sessions.length?agent.open_sessions.map(m=>m==='house'?'House session':'Inference session').join(' + '):'No open sessions'}`;
    const detail=document.createElement('details'),summary=document.createElement('summary'),id=document.createElement('code');
    summary.textContent='Account ID';id.textContent=agent.id;detail.append(summary,id);
    card.append(title,balance,status,detail);el('agentAccounts').append(card);
  }
  if(!data.agents?.length)el('agentAccounts').textContent='No agents yet.';
  el('agentBalanceNote').textContent=`Available wallet balances. Open sessions are not proof an agent is currently thinking. ${data.has_more?'Showing the latest 200 accounts. ':''}Updated ${new Date().toLocaleTimeString()}.`;
}
function scheduleOverview(version, org) {
  clearTimeout(overviewTimer);
  if(document.hidden||!org||version!==generation)return;
  overviewTimer=setTimeout(async()=>{
    try {const data=await request(`/v1/agent/organizations/${encodeURIComponent(org)}/overview`);
      if(version===generation)showOverview(data);
    } catch(error) {if(version===generation)el('agentBalanceNote').textContent='Balance refresh unavailable. Displayed values may be out of date.';}
    finally {scheduleOverview(version,org);}
  },15000);
}
async function request(path, init) {
  const response=await apiFetch(path, init);
  const data=await response.json().catch(()=>({}));
  if(!response.ok) throw new Error(typeof data.detail==='string'?data.detail:`Request failed (${response.status})`);
  return data;
}
function table(id, rows, fields) {
  const target=el(id);target.replaceChildren();
  for(const row of rows) {const tr=document.createElement('tr');for(const field of fields){const td=document.createElement('td');td.textContent=field.endsWith('_v') ? money(row[field]) : String(row[field]??'-');tr.append(td);}target.append(tr);}
  if(!rows.length){const tr=document.createElement('tr'),td=document.createElement('td');td.colSpan=fields.length;td.textContent='No activity yet.';tr.append(td);target.append(tr);}
}
async function refresh() {
  const org=el('agentOrg').value,version=++generation;
  ++fundingGeneration;
  for(const id of ['fundingCheckout','autoEnable','autoDisable'])el(id).disabled=true;
  clearTimeout(overviewTimer);
  el('agentData').hidden=true;
  if(!org){message('Create an organization to connect your first agent.');return;}
  message('Loading authorized organization activity...');
  try {
    const base=`/v1/agent/admin/orgs/${encodeURIComponent(org)}`;
    const [overview,audit,policy]=await Promise.all([request(`/v1/agent/organizations/${encodeURIComponent(org)}/overview`),request(base+'/audit?limit=50'),request(base+'/policies')]);
    if(version!==generation)return;
    const sessions=audit.agent_sessions||[],games=audit.casino_sessions||[];
    el('agentSpend').textContent=money(sessions.reduce((n,r)=>n+Number(r.spent_v||0),0).toFixed(2));
    el('agentPnl').textContent=money(games.reduce((n,r)=>n+Number(r.net_pnl_v||0),0).toFixed(2));
    el('agentCount').textContent=String(sessions.length+games.length);
    showOverview(overview);
    el('fundingAgent').replaceChildren(...(overview.agents||[]).map(agent=>{
      const option=document.createElement('option');option.value=agent.id;option.textContent=agent.name;return option;
    }));
    checkoutKey=null;
    await refreshFunding();
    if(version!==generation)return;
    el('agentPolicy').replaceChildren();
    for(const [label,key] of [['Inference enabled','agent_infer_enabled'],['Games enabled','casino_enabled'],['Session limit ($V)','agent_max_envelope_v'],['Game loss limit ($V)','casino_agent_max_loss_v'],['Max wager ($V)','casino_round_max_wager_v']]){const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=label;dd.textContent=key.endsWith('_v') ? money(policy.policy?.[key]) : String(policy.policy?.[key]??'Not configured');el('agentPolicy').append(dt,dd);}
    table('agentSessions',sessions,['agent_name','envelope_v','spent_v','status','started_at']);
    table('agentGames',games,['agent_name','rounds_played','net_pnl_v','status','started_at']);
    el('agentData').hidden=false;
    message(organizations.find(o=>o.id===org)?.is_demo
      ? 'LOCAL DEMO: simulated balances, usage and game results. No provider calls, charges or customer revenue. Inference and wagering are disabled.'
      : 'Updated '+new Date().toLocaleTimeString()+'. Read-only activity view.');
    scheduleOverview(version,org);
  } catch(error){if(version===generation)message(error.message);}
}
document.addEventListener('visibilitychange',()=>scheduleOverview(generation,el('agentOrg').value));
window.addEventListener('pagehide',()=>clearTimeout(overviewTimer));
async function loadOrganizations(selected='') {
  const data=await request('/v1/agent/organizations');organizations=data.organizations||[];el('agentOrg').replaceChildren();
  for(const org of data.organizations||[]){const opt=document.createElement('option');opt.value=org.id;opt.textContent=org.name;el('agentOrg').append(opt);}
  if(selected)el('agentOrg').value=selected;
  await refresh();
}
el('agentOrg').addEventListener('change',refresh);el('agentRefresh').addEventListener('click',refresh);
for(const [form,input,path] of [['agentOrgForm','agentOrgName',()=>'/v1/agent/organizations'],['agentAccountForm','agentName',()=>`/v1/agent/admin/orgs/${encodeURIComponent(el('agentOrg').value)}/accounts`]]) {
  el(form).addEventListener('submit',async event=>{event.preventDefault();const button=event.submitter;button.disabled=true;try{const data=await request(path(),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:el(input).value})});el(input).value='';if(form==='agentOrgForm')await loadOrganizations(data.org_id);else await refresh();}catch(error){message(error.message);}finally{button.disabled=false;}});
}
if(await bootstrapBrowserSession()){el('agentWorkspace').hidden=false;loadOrganizations().catch(e=>message(e.message));}
else {message('Sign in to view your agent organizations. ');const a=document.createElement('a');a.href=getLoginHref('/ui/agents');a.textContent='Sign in/up';el('agentMessage').append(a);}

function fundingPath(){return `/v1/agent/organizations/${encodeURIComponent(el('agentOrg').value)}/agents/${encodeURIComponent(el('fundingAgent').value)}/funding`;}
async function refreshFunding(){
  const version=++fundingGeneration;
  for(const id of ['fundingCheckout','autoEnable','autoDisable'])el(id).disabled=true;
  el('fundingHistory').replaceChildren();el('autoConsent').checked=false;
  if(!el('fundingAgent').value){el('fundingStatus').textContent='Create an agent to see its funding controls.';return;}
  try{
    const data=await request(fundingPath());if(version!==fundingGeneration)return;
    el('fundingStatus').textContent=data.reason;
    el('fundingCheckout').disabled=el('autoEnable').disabled=!data.enabled;
    const p=data.auto_topup||{};el('autoState').textContent=p.enabled?'On':'Off';
    el('autoDisable').disabled=!p.enabled;
    for(const [id,key] of [['autoThreshold','threshold_v'],['autoAmount','amount_usd'],['autoCap','monthly_cap_usd']])if(p[key])el(id).value=p[key];
    if(p.pause_reason)el('fundingStatus').textContent+=` Auto top-ups paused: ${p.pause_reason.replaceAll('_',' ')}.`;
    table('fundingHistory',data.orders||[],['created_at','amount_usd','credit_v','source','status']);
    [...el('fundingHistory').children].forEach((row,index)=>{
      const order=data.orders?.[index];if(!order)return;
      const cell=document.createElement('td');row.append(cell);
      if(['unknown','pending','requires_action','checkout','failed'].includes(order.status)){
        for(const cancel of [false,true]){
          if(cancel&&order.source!=='auto')continue;
          const button=document.createElement('button');button.className='btn';button.type='button';
          button.textContent=cancel?'Cancel unfinished payment':'Check payment';
          const path=fundingPath();button.addEventListener('click',async()=>{
            button.disabled=true;
            try{const result=await request(path+'/'+encodeURIComponent(order.id)+'/review',{
              method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cancel})});
              if(path!==fundingPath())return;
              await refreshFunding();el('fundingStatus').textContent=result.message;
            }catch(error){if(path===fundingPath())el('fundingStatus').textContent=error.message;button.disabled=false;}
          });cell.append(button);
        }
      }
    });
  }catch(error){if(version===fundingGeneration)el('fundingStatus').textContent=error.message;}
}
el('fundingAgent').addEventListener('change',()=>{checkoutKey=null;refreshFunding();});
for(const id of ['fundingAmount','fundingSaveCard'])el(id).addEventListener('change',()=>{checkoutKey=null;});
el('agentCheckout').addEventListener('submit',async event=>{
  event.preventDefault();el('fundingCheckout').disabled=true;
  const path=fundingPath(),version=fundingGeneration;
  checkoutKey ||= crypto.randomUUID();
  try{
    const data=await request(path+'/checkout',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({amount_usd:el('fundingAmount').value,idempotency_key:checkoutKey,save_card:el('fundingSaveCard').checked})});
    if(version!==fundingGeneration||path!==fundingPath())return;
    if(data.status==='checkout'&&data.checkout_url){const url=new URL(data.checkout_url);if(url.protocol!=='https:'||url.hostname!=='checkout.stripe.com')throw new Error('Unexpected checkout destination.');location.assign(url.href);}
    else {await refreshFunding();el('fundingStatus').textContent=`Payment ${data.status}. Check history before starting another purchase.`;}
  }catch(error){if(version===fundingGeneration&&path===fundingPath()){el('fundingStatus').textContent=error.message;el('fundingCheckout').disabled=false;}}
});
async function saveAuto(enabled){
  const path=fundingPath();el('autoEnable').disabled=el('autoDisable').disabled=true;
  try{await request(path+'/auto',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled,threshold_v:el('autoThreshold').value,amount_usd:el('autoAmount').value,monthly_cap_usd:el('autoCap').value,consent:enabled&&el('autoConsent').checked})});await refreshFunding();}
  catch(error){await refreshFunding();if(path===fundingPath())el('fundingStatus').textContent=error.message;}
}
el('agentAuto').addEventListener('submit',event=>{event.preventDefault();saveAuto(true);});
el('autoDisable').addEventListener('click',()=>saveAuto(false));
