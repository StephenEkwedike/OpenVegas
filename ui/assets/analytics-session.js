// Read only auth readiness. Never pass identity or credentials to analytics.
export function analyticsSessionBridge(auth, product, marketing) {
  let epoch = 0, previous = null;
  return {
    update() {
      const state = auth.getAuthStateSnapshot();
      const available = auth.isAnalyticsSessionReady?.() !== false;
      const anonymous = available && auth.isConfirmedAnonymous();
      marketing?.setPublicSession(anonymous);
      const ready = available && state?.authState !== "refreshing" && (anonymous ||
        (state?.authState === "signed_in" && auth.getSessionSummary()?.signed_in === true));
      const key = `${state?.version}:${state?.authState}:${ready}`;
      if (key === previous) return;
      previous = key;
      product?.setSessionReady(ready, ++epoch);
    },
    stop() {
      previous = null;
      marketing?.setPublicSession(false);
      product?.setSessionReady(false, ++epoch);
    },
  };
}

// Literal imports keep the production asset closure verifiable at build time.
function loadAnalyticsModule(url) {
  switch (url) {
    case "/ui/assets/page-auth.js?v=20260330": return import("/ui/assets/page-auth.js?v=20260330");
    case "/ui/assets/product-analytics.js?v=20261020": return import("/ui/assets/product-analytics.js?v=20261020");
    case "/ui/assets/analytics.js?v=20261020": return import("/ui/assets/analytics.js?v=20261020");
    default: return Promise.reject(new Error("unsupported_analytics_module"));
  }
}

// Each controller owns its deadline and cancellation. Neither waits for its peer.
export function startAnalyticsSession({ window: win = window, document: doc = document,
  loadModule = loadAnalyticsModule, timeoutMs = 4000 } = {}) {
  const deadlineMs = Math.min(10000, Math.max(1, Number(timeoutMs) || 4000));
  const cleanups = new Set(), cancellations = new Set();
  let stopped = false, authPromise;

  function bounded(work, cleanup = () => {}) {
    let active = true, rejectCancelled;
    const cancelled = new Promise((_, reject) => { rejectCancelled = reject; });
    const cancel = () => {
      if (!active) return;
      active = false;
      cleanup();
      rejectCancelled(new Error("analytics_initialization_cancelled"));
    };
    cancellations.add(cancel);
    const timer = win.setTimeout(cancel, deadlineMs);
    return Promise.race([
      Promise.resolve().then(() => work(() => active && !stopped)), cancelled,
    ]).finally(() => {
      active = false;
      win.clearTimeout(timer);
      cancellations.delete(cancel);
    });
  }

  function authReady() {
    return authPromise ||= bounded(async (active) => {
      const auth = await loadModule("/ui/assets/page-auth.js?v=20260330");
      if (!active()) throw new Error("analytics_initialization_cancelled");
      await auth.refreshSessionSummary();
      if (!active()) throw new Error("analytics_initialization_cancelled");
      return auth;
    });
  }

  async function initialize(kind) {
    let controller, bridge, unsubscribe;
    const configAbort = new win.AbortController();
    const cleanup = () => {
      configAbort.abort();
      unsubscribe?.();
      if (bridge) doc.removeEventListener("submit", bridge.stop, true);
      try { bridge?.stop(); } catch { /* Still dispose a failed controller. */ }
      try { controller?.dispose(); } catch { /* A peer must still be cleaned up. */ }
    };
    cleanups.add(cleanup);
    try {
      return await bounded(async (active) => {
        const product = kind === "product";
        const module = await loadModule(product
          ? "/ui/assets/product-analytics.js?v=20261020" : "/ui/assets/analytics.js?v=20261020");
        if (!active()) return "cancelled";
        if (!(product ? module.productPage : module.publicPage)(win.location)) {
          cleanup();
          return "excluded";
        }
        // Hold the handle before init so even a hung config body can be retired.
        controller = (product ? module.createProductAnalytics : module.createAnalytics)({
          window: win, document: doc,
          fetch: (url, options) => win.fetch(url, url === "/analytics/config"
            ? { ...options, signal: configAbort.signal } : options),
        });
        const initialized = controller.init().then(() => {
          if (!active()) return;
          // Controllers wrap shared history methods; unwind in reverse bind order.
          cleanups.delete(cleanup);
          cleanups.add(cleanup);
        });
        const [, auth] = await Promise.all([initialized, authReady()]);
        if (!active()) return "cancelled";
        bridge = analyticsSessionBridge(auth, product ? controller : null, product ? null : controller);
        doc.addEventListener("submit", bridge.stop, true);
        unsubscribe = auth.subscribeAuthState(bridge.update);
        bridge.update();
        return "initialized";
      }, cleanup);
    } catch {
      cleanup();
      return "failed";
    }
  }

  function dispose() {
    if (stopped) return;
    stopped = true;
    for (const cleanup of [...cleanups].reverse()) cleanup();
    for (const cancel of [...cancellations]) cancel();
    cleanups.clear();
    win.removeEventListener("pagehide", dispose);
  }
  win.addEventListener("pagehide", dispose);
  return { ready: Promise.all([initialize("product"), initialize("marketing")]), dispose };
}
