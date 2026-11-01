// No import-time work. The parent must explicitly confirm a signed-out session.
export const CONSENT_KEY = "ov_public_analytics_consent_v1";
export const SDK_VERSION = "1.438.12";
export const SDK_INTEGRITY = "sha384-pZfaKQd5LWcpIEUslmObFeVixNbMS7sme8H/KkHMX4n4Bqs3+J4z5fkN8u3le2AU";
const SDK_CONSENT_KEY = "ov_public_posthog_consent";
const HOSTS = new Map([
  ["https://us.i.posthog.com", "https://us-assets.i.posthog.com"],
  ["https://eu.i.posthog.com", "https://eu-assets.i.posthog.com"],
]);
export const PUBLIC_PAGES = Object.freeze({
  "/ui": "home", "/ui/": "home", "/ui/index.html": "home",
  "/ui/how-it-works": "how_it_works", "/ui/how-it-works.html": "how_it_works",
  "/ui/pricing": "pricing", "/ui/pricing.html": "pricing",
  "/ui/faq": "faq", "/ui/faq.html": "faq",
  "/ui/how-to-play": "how_to_play", "/ui/how-to-play.html": "how_to_play",
});
const ACTIONS = new Set(["install", "home", "how_it_works", "pricing", "faq", "how_to_play"]);
const ERRORS = new Set(["script", "resource", "unhandled_rejection"]);
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![\s\S])/i;
const validID = (value) => typeof value === "string" && UUID.test(value);
const DENIED = Object.freeze({ version: 1, analytics: false, replay: false });
const REPLAY_ORIGIN = "https://public.invalid";

export function publicPage(location) {
  if (!location || location.search || location.hash) return null;
  return Object.hasOwn(PUBLIC_PAGES, location.pathname) ? PUBLIC_PAGES[location.pathname] : null;
}

export function validateConfig(value) {
  if (value?.enabled !== true || typeof value.project_token !== "string"
      || !/^phc_[A-Za-z0-9]{20,128}(?![\s\S])/.test(value.project_token)
      || !HOSTS.has(value.api_host) || !Array.isArray(value.public_pages)) return null;
  return {
    project_token: value.project_token, api_host: value.api_host,
    replay_enabled: value.replay_enabled === true,
    public_pages: value.public_pages.filter((path) => Object.hasOwn(PUBLIC_PAGES, path)),
  };
}

export function parseConsent(raw) {
  try {
    const value = JSON.parse(raw);
    if (value?.version !== 1 || typeof value.analytics !== "boolean"
        || typeof value.replay !== "boolean") return null;
    return { version: 1, analytics: value.analytics, replay: value.analytics && value.replay };
  } catch { return null; }
}

