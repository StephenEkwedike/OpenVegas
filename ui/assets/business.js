import {
  bootstrapBrowserSession, clearBrowserToken, getBrowserToken, getLoginHref,
  getSessionSummary, logoutBrowserSession, setBrowserToken,
} from "/ui/assets/page-auth.js";

const el = (id) => document.getElementById(id);
let topic = "overview";
let pollTimer;
let expiryTimer;
let loading = false;
let unlocked = false;
let generation = 0;
const format = (value, suffix = "") => value === null || value === undefined
  ? "Unknown" : `${Number(value).toLocaleString(undefined, { maximumFractionDigits: 2 })}${suffix}`;
const timestamp = (value) => value ? new Date(value).toLocaleString() : "unknown";
const capacityMetrics = ["capacity-cpu", "capacity-rss", "capacity-container-cpu", "capacity-memory",
  "capacity-inflight", "capacity-latency", "capacity-errors"];
const percent = (value) => format(value === null || value === undefined ? null : value * 100, "%");
const memory = (value) => format(value === null || value === undefined ? null : value / 1048576, " MiB");

function status(message) { el("status").textContent = message; }
function clearEnrollment() {
  el("qr").removeAttribute("src");
  el("enrollment").hidden = true;
  el("otp").value = "";
}

function lock(message) {
  generation += 1;
  unlocked = false;
  clearTimeout(pollTimer);
  clearTimeout(expiryTimer);
  clearEnrollment();
  el("console").hidden = true;
  el("mfa").hidden = true;
  el("access").hidden = false;
  el("answers").replaceChildren();
  el("followups").replaceChildren();
  ["profiles", "cash", "latency", "load", ...capacityMetrics].forEach((id) => { el(id).textContent = "Unknown"; });
  ["alerts", "capacity", "observations", "latency-chart", "capacity-warnings", "capacity-unknowns"].forEach((id) => el(id).replaceChildren());
  ["finance-note", "runtime-note", "freshness", "trend-label", "capacity-status", "capacity-window",
    "capacity-note", "capacity-thresholds"].forEach((id) => { el(id).textContent = ""; });
  el("question").value = "";
  renderJourney({status:'unavailable'});
  topic = "overview";
  status(message);
}

async function request(path, payload) {
  // Never call apiFetch here: its proactive refresh can overwrite an AAL2 upgrade.
  const token = getBrowserToken();
  if (!token) throw new Error("Sign in before accessing this console.");
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 22000);
  try {
    const response = await fetch(`/business/${path}`, {
      method: payload === undefined ? "GET" : "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      credentials: "same-origin", cache: "no-store", redirect: "error",
      body: payload === undefined ? undefined : JSON.stringify(payload), signal: controller.signal,
    });
    if (response.status === 401 || response.status === 403) {
      lock("Access denied or session expired. Sign in and verify your authenticator again.");
      throw new Error("Admin access and verified MFA are required.");
    }
    if (!response.ok) {
      const messages = { 409: "Enrollment already exists. Use your existing authenticator or manage factors in Supabase.",
        429: "Request limit reached. Wait a minute before retrying.",
        400: "Verification rejected. Check the code and try again.", 422: "Check the submitted fields." };
      throw new Error(messages[response.status] || "Source unavailable. Try again later; no successful result is assumed.");
    }
    return await response.json();
  } finally { clearTimeout(timer); }
}

function armExpiry(expiresAt) {
  clearTimeout(expiryTimer);
  const remaining = Number(expiresAt) * 1000 - Date.now();
  if (remaining <= 0) { clearBrowserToken(); lock("Session expired. Sign in again."); return false; }
  expiryTimer = setTimeout(() => { clearBrowserToken(); lock("Session expired. Sign in again."); },
    Math.min(remaining, 2147483647));
  return true;
}

function list(id, lines) {
  el(id).replaceChildren(...lines.map((line) => {
    const item = document.createElement("li"); item.textContent = line; return item;
  }));
}

function renderTrend(rows) {
  const svg = el("latency-chart");
  svg.replaceChildren();
  const points = rows.slice(-60);
  const values = points.map((p) => p.http_latency_ms_p95).filter((v) => v !== null);
  const max = Math.max(1, ...values);
  // Missing samples break the line rather than being fabricated as zero latency.
  let segment = [];
  const flush = () => {
    if (!segment.length) return;
    const line = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
    line.setAttribute("points", segment.join(" ")); svg.append(line); segment = [];
  };
  points.forEach((p, i) => {
    if (p.http_latency_ms_p95 === null) { flush(); return; }
    const x = 8 + i / Math.max(1, points.length - 1) * 584;
    const y = 120 - p.http_latency_ms_p95 / max * 105;
    segment.push(`${x},${y}`);
    const dot = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    dot.setAttribute("cx", x); dot.setAttribute("cy", y); dot.setAttribute("r", "2"); svg.append(dot);
  });
  flush();
  el("trend-label").textContent = `${points.length} observations, ${values.length} with HTTP data. `
    + `Latency scale: 0 to ${format(values.length ? max : null, " ms")}. `
    + `${timestamp(points[0]?.observed_at)} to ${timestamp(points.at(-1)?.observed_at)}.`;
  list("observations", points.slice(-6).map((p) => `${timestamp(p.observed_at)}: HTTP p95 `
    + `${format(p.http_latency_ms_p95, " ms")}; host load ${format(p.load_1m)}`));
}

