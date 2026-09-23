/* Real ES modules, fake DOM/transport, manually resolved requests. No network or sleeps. */
async function main() {
  const { default: assert } = await import("node:assert/strict");
  const { readFile } = await import("node:fs/promises");
  const { resolve } = await import("node:path");
  const { SourceTextModule, createContext } = await import("node:vm");
  const [root, scenario, sourceOverride] = process.argv.slice(2);
  const source = await readFile(sourceOverride || resolve(root, "ui/assets/emotes.js"), "utf8");
  const view = await readFile(resolve(root, "ui/assets/emote-view.js"), "utf8");
  const flush = () => new Promise((done) => setImmediate(done));

  function deferred() {
    let resolvePromise, reject;
    const promise = new Promise((yes, no) => { resolvePromise = yes; reject = no; });
    return { promise, resolve: resolvePromise, reject };
  }

  class Element {
    constructor(tag, attached = false) {
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
      for (const child of this.children) child.parent = null;
      this.children = [];
      this.text = "";
      this.append(...children);
    }
    contains(child) { return child === this || this.children.some((entry) => entry.contains(child)); }
    setAttribute(key, value) { this.attributes.set(key, String(value)); }
    addEventListener(event, handler) {
      if (!this.listeners.has(event)) this.listeners.set(event, []);
      this.listeners.get(event).push(handler);
    }
    dispatch(event) { for (const handler of this.listeners.get(event) || []) handler({ target: this }); }
    showModal() { this.open = true; }
    close() { this.open = false; this.dispatch("close"); }
    getBoundingClientRect() { return { top: 0, bottom: 100 }; }
    getContext() {
      return { clearRect() {}, drawImage: (image) => { this.drawnSheet = image.src; } };
    }
  }

  function pack(id, changes = {}) {
    return { id, name: id, category: "companion", preview_only: true, purchasable: false,
      preview_url: `/ui/assets/emotes/${id}/sheet.png`,
      preview_manifest_url: `/ui/assets/emotes/${id}/manifest.json`, ...changes };
  }

  async function boot(catalog = [pack("a"), pack("b")], delayDecode = false) {
    const ids = ["catalog-status", "motion-toggle", "preview-title", "preview-description",
      "preview-stage", "preview-command", "preview-notice", "replay-preview", "emote-dialog",
      "emote-grid", "emote-search", "emote-category", "result-count", "empty-state", "copy-command"];
    const elements = new Map(ids.map((id) => [id, new Element("div", true)]));
    const $ = (id) => { assert.ok(elements.has(id), `Unexpected DOM id ${id}`); return elements.get(id); };
    $("emote-category").value = "all";
    const media = new Element("media");
    media.matches = false;
    const window = new Element("window");
    window.innerHeight = 800;
    window.matchMedia = () => media;
    const document = new Element("document");
    document.hidden = false;
    document.getElementById = $;
    document.createElement = (tag) => new Element(tag);
    const requests = new Map();
    const decodes = new Map();
    const frames = new Map();
    let nextFrame = 0;
    const context = createContext({
      window, document, performance: { now: () => 0 },
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
        "export async function apiJson(path){if(path !== '/store/emotes/owned') throw Error('Forbidden API'); return {entitlements:[]};} export function getLoginHref(){throw Error('Unexpected login');}", { context })],
      ["./emote-view.js?v=3", new SourceTextModule(view, { context })],
    ]);
    const module = new SourceTextModule(source, { context, identifier: "emotes.js" });
    await module.link((name) => { assert.ok(modules.has(name), `Forbidden import ${name}`); return modules.get(name); });
    await module.evaluate();
    await flush();
    assert.equal($("emote-grid").children.length, catalog.length);
    const cards = () => $("emote-grid").children;
    const preview = (index) => cards()[index].children[2].children.at(-1).children[0].dispatch("click");
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
    return { $, cards, preview, respond, decoded, shown, requests, window, catalog };
  }

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
    if (scenario === "queued-close") h.$("emote-dialog").dispatch("close");
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
  } else throw new Error(`Unknown scenario ${scenario}`);
  console.log(`PASS ${scenario}`);
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