// Reviewed first-party presentation only. Never copy source CSS or computed styles:
// those can contain URLs, generated content, custom properties, or private text.
const REPLAY_CLASS_STYLES = Object.freeze({
  container: "width:94%;max-width:1240px;margin:0 auto;",
  "page-shell": "padding:42px 0 54px;",
  "top-nav": "display:flex;justify-content:space-between;align-items:center;gap:20px;padding:20px 32px;border-bottom:1px solid #aaa;",
  "nav-links": "display:flex;flex-wrap:wrap;gap:12px;",
  "ov-nav-right": "display:flex;align-items:center;gap:14px;flex-wrap:wrap;",
  "brand-link": "font-weight:700;text-decoration:none;",
  "page-hero": "display:grid;grid-template-columns:1.2fr 1fr;gap:24px;padding:38px 0 32px;border-bottom:1px solid #aaa;",
  "page-title": "font-size:clamp(34px,5.5vw,80px);line-height:1.05;font-weight:400;",
  "page-lead": "max-width:560px;line-height:1.68;",
  "section-grid-3": "display:grid;grid-template-columns:repeat(3,minmax(0,1fr));",
  "section-grid-2": "display:grid;grid-template-columns:repeat(2,minmax(0,1fr));",
  card: "padding:32px;border:1px solid #aaa;min-width:0;",
  "card-title": "font-size:25px;font-weight:500;margin:15px 0;",
  "card-copy": "font-size:15px;line-height:1.66;margin-bottom:16px;",
  "spec-row": "display:flex;justify-content:space-between;gap:12px;padding:12px 0;border-top:1px solid #aaa;",
  "cta-row": "display:flex;flex-wrap:wrap;gap:10px;margin-top:20px;",
  btn: "display:inline-flex;align-items:center;justify-content:center;padding:10px 14px;border:1px solid #777;text-decoration:none;",
  "btn-primary": "border-color:#16776c;color:#16776c;",
  "growth-page": "font-family:sans-serif;",
  "growth-hero": "display:grid;grid-template-columns:minmax(0,1.8fr) minmax(240px,1fr);align-items:center;gap:40px;padding:32px 0 56px;",
  "growth-hero--guide": "grid-template-columns:1fr;",
  "growth-eyebrow": "font:11px/1.6 monospace;letter-spacing:1px;color:#16776c;margin-bottom:16px;",
  "growth-title": "font-size:clamp(42px,5.5vw,77px);line-height:1.05;font-weight:500;",
  "growth-lead": "max-width:592px;margin-top:24px;font-size:19px;line-height:1.65;",
  "growth-note": "max-width:640px;margin-top:16px;font-size:13px;line-height:1.65;",
  "growth-companion": "padding:16px 24px;border:1px solid #aaa;min-height:260px;",
  "growth-pixel-stage": "display:grid;place-items:center;min-height:260px;",
  "growth-features": "gap:0;",
  "growth-link": "display:inline-block;color:#16776c;font-size:14px;line-height:1.5;",
  "growth-install": "display:grid;grid-template-columns:1fr 1fr;align-items:center;gap:40px;padding:40px 0;",
  "growth-command": "border:1px solid #aaa;padding:24px;min-height:120px;",
  "growth-pair": "display:grid;grid-template-columns:1fr 1fr;gap:40px;padding-top:40px;border-top:1px solid #aaa;",
  "footer-meta": "display:grid;grid-template-columns:repeat(4,1fr);gap:24px;padding:32px 0;",
  "faq-item": "padding:24px 0;border-bottom:1px solid #aaa;",
  "faq-q": "font-size:23px;line-height:1.3;margin-bottom:12px;",
  "faq-a": "font-size:15px;line-height:1.7;max-width:800px;",
  "text-mono-xs": "font:11px/1.6 monospace;",
  "text-mono-sm": "font:13px/1.6 monospace;",
  tag: "display:inline-block;border:1px solid #16776c;padding:2px 6px;font:11px monospace;",
});
const REPLAY_LABELS = new Set([
  "[ OPENVEGAS ]", "Home", "How it works", "Pricing", "FAQ", "HOW TO PLAY", "Get started",
  "Explore companions", "Company while you code.", "A break, on your terms.", "Optional AI funding.",
  "Keep your tools.", "Add OpenVegas.", "Install the CLI.", "Connect once.", "Work as usual.",
  "Play when you want.", "Make it yours.", "YOUR TOOLS", "YOUR LOOK", "YOUR BALANCE", "YOUR CHOICE",
  "Your terminal.", "A little more play.", "Keep your workflow.",
  "Simple pricing for AI funding and control.", "Starter", "Pro", "Team", "Enterprise controls",
]);
const REPLAY_BLOCKED_TAGS = new Set("script style link meta iframe frame object embed canvas img picture video audio svg pre code input textarea select option form".split(" "));
const REPLAY_PRIVATE = "input, textarea, select, form, pre, code, [contenteditable], [data-ov-private], .ph-no-capture, #ov-nav-auth, #ov-analytics-privacy, #ov-product-analytics-privacy";
export function safeReplayText(value) {
  if (typeof value !== "string") return "***";
  const label = value.trim();
  return REPLAY_LABELS.has(label) ? label : value.trim() === "" ? " " : "***";
}
function replayClasses(value) {
  if (typeof value !== "string" || value.length > 2048) return [];
  return [...new Set(value.split(/\s+/).filter((name) => Object.hasOwn(REPLAY_CLASS_STYLES, name)))].sort();
}
function replayStyle(tagName, classes, width) {
  let style = "box-sizing:border-box;";
  if (tagName === "body") style += "margin:0;background:#f6f6f2;color:#202828;font:16px/1.5 sans-serif;";
  if (["h1", "h2", "h3", "p", "figure"].includes(tagName)) style += "margin:0 0 16px;";
  // Use authored order, not source token order, so modifier rules win consistently.
  for (const name of Object.keys(REPLAY_CLASS_STYLES)) if (classes.includes(name)) style += REPLAY_CLASS_STYLES[name];
  if (width <= 1024 && classes.includes("growth-features")) style += "grid-template-columns:1fr;";
  if (width <= 760 && classes.some((name) => ["growth-hero", "growth-install", "growth-pair", "page-hero", "section-grid-2", "section-grid-3", "footer-meta"].includes(name))) style += "grid-template-columns:1fr;gap:24px;";
  if (width <= 760 && classes.includes("top-nav")) style += "flex-wrap:wrap;padding:16px;";
  return style;
}
export function maskReplayAttribute(name, value) {
  if (name === "class") return replayClasses(value).join(" ");
  return "";
}