function renderCapacity(data) {
  const r = data.resources, w = data.http_window, t = data.thresholds;
  el("capacity-status").textContent = data.warnings.length ? "Attention: measured thresholds reached." :
    "No observed threshold breach. This is not proof of health or spare capacity.";
  el("capacity-cpu").textContent = format(r.process_cpu_cores);
  el("capacity-rss").textContent = memory(r.process_rss_bytes);
  el("capacity-container-cpu").textContent = `${percent(r.container_cpu_ratio)} / ${format(r.container_cpu_limit_cores, " cores")}`;
  el("capacity-memory").textContent = `${memory(r.container_memory_bytes)} / ${memory(r.container_memory_limit_bytes)} (${percent(r.container_memory_ratio)})`;
  el("capacity-inflight").textContent = `${format(data.inflight)} / ${format(data.inflight_limit)}`;
  // Insufficient/stale retained samples are not current operational verdicts.
  el("capacity-latency").textContent = w.status === "available" ? format(w.latency_ms_p95, " ms") : `Unknown (${w.status})`;
  el("capacity-errors").textContent = w.status === "available" ? percent(w.error_rate) : `Unknown (${w.status})`;
  el("capacity-window").textContent = `Observed ${timestamp(data.observed_at)}. Window: ${w.status}; `
    + `${w.retained_count}/${w.retention_limit} retained requests across up to ${format(w.coverage_seconds, "s")}. `
    + `Last event age: ${format(w.last_event_age_seconds, "s")}; aborted: ${w.aborted_count}; application errors: ${w.application_error_count}. `
    + `CPU interval: ${format(r.cpu_interval_seconds, "s")}. ${w.latency_basis}.`;
  list("capacity-warnings", data.warnings.map((a) => `${a.severity}: ${a.metric} ${format(a.observed)} >= ${format(a.threshold)}`));
  list("capacity-unknowns", data.unknowns);
  el("capacity-note").textContent = data.note;
  el("capacity-thresholds").textContent = `Warning/critical: resource or verified inflight bound ${percent(t.resource_warning_ratio)}/${percent(t.resource_critical_ratio)}; `
    + `HTTP p95 ${t.latency_warning_ms}/${t.latency_critical_ms}ms; 5xx or application errors ${percent(t.error_warning_rate)}/${percent(t.error_critical_rate)}. `
    + `HTTP requires ${t.min_requests} requests and ${t.min_observation_seconds}s of observation, with an event in the last ${t.stale_event_seconds}s.`;
}

function renderJourney(data) {
  const available=data?.status==='available';
  for(const [id,key] of [['opens','first_opens'],['signups','registrations']]) el('journey-'+id).textContent=available?format(data[key]):'Unknown';
  el('journey-conversion').textContent=available&&data.signup_rate!==null?percent(data.signup_rate):'Not enough data';
  el('journey-d1').textContent=available&&data.d1_rate!==null?percent(data.d1_rate):'Not enough data';
  el('journey-status').textContent=available?`Measured environment: ${data.environment}.`:`Journey measurement ${data?.status||'unavailable'}. No zero rate is assumed.`;
  el('journey-sample').textContent=available?`${data.returned_d1} returned / ${data.eligible_d1} eligible signups. ${data.pending_exports} events awaiting PostHog delivery.`:'';
}

function render(data) {
  el("profiles").textContent = format(data.business.profile_count);
  el("cash").textContent = data.business.paid_topup_usd === null ? "Unknown" : Number(data.business.paid_topup_usd)
    .toLocaleString(undefined, {style: "currency", currency: "USD"});
  el("latency").textContent = format(data.runtime.http_latency_ms_p95, " ms");
  el("load").textContent = format(data.runtime.load_1m);
  el("finance-note").textContent = `${data.business.note} Source: ${data.business.source}. `
    + `Observed: ${timestamp(data.business.observed_at)}. Latest included topup row update: ${timestamp(data.business.latest_topup_update)}.`;
  el("runtime-note").textContent = data.runtime.note;
  el("freshness").classList.remove("stale");
  el("freshness").textContent = `Process ${data.runtime.instance}; observed ${timestamp(data.observed_at)}. `
    + `Refresh: 30s while visible; cache: up to 15s. Database: ${data.business.status}.`;
  list("alerts", data.alerts.length ? data.alerts.map((a) => `${a.severity}: ${a.metric} `
    + `${format(a.observed)} > ${format(a.threshold)} (${a.status})`) : ["No threshold alerts in this process. No samples is not evidence of health."]);
  list("capacity", data.capacity_guidance);
  renderCapacity(data.capacity);
  renderTrend(data.trend);
}

