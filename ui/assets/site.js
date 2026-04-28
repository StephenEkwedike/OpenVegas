const NAV_LINKS = [
  ["/ui", "Home"],
  ["/ui/how-it-works", "How it works"],
  ["/ui/pricing", "Pricing"],
  ["/ui/balance", "Balance"],
  ["/ui/faq", "FAQ"],
  ["/ui/how-to-play", "HOW TO PLAY"],
  ["/ui/contact", "Contact"]
];

const THEME_KEY = "ov_theme";
const THEME_DEFAULT = "light";
const THEME_TOGGLE_ID = "ov-theme-toggle";
const NAV_AUTH_ID = "ov-nav-auth";
const PROFILE_PREFS_ENDPOINT = "/ui/profile/preferences";
let assetGuardInstalled = false;
let storageListenerInstalled = false;
let profileThemeSyncAttempted = false;
let navAuthHydrationInFlight = false;
let navAuthListenerBound = false;

function normalizeTheme(value) {
  return String(value || "").toLowerCase() === "dark" ? "dark" : "light";
}

function applyFaviconTheme(theme) {
  var normalized = normalizeTheme(theme);
  var isDark = normalized === "dark";
  var svgBase = isDark ? "/ui/assets/brand/favicon-dark.svg" : "/ui/assets/brand/favicon.svg";
  var jpgBase = isDark ? "/ui/assets/brand/favicon-dark.jpg" : "/ui/assets/brand/favicon.jpg";
  var appleBase = isDark ? "/ui/assets/brand/openvegas-logo-circle-dark.jpg" : "/ui/assets/brand/openvegas-logo-circle.jpg";
  var svgHref = svgBase + "?theme=" + normalized;
  var jpgHref = jpgBase + "?theme=" + normalized;
  var appleHref = appleBase + "?theme=" + normalized;

  function refreshLink(node, href) {
    if (!node) return;
    node.setAttribute("href", href);
    var clone = node.cloneNode(true);
    if (node.parentNode) node.parentNode.replaceChild(clone, node);
  }

  function ensureLink(rel, type, href) {
    var selector = 'link[rel="' + rel + '"]' + (type ? '[type="' + type + '"]' : "");
    var node = document.querySelector(selector);
    if (!node) {
      node = document.createElement("link");
      node.setAttribute("rel", rel);
      if (type) node.setAttribute("type", type);
      node.setAttribute("href", href);
      document.head.appendChild(node);
    }
    return node;
  }

  Array.prototype.forEach.call(
    document.querySelectorAll('link[rel="alternate icon"][type="image/jpeg"]'),
    function (node) {
      node.setAttribute("rel", "icon");
    }
  );

  var svg = ensureLink("icon", "image/svg+xml", svgHref);
  refreshLink(svg, svgHref);

  var jpg = ensureLink("icon", "image/jpeg", jpgHref);
  refreshLink(jpg, jpgHref);

  var shortcut = ensureLink("shortcut icon", "image/jpeg", jpgHref);
  refreshLink(shortcut, jpgHref);

  var apple = ensureLink("apple-touch-icon", null, appleHref);
  refreshLink(apple, appleHref);
}

function getStoredTheme() {
  try {
    return normalizeTheme(window.localStorage.getItem(THEME_KEY) || THEME_DEFAULT);
  } catch {
    return THEME_DEFAULT;
  }
}

function setStoredTheme(theme) {
  try {
    window.localStorage.setItem(THEME_KEY, normalizeTheme(theme));
  } catch {
    // no-op: private mode or disabled storage
  }
}

function updateThemeToggleState() {
  const toggle = document.getElementById(THEME_TOGGLE_ID);
  if (!toggle) return;
  const dark = currentTheme() === "dark";
  toggle.setAttribute("aria-checked", String(dark));
  toggle.setAttribute("title", dark ? "Switch to light mode" : "Switch to dark mode");
}

export function currentTheme() {
  const fromDom = document.documentElement?.dataset?.theme;
  return normalizeTheme(fromDom || THEME_DEFAULT);
}

