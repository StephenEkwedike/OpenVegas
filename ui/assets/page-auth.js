let accessToken = "";
let accessExpUnix = 0;
let refreshInFlight = null;
let bootstrapAttempted = false;
let walletBootstrapDone = false;
let authState = "signed_out"; // signed_out | refreshing | signed_in
let authStateVersion = 0;
let confirmedAnonymous = false;
let authGeneration = 0;
let invalidationChannel = null;
let invalidationStorage = null;
const seenInvalidations = new Set();
const authStateListeners = new Set();
const SESSION_STORAGE_KEY = "ov_browser_access_session";
const INVALIDATION_KEY = "ov_auth_invalidation_v1";

function nowUnix() {
  return Math.floor(Date.now() / 1000);
}

function setAuthState(next) {
  authStateVersion += 1;
  authState = next;
  for (const listener of authStateListeners) {
    try {
      listener({ authState, version: authStateVersion });
    } catch {
      // Best-effort callback fanout.
    }
  }
}

export function subscribeAuthState(listener) {
  if (typeof listener !== "function") return () => {};
  authStateListeners.add(listener);
  return () => authStateListeners.delete(listener);
}

export function getAuthStateSnapshot() {
  return { authState, version: authStateVersion };
}

function normalizeToken(raw) {
  const v = String(raw || "").trim();
  if (!v) return "";
  return v.toLowerCase().startsWith("bearer ") ? v.slice(7).trim() : v;
}

function decodeJwtPayload(token) {
  const raw = String(token || "").trim();
  if (!raw || !raw.includes(".")) return {};
  try {
    const payloadPart = raw.split(".")[1] || "";
    const b64 = payloadPart.replace(/-/g, "+").replace(/_/g, "/");
    const pad = b64.length % 4 === 0 ? "" : "=".repeat(4 - (b64.length % 4));
    const json = atob(`${b64}${pad}`);
    return JSON.parse(json);
  } catch {
    return {};
  }
}

function normalizeExpiresAt(raw) {
  const n = Number(raw || 0);
  if (!Number.isFinite(n) || n <= 0) return 0;
  return Math.floor(n);
}

function loadSessionFromStorage() {
  try {
    const raw = window.sessionStorage.getItem(SESSION_STORAGE_KEY);
    if (!raw) return;
    const parsed = JSON.parse(raw);
    const token = normalizeToken(parsed?.accessToken);
    const exp = normalizeExpiresAt(parsed?.expiresAt);
    if (!token) return;
    accessToken = token;
    accessExpUnix = exp;
    authState = "signed_in";
  } catch {
    // Ignore malformed or unavailable storage.
  }
}

function persistSessionToStorage() {
  try {
    if (!accessToken) {
      window.sessionStorage.removeItem(SESSION_STORAGE_KEY);
      return;
    }
    window.sessionStorage.setItem(
      SESSION_STORAGE_KEY,
      JSON.stringify({ accessToken, expiresAt: accessExpUnix || 0 }),
    );
  } catch {
    // Ignore storage failures (private mode, disabled storage).
  }
}

function setAccessToken(rawToken, expiresAtUnix = 0) {
  confirmedAnonymous = false;
  accessToken = normalizeToken(rawToken);
  accessExpUnix = normalizeExpiresAt(expiresAtUnix);
  persistSessionToStorage();
  setAuthState(accessToken ? "signed_in" : "signed_out");
}

function emit_metric(_name, _tags) {
  // Browser-side metric emission is optional; server refresh route records canonical metrics.
}

function expiresSoon(leewaySec = 300) {
  if (!accessToken) return true;
  if (!accessExpUnix) return true;
  return (accessExpUnix - nowUnix()) <= leewaySec;
}