async function refresh() {
  if (!unlocked || loading || document.hidden) return;
  loading = true; el("refresh").disabled = true;
  const version = generation;
  try {
    const data = await request("summary");
    if (version !== generation) return;
    render(data); el("console").hidden = false;
    const journeys=await request('journeys').catch(()=>({status:'unavailable'}));
    if(version!==generation)return;
    renderJourney(journeys);
    status("Admin and MFA verified. Read-only console; external connections remain unverified.");
  } catch (error) {
    if (version === generation) {
      el("freshness").classList.add("stale");
      el("freshness").textContent = "STALE: refresh failed. Any displayed values are previous observations, not current facts.";
      el("capacity-status").textContent = "STALE: capacity values below are previous observations, not current headroom.";
      status(error.message);
    }
  } finally {
    loading = false; el("refresh").disabled = false; clearTimeout(pollTimer);
    if (unlocked && !document.hidden) pollTimer = setTimeout(refresh, 30000);
  }
}

function addFactor(id, label) {
  const option = document.createElement("option"); option.value = id; option.textContent = label;
  el("factor").append(option); el("factor").value = id;
}

el("enroll").addEventListener("click", async () => {
  el("enroll").disabled = true;
  const version = generation;
  try {
    const data = await request("mfa/enroll", {});
    if (version !== generation) return;
    addFactor(data.factor_id, "New authenticator (pending verification)");
    if (!/^data:image\/png;base64,[A-Za-z0-9+/=]+$/.test(data.qr_png)) throw new Error("Invalid enrollment image.");
    el("qr").src = data.qr_png; el("enrollment").hidden = false;
    status("Scan the private QR, then enter your authenticator code.");
  } catch (error) { status(error.message); el("enroll").disabled = false; }
});

el("verify-form").addEventListener("submit", async (event) => {
  event.preventDefault(); el("verify").disabled = true;
  const version = generation;
  try {
    const factor_id = el("factor").value;
    if (!factor_id) throw new Error("Enroll or select an authenticator first.");
    const { challenge_id } = await request("mfa/challenge", { factor_id });
    if (version !== generation) return;
    const data = await request("mfa/verify", { factor_id, challenge_id, code: el("otp").value });
    if (version !== generation) return;
    setBrowserToken(data.access_token, data.expires_at);
    clearEnrollment(); if (!armExpiry(data.expires_at)) return;
    el("mfa").hidden = true; unlocked = true;
    await refresh();
  } catch (error) { status(error.message); }
  finally { el("otp").value = ""; el("verify").disabled = false; }
});

el("question-form").addEventListener("submit", async (event) => {
  event.preventDefault(); if (!unlocked || el("ask").disabled) return;
  el("ask").disabled = true;
  const version = generation;
  try {
    const data = await request("questions", { question: el("question").value, topic });
    if (version !== generation) return;
    topic = data.topic;
    const answer = document.createElement("article"); answer.className = "answer";
    const text = document.createElement("p"); text.textContent = data.answer;
    const source = document.createElement("small");
    source.textContent = `Topic: ${topic}. ` + data.sources.map((s) => `${s.name}: ${timestamp(s.observed_at)}`).join(" / ");
    answer.append(text, source); el("answers").append(answer);
    while (el("answers").children.length > 6) el("answers").firstChild.remove();
    el("question").value = "";
    el("followups").replaceChildren(...data.followups.map((question) => {
      const button = document.createElement("button"); button.type = "button"; button.textContent = question;
      button.addEventListener("click", () => { el("question").value = question; el("question").focus(); });
      return button;
    }));
  } catch (error) { status(error.message); }
  finally { el("ask").disabled = false; }
});

el("refresh").addEventListener("click", refresh);
document.addEventListener("visibilitychange", () => {
  clearTimeout(pollTimer);
  if (!document.hidden) refresh();
});
window.addEventListener("pagehide", () => lock("Console closed. Reload to verify access."));
window.addEventListener("pageshow", (event) => { if (event.persisted) window.location.reload(); });
el("signout").addEventListener("click", async () => {
  lock("Signing out..."); await logoutBrowserSession(); el("signout").hidden = true; status("Signed out.");
});

async function start() {
  const version = generation;
  el("login").href = getLoginHref("/ui/business");
  try {
    // Preserve a valid upgraded token on reload; bootstrap only an absent/expired session.
    if (!getBrowserToken() || getSessionSummary().expires_at * 1000 <= Date.now()) await bootstrapBrowserSession();
    if (version !== generation) return;
    if (!getBrowserToken()) { lock("Sign in with your allowlisted admin account."); return; }
    el("signout").hidden = false;
    const data = await request("mfa");
    if (version !== generation || !armExpiry(data.expires_at)) return;
    el("access").hidden = true;
    if (data.aal === "aal2") { unlocked = true; await refresh(); return; }
    el("factor").replaceChildren();
    data.factors.forEach((factor, index) => addFactor(factor.id, `Authenticator ${index + 1} (${factor.status})`));
    const verified = data.factors.find((factor) => factor.status === "verified");
    if (verified) el("factor").value = verified.id;
    el("enroll").hidden = Boolean(verified);
    el("mfa").hidden = false;
    status("Admin identity verified. Complete MFA before business data is available.");
  } catch (error) { status(error.message); }
}
start();
