let accessToken = "";
let accessExpUnix = 0;
let refreshInFlight = null;
let bootstrapAttempted = false;
let walletBootstrapDone = false;
let authState = "signed_out"; // signed_out | refreshing | signed_in
let authStateVersion = 0;
const authStateListeners = new Set();
const SESSION_STORAGE_KEY = "ov_browser_access_session";

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

function normalizeToken(raw) {
  const v = String(raw || "").trim();
  if (!v) return "";
  return v.toLowerCase().startsWith("bearer ") ? v.slice(7).trim() : v;
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
  const timeoutMs = Number(opts.timeoutMs || 4000);
  if (refreshInFlight) return refreshInFlight;

  refreshInFlight = (async () => {
    setAuthState("refreshing");
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
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
        emit_metric("auth_refresh_attempt_total", {
          surface: "browser",
          trigger,
          outcome: "failure",
          reason: "refresh_rejected",
        });
        throw new Error(`refresh_failed_${res.status}`);
      }
      const body = await res.json().catch(() => ({}));
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
    } catch (err) {
      if (String(err).includes("AbortError")) {
        emit_metric("auth_refresh_attempt_total", {
          surface: "browser",
          trigger,
          outcome: "failure",
          reason: "refresh_timeout",
        });
        throw new Error("refresh_timeout");
      }
      throw err;
    } finally {
      clearTimeout(timer);
    }
  })();

  try {
    return await refreshInFlight;
  } finally {
    refreshInFlight = null;
  }
}

export function getBrowserToken() {
  return accessToken;
}

export function setBrowserToken(rawToken, expiresAtUnix = 0) {
  const token = normalizeToken(rawToken);
  if (!token) return false;
  setAccessToken(token, expiresAtUnix);
  return true;
}

export function clearBrowserToken() {
  accessToken = "";
  accessExpUnix = 0;
  walletBootstrapDone = false;
  persistSessionToStorage();
  setAuthState("signed_out");
}

export async function bootstrapBrowserSession() {
  if (bootstrapAttempted) return Boolean(accessToken);
  bootstrapAttempted = true;
  try {
    await refreshAccessToken("bootstrap", { timeoutMs: 2500 });
    return true;
  } catch {
    clearBrowserToken();
    return false;
  }
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
}