export function applyTheme(theme) {
  const normalized = normalizeTheme(theme);
  document.documentElement.dataset.theme = normalized;
  document.documentElement.style.colorScheme = normalized;
  applyFaviconTheme(normalized);
  updateThemeToggleState();
}

export async function toggleTheme() {
  const next = currentTheme() === "light" ? "dark" : "light";
  applyTheme(next);
  setStoredTheme(next);
  void saveProfileTheme(next);
  return next;
}

async function syncThemeFromProfile(cachedTheme) {
  if (profileThemeSyncAttempted) return;
  profileThemeSyncAttempted = true;
  try {
    const auth = await import("/ui/assets/page-auth.js?v=20260330");
    if (typeof auth.bootstrapBrowserSession === "function") {
      await auth.bootstrapBrowserSession();
    }
    const token = typeof auth.getBrowserToken === "function" ? auth.getBrowserToken() : "";
    if (!token) return;
    const headers = typeof auth.authHeaders === "function"
      ? auth.authHeaders({ Accept: "application/json" })
      : new Headers({ Accept: "application/json" });
    const res = await fetch(PROFILE_PREFS_ENDPOINT, {
      method: "GET",
      credentials: "same-origin",
      headers
    });
    if (!res.ok) return;
    const prefs = await res.json().catch(() => ({}));
    const rawTheme = String(prefs?.theme || "").trim();
    if (!rawTheme) return;
    const serverTheme = normalizeTheme(rawTheme);
    if (serverTheme !== normalizeTheme(cachedTheme)) {
      applyTheme(serverTheme);
      setStoredTheme(serverTheme);
    }
  } catch {
    // Best-effort profile sync; local cache remains usable.
  }
}

async function saveProfileTheme(theme) {
  try {
    const auth = await import("/ui/assets/page-auth.js?v=20260330");
    if (typeof auth.bootstrapBrowserSession === "function") {
      await auth.bootstrapBrowserSession();
    }
    const token = typeof auth.getBrowserToken === "function" ? auth.getBrowserToken() : "";
    if (!token) return;
    const headers = typeof auth.authHeaders === "function"
      ? auth.authHeaders({ "Content-Type": "application/json" })
      : new Headers({ "Content-Type": "application/json" });
    await fetch(PROFILE_PREFS_ENDPOINT, {
      method: "PATCH",
      credentials: "same-origin",
      headers,
      body: JSON.stringify({ theme: normalizeTheme(theme) })
    });
  } catch {
    // Fire-and-forget profile save.
  }
}

function ensureThemeStorageSync() {
  if (storageListenerInstalled) return;
  storageListenerInstalled = true;
  window.addEventListener("storage", (event) => {
    if (event.key !== THEME_KEY) return;
    applyTheme(normalizeTheme(event.newValue || THEME_DEFAULT));
  });
}

function bindThemeToggle(toggle) {
  if (!toggle) return;
  if (toggle.dataset.ovThemeBound === "1") return;
  toggle.addEventListener("click", () => {
    void toggleTheme();
  });
  toggle.dataset.ovThemeBound = "1";
}

function buildThemeToggle() {
  const button = document.createElement("button");
  button.type = "button";
  button.id = THEME_TOGGLE_ID;
  button.className = "theme-toggle";
  button.setAttribute("role", "switch");
  button.setAttribute("aria-label", "Toggle light and dark mode");
  button.innerHTML = `
    <span class="theme-toggle__icon" aria-hidden="true">☀</span>
    <span class="theme-toggle__rail" aria-hidden="true"><span class="theme-toggle__thumb"></span></span>
    <span class="theme-toggle__icon" aria-hidden="true">☾</span>
  `;
  bindThemeToggle(button);
  return button;
}