async function refreshAccessToken(trigger = "retry_401", opts = {}) {
  if (refreshInFlight) return refreshInFlight.promise;
  const timeoutMs = Math.min(4000, Math.max(1, Number(opts.timeoutMs) || 4000));
  const generation = authGeneration;
  const ctrl = new AbortController();
  const cancelled = new Promise((_, reject) => {
    ctrl.signal.addEventListener("abort", () => reject(new Error("refresh_cancelled")), { once: true });
  });
  const operation = { cancel() { ctrl.abort(); } };
  const current = () => generation === authGeneration && !ctrl.signal.aborted;
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  // Defer work until the operation is installed, including for reentrant subscribers.
  const request = Promise.resolve().then(async () => {
      if (!current()) throw new Error("refresh_cancelled");
      const res = await fetch("/ui/auth/refresh", {
        method: "POST",
        credentials: "include",
        headers: {
          "Content-Type": "application/json",
          "X-OpenVegas-Refresh-Trigger": trigger,
        },
        signal: ctrl.signal,
      });
      if (!res.ok) {
        const rejection = await res.json().catch(() => ({}));
        if (!current()) throw new Error("refresh_cancelled");
        // A network failure or expired credential is not consent to record a user.
        confirmedAnonymous = res.status === 401 && rejection.detail === "No refresh session" && !accessToken;
        emit_metric("auth_refresh_attempt_total", {
          surface: "browser",
          trigger,
          outcome: "failure",
          reason: "refresh_rejected",
        });
        throw new Error(`refresh_failed_${res.status}`);
      }
      const body = await res.json().catch(() => ({}));
      if (!current()) throw new Error("refresh_cancelled");
      const token = normalizeToken(body?.access_token);
      const expUnix = normalizeExpiresAt(body?.expires_at);
      if (!token || !expUnix) {
        emit_metric("auth_refresh_attempt_total", {
          surface: "browser",
          trigger,
          outcome: "failure",
          reason: "refresh_malformed",
        });
        throw new Error("refresh_malformed");
      }
      setAccessToken(token, expUnix);
      emit_metric("auth_refresh_attempt_total", {
        surface: "browser",
        trigger,
        outcome: "success",
      });
      return token;
  });
  operation.promise = Promise.race([request, cancelled]).catch((err) => {
      if (generation === authGeneration) {
        accessToken = "";
        accessExpUnix = 0;
        walletBootstrapDone = false;
        persistSessionToStorage();
        setAuthState("signed_out");
      }
      throw err;
    }).finally(() => {
      clearTimeout(timer);
      if (refreshInFlight === operation) refreshInFlight = null;
    });
  refreshInFlight = operation;
  confirmedAnonymous = false;
  setAuthState("refreshing");
  return operation.promise;
}

function invalidateSession() {
  authGeneration += 1;
  refreshInFlight?.cancel();
  refreshInFlight = null;
  confirmedAnonymous = false;
  accessToken = "";
  accessExpUnix = 0;
  bootstrapAttempted = false;
  walletBootstrapDone = false;
  persistSessionToStorage();
  setAuthState("signed_out");
}

function rememberInvalidation(nonce) {
  seenInvalidations.add(nonce);
  if (seenInvalidations.size > 32) seenInvalidations.delete(seenInvalidations.values().next().value);
}

function broadcastInvalidation() {
  // Ephemeral deduplication only: never transmit a token, account, URL or auth state.
  const message = { type: "auth-invalidated", nonce: `${Date.now()}-${Math.random()}` };
  rememberInvalidation(message.nonce);
  try { invalidationChannel?.postMessage(message); } catch { invalidationChannel = null; }
  try {
    invalidationStorage?.setItem(INVALIDATION_KEY, JSON.stringify(message));
    invalidationStorage?.removeItem(INVALIDATION_KEY);
  } catch { invalidationStorage = null; }
  if (!invalidationChannel && !invalidationStorage) setAuthState(authState);
}

function receiveInvalidation(message) {
  if (!message || message.type !== "auth-invalidated" || typeof message.nonce !== "string"
      || message.nonce.length > 80 || Object.keys(message).length !== 2
      || seenInvalidations.has(message.nonce)) return;
  rememberInvalidation(message.nonce);
  invalidateSession();
  if (!browserHidden()) void refreshSessionSummary();
}

function browserHidden() {
  return typeof document !== "undefined" && document.visibilityState === "hidden";
}

export function isAnalyticsSessionReady() {
  return Boolean(invalidationChannel || invalidationStorage) && !browserHidden();
}

// Unlike bootstrap, this always checks the shared HTTP-only session with the server.
export async function refreshSessionSummary() {
  bootstrapAttempted = true;
  try { await refreshAccessToken("session_check", { timeoutMs: 2500 }); } catch { /* Unknown stays off. */ }
  return getSessionSummary();
}

function installSessionInvalidation() {
  try {
    invalidationChannel = new window.BroadcastChannel(INVALIDATION_KEY);
    invalidationChannel.onmessage = (event) => receiveInvalidation(event.data);
  } catch { invalidationChannel = null; }
  try {
    const storage = window.localStorage;
    storage.setItem(`${INVALIDATION_KEY}_probe`, "1");
    storage.removeItem(`${INVALIDATION_KEY}_probe`);
    invalidationStorage = storage;
  } catch { invalidationStorage = null; }
  window.addEventListener?.("storage", (event) => {
    if (event.key !== INVALIDATION_KEY || !event.newValue || event.newValue.length > 256) return;
    try { receiveInvalidation(JSON.parse(event.newValue)); } catch { /* Ignore malformed notices. */ }
  });
  const recheck = () => {
    if (browserHidden() || refreshInFlight) return;
    invalidateSession();
    void refreshSessionSummary();
  };
  window.addEventListener?.("focus", recheck);
  window.addEventListener?.("pageshow", (event) => { if (event.persisted) recheck(); });
  if (typeof document !== "undefined") document.addEventListener?.("visibilitychange", () => {
    if (browserHidden()) invalidateSession();
    else recheck();
  });
}

export function getBrowserToken() {
  return accessToken;
}