// Unknown rrweb/plugin payloads are dropped. Attributes and layout are rebuilt
// from the fixed dictionary above; neither source URLs nor source CSS survive.
const TAGS = new Set("html head body main header footer nav section article aside div span p a button input textarea select option label form fieldset legend h1 h2 h3 h4 h5 h6 ul ol li dl dt dd table thead tbody tr td th br hr strong em b i pre code details summary img canvas svg path".split(" "));
const numeric = (value) => typeof value === "number" && Number.isFinite(value) ? value : 0;
function numbers(value, keys) {
  return Object.fromEntries(keys.map((key) => [key, numeric(value?.[key])]));
}

export function sanitizeSnapshots(events, page, state = { width: 1024, nodes: new Map() }) {
  if (!Array.isArray(events) || events.length > 2000) return [];
  if (!Object.values(PUBLIC_PAGES).includes(page)) return [];
  let budget = 20000;
  function presentation(tagName, attrs = {}) {
    const classes = replayClasses(attrs.class);
    return { class: classes.join(" "), style: replayStyle(tagName, classes, state.width) };
  }
  function node(value, depth = 0) {
    if (!value || --budget < 0 || depth > 60 || ![0, 1, 2, 3, 4, 5].includes(value.type)) return null;
    const clean = { type: value.type, id: numeric(value.id) };
    if (value.type === 1) Object.assign(clean, { name: "html", publicId: "", systemId: "" });
    if (value.type === 2) {
      const blocked = REPLAY_BLOCKED_TAGS.has(value.tagName);
      clean.tagName = blocked ? "div" : TAGS.has(value.tagName) ? value.tagName : "div";
      clean.attributes = blocked ? { style: "min-height:24px;border:1px dashed #aaa;" } : presentation(clean.tagName, value.attributes);
      for (const key of ["rr_width", "rr_height"]) {
        if (/^\d{1,6}(\.\d{1,4})?px$/.test(value.attributes?.[key])) clean.attributes[key] = value.attributes[key];
      }
      if (state.nodes.size < 20000) state.nodes.set(clean.id, { tagName: clean.tagName, class: clean.attributes.class || "", blocked });
      if (blocked) { clean.childNodes = []; return clean; }
    }
    if ([3, 4, 5].includes(value.type)) clean.textContent = value.type === 3 ? safeReplayText(value.textContent) : "***";
    if (value.type === 0 || value.type === 2) {
      clean.childNodes = Array.isArray(value.childNodes)
        ? value.childNodes.slice(0, 10000).map((child) => node(child, depth + 1)).filter(Boolean) : [];
    }
    if (value.isShadow === true) clean.isShadow = true;
    if (value.isShadowHost === true) clean.isShadowHost = true;
    return clean;
  }
  const out = [];
  for (const event of events) {
    const data = event?.data;
    let clean;
    if ([0, 1].includes(event?.type)) clean = {};
    if (event?.type === 4) {
      state.width = Math.max(1, Math.min(10000, numeric(data?.width))) || 1024;
      clean = { href: `${REPLAY_ORIGIN}/${page}`, ...numbers(data, ["width", "height"]) };
    }
    if (event?.type === 2) {
      state.nodes.clear();
      const root = node(data?.node);
      if (root) clean = { node: root, initialOffset: numbers(data?.initialOffset, ["top", "left"]) };
    }
    if (event?.type === 3) {
      const source = data?.source;
      const list = (key) => Array.isArray(data[key]) ? data[key].slice(0, 10000) : [];
      if (source === 0) clean = {
        source,
        adds: list("adds").map((add) => ({ parentId: numeric(add.parentId), nextId: add.nextId == null ? null : numeric(add.nextId), node: node(add.node) })).filter((add) => add.node),
        removes: list("removes").map((v) => numbers(v, ["parentId", "id"])),
        texts: list("texts").map((v) => ({ id: numeric(v.id), value: safeReplayText(v.value) })),
        attributes: list("attributes").flatMap((v) => {
          const known = state.nodes.get(v.id);
          if (!known || known.blocked) return [];
          if (typeof v.attributes?.class === "string") known.class = replayClasses(v.attributes.class).join(" ");
          return [{ id: numeric(v.id), attributes: presentation(known.tagName, known) }];
        }),
      };
      if ([1, 6, 12].includes(source)) clean = { source, positions: list("positions").map((v) => numbers(v, ["x", "y", "id", "timeOffset"])) };
      if (source === 2) clean = { source, ...numbers(data, ["type", "id", "x", "y"]) };
      if (source === 3) clean = { source, ...numbers(data, ["id", "x", "y"]) };
      if (source === 4) {
        state.width = Math.max(1, Math.min(10000, numeric(data?.width)));
        clean = { source, ...numbers(data, ["width", "height"]) };
      }
      // Input records (source 5) are excluded, including checkbox/select state.
    }
    if (clean) out.push({ type: event.type, timestamp: numeric(event.timestamp), data: clean });
    if (event?.type === 3 && data?.source === 4 && state.nodes.size) {
      out.push({ type: 3, timestamp: numeric(event.timestamp), data: {
        source: 0, adds: [], removes: [], texts: [],
        attributes: [...state.nodes].filter(([, known]) => !known.blocked).map(([id, known]) => ({ id, attributes: presentation(known.tagName, known) })),
      } });
    }
  }
  return budget < 0 ? [] : out;
}