export function renderThemeToggle() {
  const existing = document.getElementById(THEME_TOGGLE_ID);
  const topNav = document.querySelector(".top-nav");
  const preferredHost =
    topNav?.querySelector(".ov-nav-right") ||
    topNav?.querySelector(".founder-links")?.parentElement ||
    topNav ||
    document.body ||
    document.documentElement;
  if (!preferredHost) return;

  const toggle = existing || buildThemeToggle();
  if (!existing || toggle.parentElement !== preferredHost) {
    preferredHost.appendChild(toggle);
  }

  if (preferredHost === document.body || preferredHost === document.documentElement) {
    toggle.classList.add("theme-toggle--floating");
  } else {
    toggle.classList.remove("theme-toggle--floating");
  }

  bindThemeToggle(toggle);
  updateThemeToggleState();
}

function currentUiPath() {
  const pathname = window.location.pathname || "/ui";
  const search = window.location.search || "";
  const hash = window.location.hash || "";
  if (pathname === "/ui/login") {
    const params = new URLSearchParams(search);
    const candidate = String(params.get("next") || "").trim();
    if (candidate.startsWith("/ui") && !candidate.startsWith("/ui/login")) {
      return candidate;
    }
    return "/ui";
  }
  const p = `${pathname}${search}${hash}`;
  return p.startsWith("/ui") ? p : "/ui";
}

function buildNavAuthButtons(nextPath) {
  const wrap = document.createElement("div");
  wrap.id = NAV_AUTH_ID;
  wrap.className = "nav-auth";
  wrap.dataset.ovNavAuth = "1";
  const next = encodeURIComponent(nextPath || "/ui");
  wrap.innerHTML = `
    <a class="btn btn-primary" href="/ui/login?next=${next}">OpenVegas login</a>
    <a class="btn" href="/ui/login?mode=signup&next=${next}">OpenVegas signup</a>
  `;
  return wrap;
}

function shortenEmailForNav(email) {
  const value = String(email || "").trim();
  if (!value) return "unknown";
  return value.length > 8 ? `${value.slice(0, 8)}...` : value;
}

function renderSignedOutNav(authNode, nextPath) {
  const next = encodeURIComponent(nextPath || "/ui");
  authNode.innerHTML = `
    <a class="btn btn-primary" href="/ui/login?next=${next}">OpenVegas login</a>
    <a class="btn" href="/ui/login?mode=signup&next=${next}">OpenVegas signup</a>
  `;
}

function renderSignedInNav(authNode, summary) {
  const email = shortenEmailForNav(summary?.email || "");
  authNode.innerHTML = `
    <span class="nav-auth-user text-mono-sm">Signed in as ${email}</span>
    <button type="button" class="btn" data-ov-logout="1">Logout</button>
  `;
}

async function hydrateNavAuthNode(authNode, nextPath) {
  if (!authNode || navAuthHydrationInFlight) return;
  navAuthHydrationInFlight = true;
  try {
    const auth = await import("/ui/assets/page-auth.js?v=20260330");
    const summary = await auth.resolveSessionSummary({ refresh: true });
    if (summary?.signed_in) {
      renderSignedInNav(authNode, summary);
      const logoutBtn = authNode.querySelector("[data-ov-logout='1']");
      if (logoutBtn && logoutBtn.dataset.bound !== "1") {
        logoutBtn.addEventListener("click", async () => {
          await auth.logoutBrowserSession();
          renderSignedOutNav(authNode, nextPath);
          window.location.reload();
        });
        logoutBtn.dataset.bound = "1";
      }
    } else {
      renderSignedOutNav(authNode, nextPath);
    }
    if (!navAuthListenerBound && typeof auth.subscribeAuthState === "function") {
      auth.subscribeAuthState(async () => {
        const latest = await auth.resolveSessionSummary({ refresh: false });
        if (latest?.signed_in) {
          renderSignedInNav(authNode, latest);
          const logoutBtn = authNode.querySelector("[data-ov-logout='1']");
          if (logoutBtn && logoutBtn.dataset.bound !== "1") {
            logoutBtn.addEventListener("click", async () => {
              await auth.logoutBrowserSession();
              renderSignedOutNav(authNode, nextPath);
              window.location.reload();
            });
            logoutBtn.dataset.bound = "1";
          }
        } else {
          renderSignedOutNav(authNode, nextPath);
        }
      });
      navAuthListenerBound = true;
    }
  } catch {
    renderSignedOutNav(authNode, nextPath);
  } finally {
    navAuthHydrationInFlight = false;
  }
}

