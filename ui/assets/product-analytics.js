import { PUBLIC_PAGES, validateConfig } from "./analytics.js";

// Signed-in counts need a fresh choice; neither marketing nor product v1 grants it.
export const PRODUCT_CONSENT_KEY = "ov_product_activity_consent_v2";
export const PRODUCT_PAGES = Object.freeze({
  ...PUBLIC_PAGES, "/ui/emotes": "emotes", "/ui/emotes.html": "emotes",
  "/ui/tv": "tv", "/ui/tv.html": "tv",
});
const ACTIONS = new Set(["preview", "select", "play", "pause"]);
const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}(?![\s\S])/i;
const PRIVATE = "input, textarea, select, form, pre, code, [contenteditable], [data-ov-private], .ph-no-capture, #ov-nav-auth, #ov-analytics-privacy, #ov-product-analytics-privacy";

export function productPage(location) {
  if (!location || location.search || location.hash) return null;
  return Object.hasOwn(PRODUCT_PAGES, location.pathname) ? PRODUCT_PAGES[location.pathname] : null;
}
export function parseProductConsent(raw) {
  try {
    const value = JSON.parse(raw);
    return value?.version === 2 && typeof value.activity === "boolean"
      ? { version: 2, activity: value.activity } : null;
  } catch { return null; }
}
export function activityPayload(event, feature, action, sessionId) {
  if (!Object.values(PRODUCT_PAGES).includes(feature) || typeof sessionId !== "string" || !UUID_V4.test(sessionId)) return null;
  if (event === "product_page_view" ? action !== "view" : event !== "product_action" || !ACTIONS.has(action)) return null;
  return { event, surface: "web", feature, action, outcome: "none", session_id: sessionId, consent: true };
}