export function sanitizeEvent(event, { config, page, anonymousId, replay, replayState }) {
  if (!page || !Object.values(PUBLIC_PAGES).includes(page) || !validID(anonymousId)) return null;
  const source = event?.properties || {};
  const properties = { token: config.project_token, distinct_id: anonymousId, $process_person_profile: false, $geoip_disable: true };
  for (const key of ["$session_id", "$window_id"]) {
    if (validID(source[key])) properties[key] = source[key];
  }
  if (event?.event === "$snapshot") {
    if (!replay) return null;
    const snapshots = sanitizeSnapshots(source.$snapshot_data, page, replayState);
    if (!snapshots.length || !properties.$session_id || !properties.$window_id) return null;
    Object.assign(properties, {
      $snapshot_data: snapshots, $snapshot_bytes: new TextEncoder().encode(JSON.stringify(snapshots)).length,
      $lib: "web", $lib_version: SDK_VERSION, $snapshot_host: "public.invalid",
    });
  } else {
    properties.page = page;
    if (event?.event === "marketing_click" && ACTIONS.has(source.action)) properties.action = source.action;
    else if (event?.event === "marketing_error" && ERRORS.has(source.kind)) properties.kind = source.kind;
    else if (event?.event !== "marketing_page_view") return null;
  }
  // Rebuild the envelope too: $set/$set_once can otherwise sit outside properties.
  const clean = { event: event.event, properties };
  // The SDK adds this Date before before_send and does not restore it afterward.
  // Do not retain arbitrary string/object timestamps or any identity envelope.
  if (event.timestamp instanceof Date && Number.isFinite(Date.prototype.getTime.call(event.timestamp))) {
    clean.timestamp = new Date(Date.prototype.getTime.call(event.timestamp));
  }
  return clean;
}