export function isConfirmedAnonymous() {
  return isAnalyticsSessionReady() && confirmedAnonymous && !accessToken && authState === "signed_out";
}

export function setBrowserToken(rawToken, expiresAtUnix = 0) {
  const token = normalizeToken(rawToken);
  if (!token) return false;
  invalidateSession();
  setAccessToken(token, expiresAtUnix);
  broadcastInvalidation();
  return true;
}

export function clearBrowserToken() {
  invalidateSession();
  broadcastInvalidation();
}

export async function logoutBrowserSession() {
  clearBrowserToken();
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 4000);
  try {
    await fetch("/ui/auth/logout", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      signal: ctrl.signal,
    });
  } catch {
    // Best-effort remote logout; local clear always runs.
  } finally {
    clearTimeout(timer);
  }
  clearBrowserToken();
}

export async function bootstrapBrowserSession() {
  if (bootstrapAttempted) return Boolean(accessToken);
  bootstrapAttempted = true;
  try {
    await refreshAccessToken("bootstrap", { timeoutMs: 2500 });
    return true;
  } catch {
    return false;
  }
}

export function getSessionSummary() {
  const token = getBrowserToken();
  const claims = decodeJwtPayload(token);
  return {
    signed_in: Boolean(token),
    email: String(claims?.email || claims?.user_metadata?.email || "").trim(),
    user_id: String(claims?.sub || "").trim(),
    expires_at: accessExpUnix || 0,
  };
}

export async function resolveSessionSummary({ refresh = false } = {}) {
  if (refresh && (!accessToken || expiresSoon(0))) {
    await bootstrapBrowserSession();
  }
  return getSessionSummary();
}

export function getLoginHref(nextPath) {
  const fallback = `${window.location.pathname}${window.location.search}${window.location.hash}`;
  const next = String(nextPath || fallback || "/ui/balance");
  return `/ui/login?next=${encodeURIComponent(next)}`;
}

export function getSignupHref(nextPath) {
  const fallback = `${window.location.pathname}${window.location.search}${window.location.hash}`;
  const next = String(nextPath || fallback || "/ui/balance");
  return `/ui/login?mode=signup&next=${encodeURIComponent(next)}`;
}

export function authHeaders(extraHeaders = {}) {
  const headers = new Headers(extraHeaders || {});
  const token = getBrowserToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  return headers;
}

function withAuthInit(init = {}) {
  return {
    ...init,
    cache: init.cache || "no-store",
    credentials: init.credentials || "same-origin",
    headers: authHeaders(init.headers),
  };
}

export async function apiFetch(url, init = {}) {
  if (expiresSoon(300)) {
    try {
      await refreshAccessToken("proactive");
    } catch {
      // Allow request path + retry logic to decide final auth result.
    }
  }

  const first = await fetch(url, withAuthInit(init));
  if (first.status !== 401 && first.status !== 403) return first;

  try {
    await refreshAccessToken("retry_401");
  } catch {
    clearBrowserToken();
    return first;
  }
  return fetch(url, withAuthInit(init));
}

async function ensureWalletBootstrap() {
  if (walletBootstrapDone) return;
  const res = await apiFetch("/wallet/bootstrap", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
  });
  if (res.status === 401 || res.status === 403) {
    clearBrowserToken();
    throw new Error("wallet_bootstrap_unauthorized");
  }
  if (!res.ok) {
    throw new Error(`wallet_bootstrap_failed:${res.status}`);
  }
  walletBootstrapDone = true;
}

export async function apiJson(url, init = {}) {
  const res = await apiFetch(url, init);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const error = new Error(`${res.status}: ${JSON.stringify(body)}`);
    error.status = res.status;
    error.body = body;
    throw error;
  }
  return body;
}

export async function authProbe() {
  if (!accessToken || expiresSoon(0)) {
    await bootstrapBrowserSession();
  }
  if (!accessToken) {
    return { ok: false, status: 401, data: null, reason: "missing_token" };
  }

  await ensureWalletBootstrap();

  const res = await apiFetch("/wallet/balance", { method: "GET" });
  if (res.status === 401 || res.status === 403) {
    clearBrowserToken();
    return { ok: false, status: res.status, data: null, reason: "unauthorized" };
  }
  if (!res.ok) {
    throw new Error(`Auth probe failed: ${res.status}`);
  }
  const data = await res.json();
  return { ok: true, status: res.status, data, reason: null };
}

export function showSignedOutPanel({ panelId = "authState", contentId = "authContent" } = {}) {
  const panel = document.getElementById(panelId);
  const content = document.getElementById(contentId);
  if (panel) panel.hidden = false;
  if (content) content.hidden = true;
}

export function showAuthedContent({ panelId = "authState", contentId = "authContent" } = {}) {
  const panel = document.getElementById(panelId);
  const content = document.getElementById(contentId);
  if (panel) panel.hidden = true;
  if (content) content.hidden = false;
}

if (typeof window !== "undefined") {
  loadSessionFromStorage();
  installSessionInvalidation();
}
