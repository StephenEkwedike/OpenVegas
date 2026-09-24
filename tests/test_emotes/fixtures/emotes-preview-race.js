/* Real ES modules, fake DOM/transport, manually resolved requests. No network or sleeps. */
async function main() {
  const { default: assert } = await import("node:assert/strict");
  const { readFile } = await import("node:fs/promises");
  const { resolve } = await import("node:path");
  const { SourceTextModule, createContext } = await import("node:vm");
  const [root, scenario, sourceOverride] = process.argv.slice(2);
  const source = await readFile(sourceOverride || resolve(root, "ui/assets/emotes.js"), "utf8");
  const view = await readFile(resolve(root, "ui/assets/emote-view.js"), "utf8");
  const html = await readFile(resolve(root, "ui/emotes.html"), "utf8");
  const flush = () => new Promise((done) => setImmediate(done));

  function deferred() {
    let resolvePromise, reject;
    const promise = new Promise((yes, no) => { resolvePromise = yes; reject = no; });
    return { promise, resolve: resolvePromise, reject };
  }

  class Element {
    constructor(tag, attached = false, document = null) {
      this.tagName = tag;
      this.attached = attached;
      this.parent = null;
      this.children = [];
      this.listeners = new Map();
      this.attributes = new Map();
      this.classList = { toggle() {} };
      this.value = "";
      this.open = false;
      this.text = "";
      this.ownerDocument = document;
      this.disabled = false;
      this.hidden = false;
    }
    get isConnected() { return this.attached || Boolean(this.parent?.isConnected); }
    get textContent() { return this.text + this.children.map((child) => child.textContent).join(""); }
    set textContent(text) { this.replaceChildren(); this.text = String(text); }
    set innerHTML(_) { throw new Error("HTML insertion is forbidden"); }
    insertAdjacentHTML() { throw new Error("HTML insertion is forbidden"); }
    append(...children) {
      for (const child of children) { child.parent = this; this.children.push(child); }
    }
    replaceChildren(...children) {
      if (this.children.some((child) => child.contains(this.ownerDocument?.activeElement))) {
        this.ownerDocument.activeElement = this.ownerDocument.body;
      }
      for (const child of this.children) child.parent = null;
      this.children = [];
      this.text = "";
      this.append(...children);
    }
    contains(child) { return child === this || this.children.some((entry) => entry.contains(child)); }
    setAttribute(key, value) { this.attributes.set(key, String(value)); }
    getAttribute(key) { return this.attributes.get(key) ?? null; }
    addEventListener(event, handler) {
      if (!this.listeners.has(event)) this.listeners.set(event, []);
      this.listeners.get(event).push(handler);
    }
    dispatch(event) { for (const handler of this.listeners.get(event) || []) handler({ target: this }); }
    click() {
      if (!this.disabled && !this.hidden
        && (!this.ownerDocument?.modal?.open || this.ownerDocument.modal.contains(this))) this.dispatch("click");
    }
    focus() {
      const doc = this.ownerDocument;
      if (doc && this.isConnected && !this.disabled && !this.hidden
        && (!doc.modal?.open || doc.modal.contains(this))) doc.activeElement = this;
    }
    showModal() {
      this.previousFocus = this.ownerDocument.activeElement;
      this.open = true;
      this.ownerDocument.modal = this;
      this.ownerDocument.closeButton.focus();
    }
    close() {
      this.open = false;
      if (this.previousFocus?.isConnected) this.previousFocus.focus();
      else this.ownerDocument.activeElement = this.ownerDocument.body;
      this.dispatch("close");
    }
    getBoundingClientRect() { return { top: 0, bottom: 100 }; }
    getContext() {
      return { clearRect() {}, drawImage: (image, x) => { this.drawnSheet = image.src; this.drawnX = x; } };
    }
  }

  function pack(id, changes = {}) {
    return { id, name: id, category: "companion", preview_only: true, purchasable: false,
      preview_url: `/ui/assets/emotes/${id}/sheet.png`,
      preview_manifest_url: `/ui/assets/emotes/${id}/manifest.json`, ...changes };
  }

  async function boot(catalog = [pack("a"), pack("b")], delayDecode = false, options = {}) {
    const ids = ["catalog-status", "motion-toggle", "preview-title", "preview-description",
      "preview-stage", "preview-command", "preview-notice", "replay-preview", "emote-dialog",
      "emote-grid", "emote-search", "emote-category", "result-count", "empty-state", "copy-command",
      "ownership-retry", "preview-motion-toggle", "dialog-close"];
    const elements = new Map(ids.map((id) => [id, new Element("div", true)]));
    const $ = (id) => { assert.ok(elements.has(id), `Unexpected DOM id ${id}`); return elements.get(id); };
    $("emote-category").value = "all";
    const media = new Element("media");
    media.matches = Boolean(options.reducedMotion);
    const window = new Element("window");
    window.innerHeight = 800;
    window.matchMedia = () => media;
    let confirmations = 0;
    window.confirm = () => { confirmations++; return options.confirm !== false; };
    const document = new Element("document");
    document.hidden = false;
    document.getElementById = $;
    document.createElement = (tag) => new Element(tag, false, document);
    document.body = new Element("body", true, document);
    document.activeElement = document.body;
    for (const element of elements.values()) element.ownerDocument = document;
    document.closeButton = $("dialog-close");
    for (const id of ["dialog-close", "preview-stage", "preview-motion-toggle", "replay-preview"]) {
      $("emote-dialog").append($(id));
    }
    const requests = new Map();
    const decodes = new Map();
    const frames = new Map();
    let nextFrame = 0;
    let now = 0;
    let nextKey = 0;
    const apiHandler = options.api || (async (path) => {
      assert.equal(path, "/store/emotes/owned", `Forbidden API ${path}`);
      return { entitlements: [] };
    });
    const context = createContext({
      window, document, apiHandler, performance: { now: () => now },
      crypto: { randomUUID: () => `fixture-key-${++nextKey}` },
      requestAnimationFrame: (callback) => { frames.set(++nextFrame, callback); return nextFrame; },
      cancelAnimationFrame: (id) => frames.delete(id),
      AbortSignal: { timeout: () => ({}) },
      // Timeout callbacks are never advanced; request resolution is entirely test-owned.
      setTimeout: () => 1,
      fetch: (url) => {
        if (url === "/store/emotes/catalog") {
          return Promise.resolve({ ok: true, json: async () => ({ items: catalog }) });
        }
        assert.ok(catalog.some((item) => item.preview_manifest_url === url), `Forbidden fetch ${url}`);
        assert.ok(!requests.has(url), `Unexpected duplicate fetch ${url}`);
        const pending = deferred();
        requests.set(url, pending);
        return pending.promise;
      },
      Image: class {
        constructor() { this.naturalWidth = 4; this.naturalHeight = 2; }
        decode() {
          if (!delayDecode) return Promise.resolve();
          const pending = deferred(); decodes.set(this.src, pending); return pending.promise;
        }
      },
    });
    const modules = new Map([
      ["/ui/assets/site.js?v=20260330", new SourceTextModule(
        "export function renderTopNav(){}; export function renderFounderLinks(){}; export function installAssetGuard(){}", { context })],
      ["/ui/assets/page-auth.js?v=20260330", new SourceTextModule(
        "export async function apiJson(path, options){return apiHandler(path, options);} export function getLoginHref(){return '/fixture-login';}", { context })],
      ["./emote-view.js?v=3", new SourceTextModule(view, { context })],
    ]);
    const module = new SourceTextModule(source, { context, identifier: "emotes.js" });
    await module.link((name) => { assert.ok(modules.has(name), `Forbidden import ${name}`); return modules.get(name); });
    await module.evaluate();
    await flush();
    assert.equal($("emote-grid").children.length, catalog.length);
    const cards = () => $("emote-grid").children;
    const control = (index, action = 0) => cards()[index].children[2].children.at(-1).children[action];
    const preview = (index) => { control(index).focus(); control(index).click(); };
    const step = (time) => {
      now = time;
      for (const [id, callback] of [...frames]) { frames.delete(id); callback(now); }
    };
    const manifest = { schema_version: 1, frame: { width: 2, height: 2 }, animations: {
      waiting: { frames: [0, 1], frame_ms: 100, loop: true },
      complete: { frames: [0, 1], frame_ms: 100, loop: false },
    } };
    async function respond(index) {
      requests.get(catalog[index].preview_manifest_url).resolve({ ok: true, text: async () => JSON.stringify(manifest) });
      await flush();
    }
    async function decoded(index) { decodes.get(catalog[index].preview_url).resolve(); await flush(); }
    function shown(index) {
      const stage = $("preview-stage");
      assert.equal(stage.children.length, 1);
      assert.equal(stage.children[0].tagName, "canvas");
      assert.equal(stage.children[0].drawnSheet, catalog[index].preview_url);
      assert.equal($("preview-title").textContent, catalog[index].name);
      assert.equal($("preview-command").textContent, `openvegas emote preview ${catalog[index].id}`);
    }
    return { $, cards, control, preview, respond, decoded, shown, requests, window, catalog,
      document, media, frames, step, confirmations: () => confirmations };
  }

  function server() {
    const calls = [];
    return {
      calls,
      api(path, options) {
        assert.ok(["/store/emotes/owned", "/store/emotes/equip", "/store/buy"].includes(path), `Forbidden API ${path}`);
        const pending = deferred();
        calls.push({ path, body: options?.body && JSON.parse(options.body), ...pending });
        return pending.promise;
      },
      async reply(index, result) { calls[index].resolve(result); await flush(); },
      async fail(index, status) { calls[index].reject(Object.assign(new Error("Fixture failure"), { status })); await flush(); },
    };
  }
  const sale = (id = "a") => pack(id, { purchasable: true, preview_only: false, cost_v: "2.5" });
  const library = (ids, equipped) => ({ entitlements: ids.map((item_id) => ({
    item_id, effective_status: "active", activatable: true, equipped: item_id === equipped,
  })) });

  if (scenario === "response-race" || scenario === "decode-race") {
    const delayedDecode = scenario === "decode-race";
    const h = await boot(undefined, delayedDecode);
    h.preview(0);
    if (delayedDecode) await h.respond(0);
    h.$("emote-dialog").close();
    h.preview(1);
    await h.respond(1);
    if (delayedDecode) await h.decoded(1);
    h.shown(1);
    if (delayedDecode) await h.decoded(0); else await h.respond(0);
    h.shown(1);
  } else if (scenario === "closed-modal" || scenario === "pagehide") {
    const h = await boot();
    h.preview(0);
    if (scenario === "closed-modal") h.$("emote-dialog").close(); else h.window.dispatch("pagehide");
    await h.respond(0);
    assert.equal(h.$("preview-stage").children.length, 0);
  } else if (scenario === "stale-error") {
    const h = await boot();
    h.preview(0);
    const staleMessage = h.$("preview-stage").children[0];
    h.$("emote-dialog").close();
    h.preview(1);
    await h.respond(1);
    h.requests.get(h.catalog[0].preview_manifest_url).reject(new Error("stale failure"));
    await flush();
    h.shown(1);
    assert.equal(staleMessage.textContent, "Loading animation preview...");
  } else if (scenario === "cached-reopen" || scenario === "queued-close") {
    const h = await boot();
    h.preview(0);
    if (scenario === "queued-close") h.$("emote-dialog").open = false;
    else h.$("emote-dialog").close();
    h.preview(0);
    if (scenario === "queued-close") {
      h.$("emote-dialog").dispatch("close");
      assert.equal(h.document.activeElement, h.$("dialog-close"), "An old close event must not steal the reopened dialog's focus");
    }
    await h.respond(0);
    h.shown(0);
    assert.equal(h.requests.size, 2, "Card and modal must share cached requests");
  } else if (scenario === "independent-cards") {
    const h = await boot();
    h.preview(0);
    await h.respond(1);
    await h.respond(0);
    for (let index = 0; index < 2; index++) {
      const canvas = h.cards()[index].children[1].children[0];
      assert.equal(canvas.drawnSheet, h.catalog[index].preview_url);
    }
    h.shown(0);
  } else if (scenario === "compatibility-empty") {
    for (const compatibility of [undefined, null, [], "all terminals", {}, [null, 42, "", "  "]]) {
      const h = await boot([pack("a", { compatibility, category: "completion" })]);
      const expected = "Compatibility: not certified. No terminal or host compatibility is listed for this pack.";
      assert.ok(h.cards()[0].textContent.includes(expected));
      h.preview(0);
      assert.ok(h.$("preview-notice").textContent.endsWith(expected));
      assert.ok(h.cards()[0].textContent.includes("Not yet for sale"));
    }
  } else if (scenario === "compatibility-text") {
    const literal = '<img src=x onerror="throw 1">';
    const h = await boot([pack("a", { compatibility: ["Fixture Host 1.2 (unverified)", literal] })]);
    const expected = `Catalog compatibility: Fixture Host 1.2 (unverified); ${literal}. Native terminal certification is not provided by this listing.`;
    const label = h.cards()[0].children[2].children[2];
    assert.equal(label.textContent, expected);
    assert.equal(label.children.length, 0, "Compatibility must remain text, never markup");
    assert.equal(label.className, "text-mono-xs");
    h.preview(0);
    assert.ok(h.$("preview-notice").textContent.endsWith(expected));
    assert.equal(h.$("preview-notice").children.length, 0);
    assert.ok(h.cards()[0].textContent.includes("Not yet for sale"));
  } else if (scenario === "compatibility-bounds") {
    const values = [" Fixture 1 ", "Fixture 1", null, {}, "bad\nlabel", "x".repeat(129),
      ...Array.from({ length: 30 }, (_, i) => `Fixture ${i + 2}`)];
    const h = await boot([pack("a", { compatibility: values })]);
    const label = h.cards()[0].children[2].children[2].textContent;
    assert.equal(label, `Catalog compatibility: ${["Fixture 1", ...Array.from({ length: 14 }, (_, i) => `Fixture ${i + 2}`)].join("; ")}. Native terminal certification is not provided by this listing.`);
    h.preview(0);
    assert.ok(h.$("preview-notice").textContent.endsWith(label));
  } else if (scenario === "ownership-loading") {
    const s = server();
    const h = await boot([sale()], false, { api: s.api });
    assert.equal(h.control(0, 1).disabled, true);
    h.control(0, 1).dispatch("click");
    assert.equal(s.calls.length, 1);
    assert.equal(h.confirmations(), 0);
    await s.reply(0, library(["a"]));
    assert.equal(h.control(0, 1).textContent, "Equip");
    assert.equal(h.control(0, 1).disabled, false);
  } else if (["ownership-stale-success", "ownership-stale-error"].includes(scenario)) {
    for (const status of scenario === "ownership-stale-error" ? [503, 401] : [null]) {
      const s = server();
      const h = await boot([sale("a"), sale("b")], false, { api: s.api });
      await s.reply(0, library(["a", "b"]));
      h.control(0, 1).click();
      h.control(1, 1).click();
      assert.equal(s.calls[1].body.item_id, "a");
      assert.equal(s.calls[2].body.item_id, "b");
      await s.reply(1, {});
      await s.reply(2, {});
      assert.equal(s.calls[3].path, "/store/emotes/owned");
      assert.equal(s.calls[4].path, "/store/emotes/owned");
      await s.reply(4, library(["a", "b"], "b"));
      const currentMessage = h.$("catalog-status").textContent;
      if (status) await s.fail(3, status);
      else await s.reply(3, library(["a", "b"], "a"));
      assert.equal(h.control(0, 1).textContent, "Equip");
      assert.equal(h.control(1, 1).textContent, "Equipped");
      assert.equal(h.control(1, 1).disabled, true);
      assert.equal(h.$("catalog-status").textContent, currentMessage);
      assert.equal(h.$("ownership-retry").hidden, true);
    }
  } else if (["ownership-transient", "ownership-auth"].includes(scenario)) {
    const s = server();
    const h = await boot([sale()], false, { api: s.api });
    await s.reply(0, library(["a"]));
    h.control(0, 1).click();
    await s.reply(1, {});
    await s.fail(2, scenario === "ownership-auth" ? 401 : 503);
    assert.equal(h.control(0, 1).disabled, true);
    assert.equal(h.$("ownership-retry").hidden, false);
    assert.equal(h.$("ownership-retry").disabled, false);
    assert.equal(h.control(0, 1).textContent, scenario === "ownership-auth" ? "Buy for 2.5 $V" : "Equip");
    if (scenario === "ownership-auth") {
      assert.equal(h.$("catalog-status").children[0].href, "/fixture-login");
    }
    h.$("emote-category").value = "owned";
    h.$("emote-category").dispatch("change");
    assert.equal(h.cards().length, scenario === "ownership-auth" ? 0 : 1);
    h.$("ownership-retry").click();
    h.$("ownership-retry").dispatch("click");
    assert.equal(s.calls.length, 4, "Retry cannot issue overlapping reads");
    await s.reply(3, library(["a"], "a"));
    assert.equal(h.control(0, 1).textContent, "Equipped");
    assert.equal(h.$("ownership-retry").hidden, true);
  } else if (scenario === "ownership-latest-error") {
    for (const status of [503, 401]) {
      const s = server();
      const h = await boot([sale("a"), sale("b")], false, { api: s.api });
      await s.reply(0, library(["a", "b"]));
      h.control(0, 1).click();
      h.control(1, 1).click();
      await s.reply(1, {});
      await s.reply(2, {});
      await s.fail(4, status);
      const message = h.$("catalog-status").textContent;
      await s.reply(3, library(["a", "b"], "a"));
      assert.equal(h.$("catalog-status").textContent, message);
      assert.equal(h.control(0, 1).disabled, true);
      assert.equal(h.control(1, 1).disabled, true);
      assert.equal(h.$("ownership-retry").hidden, false);
      h.$("emote-category").value = "owned";
      h.$("emote-category").dispatch("change");
      assert.equal(h.cards().length, status === 401 ? 0 : 2);
    }
  } else if (scenario === "ownership-invalid-response") {
    const s = server();
    const h = await boot([sale()], false, { api: s.api });
    await s.reply(0, {});
    assert.equal(h.control(0, 1).disabled, true);
    assert.equal(h.$("ownership-retry").hidden, false);
    h.$("ownership-retry").click();
    await s.reply(1, library([]));
    assert.equal(h.control(0, 1).disabled, false);
  } else if (scenario === "retry-focus") {
    const s = server();
    const h = await boot([sale()], false, { api: s.api });
    await s.fail(0, 503);
    h.$("ownership-retry").focus();
    h.$("ownership-retry").click();
    assert.equal(h.document.activeElement, h.$("ownership-retry"));
    assert.equal(h.$("ownership-retry").getAttribute("aria-disabled"), "true");
    h.$("ownership-retry").click();
    assert.equal(s.calls.length, 2);
    await s.reply(1, library([]));
    assert.equal(h.document.activeElement, h.$("emote-search"));
  } else if (scenario === "pending-rerender" || scenario === "purchase-authority") {
    const s = server();
    const h = await boot([sale()], false, { api: s.api });
    await s.reply(0, library([]));
    h.control(0, 1).click();
    const firstKey = s.calls[1].body.idempotency_key;
    h.$("emote-search").value = "a";
    h.$("emote-search").dispatch("input");
    assert.equal(h.control(0, 1).disabled, true);
    h.control(0, 1).dispatch("click");
    assert.equal(s.calls.length, 2);
    await s.reply(1, { owned: true, equipped: true });
    h.$("emote-category").dispatch("change");
    assert.equal(h.control(0, 1).disabled, true);
    assert.equal(h.control(0, 1).textContent, "Buy for 2.5 $V", "POST must not infer ownership");
    h.control(0, 1).dispatch("click");
    assert.equal(s.calls.length, 3);
    if (scenario === "pending-rerender") {
      await s.reply(2, library(["a"]));
      assert.equal(h.control(0, 1).textContent, "Equip");
      assert.equal(h.control(0, 1).disabled, false);
    } else {
      await s.fail(2, 503);
      assert.equal(h.control(0, 1).disabled, true);
      h.$("ownership-retry").click();
      await s.reply(3, library([]));
      h.control(0, 1).click();
      assert.equal(s.calls[4].body.idempotency_key, firstKey);
      await s.reply(4, {});
      await s.reply(5, library(["a"]));
      assert.equal(h.control(0, 1).textContent, "Equip");
    }
  } else if (scenario === "equip-authority") {
    const s = server();
    const h = await boot([sale()], false, { api: s.api });
    await s.reply(0, library(["a"]));
    h.control(0, 1).click();
    await s.reply(1, { equipped: true });
    await s.reply(2, library(["a"]));
    assert.equal(h.control(0, 1).textContent, "Equip", "Only a fresh GET can confirm equipped state");
    assert.equal(h.control(0, 1).disabled, false);
    assert.ok(!h.$("catalog-status").textContent.includes("equipped on your account"));
  } else if (scenario === "action-failure") {
    for (const status of [503, 400, 401]) {
      const s = server();
      const h = await boot([sale()], false, { api: s.api });
      await s.reply(0, library([]));
      h.control(0, 1).click();
      await s.fail(1, status);
      assert.equal(h.control(0, 1).disabled, true);
      assert.equal(h.$("ownership-retry").hidden, false);
      h.$("ownership-retry").click();
      await s.reply(2, library([]));
      h.control(0, 1).click();
      assert.equal(s.calls.length, 4);
      if (status !== 401) assert.equal(s.calls[1].body.idempotency_key, s.calls[3].body.idempotency_key);
      else assert.notEqual(s.calls[1].body.idempotency_key, s.calls[3].body.idempotency_key);
    }
  } else if (scenario === "purchase-cancel") {
    const s = server();
    const h = await boot([sale()], false, { api: s.api, confirm: false });
    await s.reply(0, library([]));
    h.control(0, 1).click();
    assert.equal(s.calls.length, 1);
    assert.equal(h.control(0, 1).disabled, false);
  } else if (scenario === "action-invalidates-refresh") {
    for (const status of [503, 401]) {
      const s = server();
      const h = await boot([sale("a"), sale("b")], false, { api: s.api });
      await s.reply(0, library(["a", "b"]));
      h.control(0, 1).click();
      h.control(1, 1).click();
      await s.reply(1, {});
      await s.fail(2, status);
      const message = h.$("catalog-status").textContent;
      await s.reply(3, library(["a", "b"], "a"));
      assert.equal(h.$("catalog-status").textContent, message);
      assert.equal(h.control(0, 1).disabled, true);
      assert.equal(h.$("ownership-retry").hidden, false);
      h.$("emote-category").value = "owned";
      h.$("emote-category").dispatch("change");
      assert.equal(h.cards().length, status === 401 ? 0 : 2);
    }
  } else if (scenario === "focus-refresh" || scenario === "dialog-focus-refresh") {
    const s = server();
    const h = await boot([sale()], false, { api: s.api });
    const opener = h.control(0);
    opener.focus();
    if (scenario === "dialog-focus-refresh") h.preview(0);
    await s.reply(0, library([]));
    assert.equal(opener.isConnected, false, "Fixture must exercise the replacement path");
    if (scenario === "dialog-focus-refresh") {
      assert.equal(h.document.activeElement, h.$("dialog-close"), "Refresh must not steal modal focus");
      h.$("emote-dialog").close();
    }
    assert.equal(h.document.activeElement, h.control(0));
    assert.equal(h.document.activeElement.isConnected, true);
  } else if (scenario === "focus-filter" || scenario === "focus-action") {
    const s = server();
    const h = await boot([sale()], false, { api: s.api });
    if (scenario === "focus-filter") {
      h.$("emote-search").focus();
      await s.reply(0, library([]));
      assert.equal(h.document.activeElement, h.$("emote-search"));
    } else {
      await s.reply(0, library([]));
      h.control(0, 1).focus();
      h.control(0, 1).click();
      assert.equal(h.document.activeElement, h.control(0), "Pending action moves focus to its enabled Preview control");
      await s.reply(1, {});
      await s.reply(2, library(["a"], "a"));
      assert.equal(h.document.activeElement, h.control(0));
    }
  } else if (scenario === "dialog-focus-fallback") {
    const s = server();
    const h = await boot([sale()], false, { api: s.api });
    await s.reply(0, library(["a"]));
    h.$("emote-category").value = "owned";
    h.$("emote-category").dispatch("change");
    h.control(0, 1).click();
    h.preview(0);
    await s.reply(1, {});
    await s.fail(2, 401);
    assert.equal(h.cards().length, 0);
    assert.equal(h.document.activeElement, h.$("dialog-close"));
    h.$("emote-dialog").close();
    assert.equal(h.document.activeElement, h.$("emote-search"));
  } else if (scenario === "asset-references") {
    assert.match(html, /href="\/ui\/assets\/emotes\.css\?v=4"/);
    assert.match(html, /src="\/ui\/assets\/emotes\.js\?v=4"/);
    assert.match(source, /from "\.\/emote-view\.js\?v=3"/);
    assert.match(source, /from "\/ui\/assets\/site\.js\?v=20260330"/);
    assert.match(source, /from "\/ui\/assets\/page-auth\.js\?v=20260330"/);
  } else if (scenario === "modal-motion") {
    const dialog = html.match(/<dialog\b[^>]*>([\s\S]*?)<\/dialog>/)[1];
    assert.match(dialog, /id="preview-motion-toggle"[^>]*type="button"[^>]*aria-pressed="false"/);
    const h = await boot([pack("a")]);
    await h.respond(0);
    h.preview(0);
    await flush();
    const canvas = h.$("preview-stage").children[0];
    h.step(100);
    assert.equal(canvas.drawnX, 2);
    h.$("preview-motion-toggle").focus();
    assert.equal(h.document.activeElement, h.$("preview-motion-toggle"));
    h.$("preview-motion-toggle").click();
    for (const id of ["motion-toggle", "preview-motion-toggle"]) {
      assert.equal(h.$(id).textContent, "Play previews");
      assert.equal(h.$(id).getAttribute("aria-pressed"), "true");
    }
    assert.equal(h.frames.size, 0);
    h.step(500);
    assert.equal(canvas.drawnX, 2);
    h.$("preview-motion-toggle").click();
    h.step(600);
    assert.equal(canvas.drawnX, 0);
    assert.equal(h.$("motion-toggle").getAttribute("aria-pressed"), "false");
    h.$("emote-dialog").close();
    h.$("motion-toggle").click();
    h.preview(0);
    await flush();
    assert.equal(h.$("preview-motion-toggle").textContent, "Play previews");
  } else if (scenario === "modal-reduced-motion") {
    const h = await boot([pack("a", { category: "completion" })], false, { reducedMotion: true });
    await h.respond(0);
    h.preview(0);
    await flush();
    for (const id of ["motion-toggle", "preview-motion-toggle", "replay-preview"]) {
      assert.equal(h.$(id).disabled, true);
      h.$(id).dispatch("click");
    }
    assert.equal(h.frames.size, 0);
    assert.equal(h.$("preview-stage").children[0].drawnX, 0);
    h.media.matches = false;
    h.media.dispatch("change");
    assert.equal(h.$("preview-motion-toggle").disabled, false);
    assert.equal(h.$("replay-preview").disabled, false);
    h.step(100);
    assert.equal(h.$("preview-stage").children[0].drawnX, 2);
    h.$("preview-motion-toggle").click();
    h.$("replay-preview").click();
    assert.equal(h.$("preview-motion-toggle").textContent, "Pause previews");
    assert.equal(h.$("motion-toggle").getAttribute("aria-pressed"), "false");
    assert.equal(h.$("preview-stage").children[0].drawnX, 0);
    h.media.matches = true;
    h.media.dispatch("change");
    assert.equal(h.frames.size, 0);
    assert.equal(h.$("preview-motion-toggle").disabled, true);
    assert.equal(h.$("preview-motion-toggle").getAttribute("aria-pressed"), "true");
  } else throw new Error(`Unknown scenario ${scenario}`);
  console.log(`PASS ${scenario}`);
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