export function sdkOptions(config, beforeSend, anonymousId, replay, getPublicLocation = () => null) {
  return {
    api_host: config.api_host, defaults: "2026-05-30", debug: false,
    bootstrap: { distinctID: anonymousId, isIdentifiedID: false },
    autocapture: false, capture_pageview: false, capture_pageleave: false,
    capture_dead_clicks: false, capture_exceptions: false, capture_performance: false,
    capture_heatmaps: false, rageclick: false, enable_recording_console_log: false,
    capture_webmcp: false, disable_scroll_properties: true,
    logs: { captureConsoleLogs: false, beforeSend: () => null },
    metrics: { network: false, beforeSend: () => null },
    disable_surveys: true, disable_conversations: true, disable_product_tours: true,
    disable_web_experiments: true, opt_in_site_apps: false,
    disable_external_dependency_loading: true, strict_script_versioning: true,
    advanced_disable_flags: !replay, advanced_disable_decide: !replay,
    advanced_disable_feature_flags: true, advanced_disable_feature_flags_on_first_load: true,
    advanced_disable_toolbar_metrics: true, feature_flag_request_max_retries: 0,
    person_profiles: "never", persistence: "memory", disable_persistence: true,
    opt_out_capturing_by_default: true, opt_out_persistence_by_default: true,
    consent_persistence_name: SDK_CONSENT_KEY, cross_subdomain_cookie: false,
    save_referrer: false, save_campaign_params: false, custom_campaign_params: [],
    ip: false, respect_dnt: true, disableDeviceModel: true,
    disable_capture_url_hashes: true,
    // This hook is LOCAL rule targeting, not an outbound privacy filter. A fake
    // origin cannot match the operator's domain/URL rules. Never return empty:
    // the pinned SDK falls back to the full raw URL for an empty override.
    get_current_url: () => {
      try {
        const location = getPublicLocation();
        if (publicPage(location) && ["http:", "https:"].includes(location.protocol)) {
          return `${location.origin}${location.pathname}`;
        }
      } catch { /* Never let SDK fallback expose an unfiltered location. */ }
      return `${REPLAY_ORIGIN}/`;
    },
    request_batching: false, disable_compression: true, disable_beacon: true,
    on_request_error: () => {}, before_send: beforeSend,
    disable_session_recording: true,
    session_recording: {
      maskAllInputs: true, maskTextSelector: "*", maskTextFn: (text, element) => element?.closest?.(REPLAY_PRIVATE) ? "***" : safeReplayText(text), maskInputFn: () => "***",
      maskAllElementAttributes: false, maskAttributeFn: maskReplayAttribute,
      blockSelector: `${REPLAY_PRIVATE}, iframe, frame, object, embed, canvas, img, picture, video, audio, svg, script, style, link, meta`,
      inlineStylesheet: false, collectFonts: false, recordCrossOriginIframes: false,
      recordHeaders: false, recordBody: false, captureJsonLd: false,
      captureCanvas: { recordCanvas: false }, canvasCapture: { maskRegionsFn: () => null },
      // The SDK also calls this with ONLY {name} to mask rrweb Meta URLs.
      // Returning null there drops Meta entirely. Real network records still
      // drop; the URL-only hook returns only a synthetic fixed-page label.
      maskCapturedNetworkRequestFn: (request) => {
        const page = publicPage(getPublicLocation());
        return page && request && Object.keys(request).length === 1 && typeof request.name === "string"
          ? { name: `${REPLAY_ORIGIN}/${page}` } : null;
      },
      slimDOMOptions: "all", compress_events: false,
    },
  };
}