function ensureNavRightCluster(nav) {
  const topNav = nav?.closest?.(".top-nav");
  if (!topNav) return null;
  const existing = topNav.querySelector(".ov-nav-right");
  if (existing) return existing;
  const parent = nav.parentElement;
  if (parent && parent !== topNav) {
    parent.classList.add("ov-nav-right");
    return parent;
  }
  const cluster = document.createElement("div");
  cluster.className = "ov-nav-right";
  nav.replaceWith(cluster);
  cluster.appendChild(nav);
  topNav.appendChild(cluster);
  return cluster;
}

function renderNavAuthButtons(nav) {
  const rightCluster = ensureNavRightCluster(nav);
  if (!rightCluster) return;
  const nextPath = currentUiPath();
  const existing = document.getElementById(NAV_AUTH_ID);
  const authNode = existing || buildNavAuthButtons(nextPath);
  if (!existing) {
    rightCluster.appendChild(authNode);
  }
  renderSignedOutNav(authNode, nextPath);
  void hydrateNavAuthNode(authNode, nextPath);
  const themeToggle = document.getElementById(THEME_TOGGLE_ID);
  if (themeToggle && authNode.nextElementSibling !== themeToggle) {
    rightCluster.insertBefore(authNode, themeToggle);
  } else if (!themeToggle && authNode.parentElement !== rightCluster) {
    rightCluster.appendChild(authNode);
  }
}

function initTheme() {
  const cachedTheme = getStoredTheme();
  applyTheme(cachedTheme);
  ensureThemeStorageSync();
  void syncThemeFromProfile(cachedTheme);
}

export function renderTopNav(targetId = "siteNav") {
  const nav = document.getElementById(targetId);
  if (!nav) return;
  const current = window.location.pathname;
  const existingAnchors = Array.from(nav.querySelectorAll("a[href]"));
  if (existingAnchors.length) {
    for (const anchor of existingAnchors) {
      const href = anchor.getAttribute("href") || "";
      const active = href === current;
      anchor.classList.toggle("active", active);
    }
  } else {
    nav.innerHTML = NAV_LINKS.map(([href, label]) => {
      const active = current === href;
      return `<a href="${href}" class="${active ? "active" : ""}">${label}</a>`;
    }).join("");
  }
  renderNavAuthButtons(nav);
  renderThemeToggle();
}

export function renderFounderLinks(targetId = "founderLinks") {
  const node = document.getElementById(targetId);
  if (!node) return;
  node.innerHTML = `<a href="/ui/slidedeck/00-founders.html" class="text-mono-sm">[ FOUNDER DECK ]</a>`;
}

export function installAssetGuard() {
  if (typeof window === "undefined") return;
  renderThemeToggle();
  if (assetGuardInstalled) return;
  assetGuardInstalled = true;
  window.addEventListener(
    "error",
    (e) => {
      const src = String(e?.target?.src || e?.target?.href || "");
      if (!src.includes("/ui/assets/")) return;
      if (document.getElementById("asset-guard-banner")) return;
      const n = document.createElement("div");
      n.id = "asset-guard-banner";
      n.textContent = "UI asset failed to load. Check /ui/assets paths.";
      n.style.cssText =
        "position:fixed;top:0;left:0;right:0;padding:8px;background:#ff7b7b;color:#040404;z-index:9999;font-family:monospace";
      document.body.appendChild(n);
    },
    true,
  );
}

export function byId(id) {
  return document.getElementById(id);
}

if (typeof window !== "undefined") {
  initTheme();
}

if (typeof window !== "undefined" && typeof window.openvegasEmitMetric !== "function") {
  window.openvegasEmitMetric = function (_name, _tags) {
    // Browser metric noop; backend remains source of truth.
  };
}