export function createProductAnalytics({ window: win = window, document: doc = document,
  fetch: fetcher = win.fetch.bind(win), now = () => Date.now() } = {}) {
  let initialized, config, controls, checkbox, status;
  let sessionReady = false, disposed = false, invalidContext = false;
  let sessionEpoch = null;
  let consent = false, durable = false, sessionId = null, pageSent = false;
  let budget = [], total = 0;
  const pending = new Set(), removers = [], recent = new Map();
  function savedChoice() { try { return parseProductConsent(win.localStorage.getItem(PRODUCT_CONSENT_KEY)); } catch { return null; } }
  const privacySignal = () => win.navigator?.globalPrivacyControl === true
    || ["1", "yes"].includes(win.navigator?.doNotTrack) || win.doNotTrack === "1";
  function eligible() {
    const feature = productPage(win.location);
    return !!config && !!feature && (feature === "emotes" || feature === "tv" || config.public_pages.includes(win.location.pathname))
      && sessionReady && !disposed && !invalidContext && !privacySignal();
  }
  function permitted() { return eligible() && consent && (!durable || savedChoice()?.activity === true); }
  function stop() {
    sessionId = null; pageSent = false;
    for (const request of pending) { request.controller.abort(); win.clearTimeout(request.timer); }
    pending.clear(); recent.clear();
  }
  function render() {
    if (!controls) return;
    controls.hidden = !sessionReady || !productPage(win.location);
    checkbox.checked = consent;
    checkbox.disabled = privacySignal() || invalidContext;
    status.textContent = privacySignal() ? "Your browser privacy signal disables product activity."
      : invalidContext ? "Product activity stopped. Reload to change your choice."
      : consent ? "Product activity opted in. Only fixed page and action counts are sent; delivery is best effort."
      : "Product activity is off. Marketing/replay consent does not enable it.";
  }
  async function send(event, action) {
    if (!permitted()) { stop(); return false; }
    const feature = productPage(win.location), time = now();
    budget = budget.filter((at) => time - at < 60000);
    const key = `${feature}:${action}`;
    if (total >= 100 || budget.length >= 30 || pending.size >= 3
        || (recent.has(key) && time - recent.get(key) < 500)) return false;
    let payload;
    try {
      sessionId ||= win.crypto.randomUUID();
      payload = activityPayload(event, feature, action, sessionId);
    } catch { return false; }
    if (!payload) return false;
    const controller = new win.AbortController();
    const request = { controller, timer: win.setTimeout(() => controller.abort(), 2000) };
    pending.add(request); budget.push(time); total++; recent.set(key, time);
    try {
      const response = await fetcher("/analytics/activity", {
        method: "POST", credentials: "omit", referrerPolicy: "no-referrer", redirect: "error", cache: "no-store",
        keepalive: false, headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify(payload), signal: controller.signal,
      });
      return response.ok === true && !controller.signal.aborted;
    } catch { return false; }
    finally { win.clearTimeout(request.timer); pending.delete(request); }
  }
  function start() {
    if (!permitted()) { stop(); render(); return; }
    if (!pageSent) { pageSent = true; void send("product_page_view", "view"); }
    render();
  }
  function setConsent(value) {
    if (disposed) return;
    stop(); consent = value?.activity === true && !privacySignal();
    try { win.localStorage.setItem(PRODUCT_CONSENT_KEY, JSON.stringify({ version: 2, activity: consent })); durable = true; }
    catch { durable = false; }
    start();
  }
  function listen(target, type, fn) {
    target.addEventListener(type, fn);
    removers.push(() => target.removeEventListener(type, fn));
  }
  function invalidate() { invalidContext = true; stop(); render(); }
  function mount() {
    controls = doc.createElement("details"); controls.id = "ov-product-analytics-privacy";
    controls.className = "ph-no-capture"; controls.open = !savedChoice();
    controls.style.cssText = "margin:1rem;padding:.65rem 1rem;border:1px solid currentColor;border-radius:.5rem;font:inherit;font-size:.875rem;max-width:38rem";
    const element = (tag, text, parent = controls) => {
      const el = doc.createElement(tag); if (text) el.textContent = text; parent.appendChild(el); return el;
    };
    element("summary", "Product activity privacy");
    element("p", "Separately allow anonymous fixed feature and action counts on marketing, emote and TV pages, including while signed in. A temporary random session ID groups counts and resets on auth changes. No account identity, signed-in state, DOM, input, prompts, code, URLs, payment data or replay is sent. Optional and off by default.");
    const label = element("label"); label.style.cssText = "display:block;padding:.5rem 0";
    checkbox = element("input", null, label); checkbox.type = "checkbox"; checkbox.id = "ov-product-activity-choice";
    element("span", " Allow anonymous product counts, including while signed in", label);
    const save = element("button", "Save product choice"), reject = element("button", "Reject product activity");
    for (const button of [save, reject]) { button.type = "button"; button.style.cssText = "font:inherit;min-height:44px;margin:.25rem .5rem .25rem 0;padding:.5rem .75rem"; }
    save.addEventListener("click", () => setConsent({ activity: checkbox.checked }));
    reject.addEventListener("click", () => setConsent({ activity: false }));
    status = element("p"); status.setAttribute("role", "status"); status.setAttribute("aria-live", "polite");
    doc.body.appendChild(controls); render();
  }
  function bind() {
    listen(doc, "click", (event) => {
      const target = event.target?.closest?.("[data-ov-product-action]");
      if (!target || target.closest?.(PRIVATE) || target.disabled) return;
      const feature = target.getAttribute("data-ov-product-feature") || productPage(win.location);
      if (feature !== productPage(win.location)) return;
      const href = target.getAttribute("href");
      if (href) {
        try { const url = new URL(href, win.location.origin); if (url.origin !== win.location.origin || !productPage(url)) return; }
        catch { return; }
      }
      const action = target.getAttribute("data-ov-product-action");
      if (ACTIONS.has(action)) void send("product_action", action);
    });
    listen(win, "storage", (event) => {
      if (event.key !== null && event.key !== PRODUCT_CONSENT_KEY) return;
      stop(); consent = savedChoice()?.activity === true; durable = true; start();
    });
    for (const type of ["pagehide", "popstate", "hashchange"]) listen(win, type, invalidate);
    listen(win, "focus", () => { if (!permitted()) stop(); render(); });
    listen(doc, "visibilitychange", () => { if (!permitted()) stop(); render(); });
    for (const name of ["pushState", "replaceState"]) {
      const original = win.history[name];
      function wrapped(...args) { invalidate(); return original.apply(this, args); }
      win.history[name] = wrapped;
      removers.push(() => { if (win.history[name] === wrapped) win.history[name] = original; });
    }
  }
  async function initialize() {
    if (disposed || !productPage(win.location)) return api;
    const controller = new win.AbortController();
    const request = { controller, timer: win.setTimeout(() => controller.abort(), 3000) };
    pending.add(request);
    try {
      const response = await fetcher("/analytics/config", { credentials: "omit", referrerPolicy: "no-referrer", redirect: "error", cache: "no-store", headers: { Accept: "application/json" }, signal: controller.signal });
      if (!response.ok || controller.signal.aborted || disposed || !productPage(win.location)) return api;
      config = validateConfig(await response.json());
      if (!config || controller.signal.aborted || disposed) return api;
      pending.delete(request); win.clearTimeout(request.timer);
      const saved = savedChoice(); consent = saved?.activity === true; durable = !!saved;
      mount(); bind(); start();
    } catch { stop(); }
    finally { pending.delete(request); win.clearTimeout(request.timer); }
    return api;
  }
  const api = {
    init: () => (initialized ||= initialize()), setConsent,
    withdraw: () => setConsent({ activity: false }),
    // A new epoch marks an auth boundary, even true -> true (account switch).
    // Only a local monotonic epoch enters here; it is never persisted or sent.
    setSessionReady(value, epoch) {
      if (disposed) return;
      const versioned = epoch !== undefined;
      const valid = !versioned || (Number.isSafeInteger(epoch) && epoch >= 0);
      if (valid && versioned && sessionEpoch !== null && epoch < sessionEpoch) return;
      const ready = valid && value === true;
      if (valid && versioned && epoch === sessionEpoch && ready === sessionReady) return;
      stop(); sessionReady = ready;
      if (valid && versioned) sessionEpoch = epoch;
      start();
    },
    refreshContext() { if (!eligible()) stop(); render(); },
    openPreferences() { if (controls && !controls.hidden) { controls.open = true; controls.querySelector("summary")?.focus(); } },
    dispose() { disposed = true; stop(); removers.forEach((remove) => remove()); controls?.remove(); },
  };
  return api;
}

let singleton;
export function bootstrapProductAnalytics() {
  singleton ||= createProductAnalytics();
  return singleton.init();
}