let sdkLoad;
let instanceNumber = 0;
export function loadPostHog(win, doc, config) {
  if (sdkLoad) return sdkLoad;
  // Never adopt a possibly identified/previously configured global instance.
  if (win.posthog) return Promise.reject(new Error("Conflicting analytics instance"));
  sdkLoad = new Promise((resolve, reject) => {
    // Also cover SDK fetch/XHR requests if the document has a permissive policy.
    const referrer = doc.createElement("meta");
    referrer.name = "referrer";
    referrer.content = "no-referrer";
    doc.head.appendChild(referrer);
    const script = doc.createElement("script");
    script.src = `${HOSTS.get(config.api_host)}/static/${SDK_VERSION}/array.full.no-external.js`;
    script.integrity = SDK_INTEGRITY;
    script.crossOrigin = "anonymous";
    script.referrerPolicy = "no-referrer";
    script.async = true;
    const timer = win.setTimeout(() => { script.remove(); reject(new Error("Analytics unavailable")); }, 15000);
    script.onerror = () => { win.clearTimeout(timer); script.remove(); reject(new Error("Analytics unavailable")); };
    script.onload = () => {
      win.clearTimeout(timer);
      if (typeof win.posthog?.init !== "function") return reject(new Error("Analytics unavailable"));
      const root = win.posthog;
      resolve((token, options) => {
        const name = `ov_public_${++instanceNumber}`;
        const sdk = root.init(token, options, name);
        return { sdk, release: () => { delete root[name]; } };
      });
    };
    doc.head.appendChild(script);
  });
  return sdkLoad;
}

function retireSDK(handle) {
  if (!handle) return;
  const sdk = handle.sdk;
  // Pinned-SDK teardown adapter: public shutdown flushes queues, including retries
  // captured before opt-out. Seal both dispatch paths BEFORE calling it.
  sdk._send_request = () => {};
  if (sdk._retryQueue) {
    sdk._retryQueue.retriableRequest = () => {};
    sdk._retryQueue._enqueue = () => {};
  }
  for (const operation of [
    () => sdk.set_config({ before_send: () => null, advanced_disable_flags: true, advanced_disable_decide: true }),
    () => sdk.stopSessionRecording(), () => sdk.opt_out_capturing(),
    () => sdk.reset(true), () => sdk.clear_opt_in_out_capturing(),
    () => sdk.shutdown()?.catch?.(() => {}),
    () => sdk.sessionManager?.destroy(), () => sdk.pageViewManager?.destroy(),
    () => sdk.persistence?.clear(), () => sdk.sessionPersistence?.clear(),
  ]) {
    try { operation(); } catch { /* Cleanup remains independent and fail-closed. */ }
  }
  sdk.__loaded = false;
  handle.release?.();
}

export function createAnalytics({ window: win = window, document: doc = document,
  fetch: fetcher = win.fetch.bind(win), loadSDK = loadPostHog } = {}) {
  let config = null, consent = { ...DENIED }, sdkHandle = null, generation = 0;
  let publicSession = false, invalidContext = false, disposed = false, initialized;
  let durableChoice = false;
  let controls, status, analyticsInput, replayInput;
  const removers = [];
  const readChoice = () => { try { return parseConsent(win.localStorage.getItem(CONSENT_KEY)); } catch { return null; } };
  const privacySignal = () => win.navigator?.globalPrivacyControl === true
    || ["1", "yes"].includes(win.navigator?.doNotTrack) || win.doNotTrack === "1";
  function page() {
    if (disposed || invalidContext || !publicSession || privacySignal()
        || !config?.public_pages.includes(win.location.pathname)) return null;
    return publicPage(win.location);
  }
  function permitted() {
    const saved = readChoice();
    return !!page() && consent.analytics && (!durableChoice || saved?.analytics === true);
  }
  function stop() {
    generation += 1;
    const previous = sdkHandle;
    sdkHandle = null;
    retireSDK(previous);
    try { win.localStorage.removeItem(SDK_CONSENT_KEY); } catch { /* Storage may be unavailable. */ }
  }
  function listen(target, type, fn) {
    target.addEventListener(type, fn);
    removers.push(() => target.removeEventListener(type, fn));
  }
  function render() {
    if (!controls) return;
    controls.hidden = !publicSession || !publicPage(win.location);
    analyticsInput.checked = consent.analytics;
    analyticsInput.disabled = privacySignal() || invalidContext;
    replayInput.checked = consent.replay;
    replayInput.disabled = !consent.analytics || !config.replay_enabled || privacySignal() || invalidContext;
    status.textContent = invalidContext ? "Tracking stopped. Reload this public page to change choices."
      : privacySignal() ? "Your browser privacy signal disables optional tracking."
      : !consent.analytics ? "Optional analytics and replay are off."
      : sdkHandle ? `Analytics on. Replay ${consent.replay ? "opted in" : "off"}.`
      : "Your choice is saved. Analytics is not active.";
  }
  function capture(event, properties = {}) {
    if (!permitted() || !sdkHandle) return false;
    sdkHandle.sdk.capture(event, properties);
    return true;
  }
  async function start() {
    if (!permitted() || sdkHandle) return;
    const epoch = ++generation;
    try {
      const factory = await loadSDK(win, doc, config);
      if (epoch !== generation || !permitted()) return;
      const id = win.crypto.randomUUID();
      const replayState = { width: 1024, nodes: new Map() };
      const beforeSend = (event) => epoch === generation && permitted()
        ? sanitizeEvent(event, { config, page: page(), anonymousId: id, replayState,
          replay: consent.replay && config.replay_enabled && (!durableChoice || readChoice()?.replay === true) }) : null;
      const options = sdkOptions(config, beforeSend, id, consent.replay && config.replay_enabled,
        () => epoch === generation && permitted() ? win.location : null);
      const handle = factory(config.project_token, options);
      if (!handle?.sdk || epoch !== generation || !permitted()) { retireSDK(handle); return; }
      sdkHandle = handle;
      handle.sdk.opt_in_capturing({ captureEventName: false });
      if (consent.replay && config.replay_enabled) handle.sdk.startSessionRecording();
      capture("marketing_page_view");
    } catch { stop(); }
    render();
  }
  async function setConsent(choice) {
    stop();
    consent = {
      version: 1, analytics: choice?.analytics === true && !privacySignal(),
      replay: choice?.analytics === true && choice?.replay === true && config?.replay_enabled === true && !privacySignal(),
    };
    try { win.localStorage.setItem(CONSENT_KEY, JSON.stringify(consent)); durableChoice = true; }
    catch { durableChoice = false; /* Page-only choice. */ }
    render();
    await start();
  }
  function mount() {
    controls = doc.createElement("details");
    controls.id = "ov-analytics-privacy";
    controls.className = "ph-no-capture";
    controls.open = !readChoice();
    controls.style.cssText = "position:relative;margin:1rem;padding:.65rem 1rem;border:1px solid currentColor;border-radius:.5rem;font:inherit;font-size:.875rem;max-width:38rem;background:var(--bg,Canvas);color:var(--text,CanvasText)";
    const element = (tag, text, parent = controls) => {
      const el = doc.createElement(tag); if (text) el.textContent = text; parent.appendChild(el); return el;
    };
    element("summary", "Privacy choices").style.cursor = "pointer";
    element("p", "Optional PostHog analytics measures public-page visits, selected clicks and error categories. Replay is a separate choice: inputs and private text are masked; only reviewed public labels and layout are retained. Sensitive pages are excluded. Neither is needed to use OpenVegas.");
    const checkbox = (label, id) => {
      const wrap = element("label"); wrap.style.cssText = "display:block;padding:.4rem 0";
      const input = element("input", null, wrap); input.type = "checkbox"; input.id = id;
      element("span", ` ${label}`, wrap); return input;
    };
    analyticsInput = checkbox("Allow public-page analytics", "ov-analytics-choice");
    replayInput = checkbox("Also allow masked session replay", "ov-replay-choice");
    if (!config.replay_enabled) element("p", "Session replay is not enabled for this site.");
    analyticsInput.addEventListener("change", () => {
      replayInput.disabled = !analyticsInput.checked || !config.replay_enabled;
      if (!analyticsInput.checked) replayInput.checked = false;
    });
    const save = element("button", "Save choices"); save.type = "button";
    const reject = element("button", "Reject marketing and replay"); reject.type = "button";
    for (const button of [save, reject]) button.style.cssText = "font:inherit;min-height:44px;margin:.25rem .5rem .25rem 0;padding:.5rem .75rem";
    save.addEventListener("click", () => { void setConsent({ analytics: analyticsInput.checked, replay: replayInput.checked }); });
    reject.addEventListener("click", () => { void setConsent(DENIED); });
    status = element("p"); status.setAttribute("role", "status"); status.setAttribute("aria-live", "polite");
    doc.body.appendChild(controls);
    render();
  }
  function invalidate() { invalidContext = true; stop(); render(); }
  function bind() {
    listen(doc, "click", (event) => {
      if (!permitted()) return;
      const target = event.target?.closest?.("[data-ov-analytics-action], a[href]");
      if (!target || target.closest?.("#ov-analytics-privacy, .ph-no-capture")) return;
      let action = target.getAttribute("data-ov-analytics-action");
      if (!action) {
        try {
          const url = new URL(target.getAttribute("href"), win.location.origin);
          if (url.origin === win.location.origin) action = publicPage(url);
        } catch { return; }
      }
      if (ACTIONS.has(action)) capture("marketing_click", { action });
    });
    listen(win, "error", (event) => capture("marketing_error", { kind: event.target && event.target !== win ? "resource" : "script" }));
    listen(win, "unhandledrejection", () => capture("marketing_error", { kind: "unhandled_rejection" }));
    listen(win, "storage", (event) => {
      if (event.key !== null && event.key !== CONSENT_KEY) return;
      stop(); consent = readChoice() || { ...DENIED }; durableChoice = true; render(); void start();
    });
    listen(win, "pagehide", invalidate);
    listen(win, "popstate", invalidate);
    listen(win, "hashchange", invalidate);
    listen(win, "focus", () => { if (!page()) stop(); render(); });
    listen(doc, "visibilitychange", () => { if (!page()) stop(); render(); });
    // This is a multi-page site. Stop BEFORE any SPA mutation, not after replay
    // has observed the destination DOM. A full document load can start afresh.
    for (const name of ["pushState", "replaceState"]) {
      const original = win.history[name];
      function wrapped(...args) { invalidate(); return original.apply(this, args); }
      win.history[name] = wrapped;
      removers.push(() => { if (win.history[name] === wrapped) win.history[name] = original; });
    }
  }
  async function initialize() {
    if (disposed || !publicPage(win.location)) return api;
    try {
      const response = await fetcher("/analytics/config", { credentials: "omit", cache: "no-store", redirect: "error", referrerPolicy: "no-referrer", headers: { Accept: "application/json" } });
      if (!response.ok || disposed) return api;
      config = validateConfig(await response.json());
      if (!config || !config.public_pages.includes(win.location.pathname) || !publicPage(win.location) || disposed) return api;
      const saved = readChoice();
      durableChoice = !!saved;
      consent = saved || { ...DENIED };
      consent.replay = consent.replay && config.replay_enabled;
      mount(); bind(); await start();
    } catch { stop(); }
    return api;
  }
  const api = {
    init: () => (initialized ||= initialize()),
    setConsent,
    withdraw: () => setConsent(DENIED),
    setPublicSession(value) { publicSession = value === true; if (!publicSession) stop(); render(); if (publicSession) void start(); },
    refreshContext() { if (!page()) stop(); render(); },
    openPreferences() { if (controls && !controls.hidden) { controls.open = true; controls.querySelector("summary")?.focus(); } },
    dispose() { disposed = true; stop(); removers.forEach((remove) => remove()); controls?.remove(); },
  };
  return api;
}

let singleton;
export function bootstrapAnalytics() {
  singleton ||= createAnalytics();
  return singleton.init();
}
