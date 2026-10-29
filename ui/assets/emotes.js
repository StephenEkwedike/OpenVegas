import {money} from './currency.js';
import { renderTopNav, renderFounderLinks, installAssetGuard } from "/ui/assets/site.js?v=20260330";
import { apiJson, getLoginHref } from "/ui/assets/page-auth.js?v=20260330";
import { catalogItems, collectionStatus, equipmentRequest, previewSpecs, previewSample, defaultPreviewClip, characterGroups, animationLabel, selectedVariant, isPurchasable, matchingItems, sortedItems, previewCommand, previewPath, plannedPriceLabel, scenePath, isStillPreview } from "./emote-view.js?v=7";

renderTopNav();
renderFounderLinks();
installAssetGuard();

const $ = (id) => document.getElementById(id);
const media = window.matchMedia("(prefers-reduced-motion: reduce)");
const players = new Set();
const images = new Map();
const previewRequests = new WeakMap();
const owned = new Map();
const purchaseKeys = new Map();
const pendingItems = new Set();
const cardControls = new Map();
const variantSelections = new Map();
let ownershipState = "loading";
let ownershipRequest = 0;
let dialogOpener = null;
let dialogItem = null;
let dialogClip = null;
let dialogClips = [];
let copyRequest = 0;
const clipLabels = { idle: "Idle", waiting: "Working", complete: "Completion" };
let items = [];
let paused = media.matches;
let animationId = null;

function node(tag, text, className) {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = text;
  if (className) el.className = className;
  return el;
}

function tell(message) { $("catalog-status").textContent = message; }

let reviewScenesLoaded = false;
function setScene(stage, value) {
  const scene = scenePath(value);
  // Only fixed stylesheet URLs can load; catalog text never becomes CSS.
  if (scene && !reviewScenesLoaded) {
    const stylesheet = document.createElement("link");
    stylesheet.rel = "stylesheet";
    stylesheet.href = "/ui/assets/emotes/previews/scenes/scenes.css";
    document.head.append(stylesheet);
    reviewScenesLoaded = true;
  }
  stage.setAttribute("data-scene", scene || "");
  stage.classList.toggle("scene-preview", Boolean(scene));
}

function compatibilityLabel(item) {
  const labels = Array.isArray(item?.compatibility)
    ? [...new Set(item.compatibility.slice(0, 20)
      .filter((value) => typeof value === "string" && value.trim().length > 0
        && value.length <= 128 && !/[\u0000-\u001f\u007f]/.test(value))
      .map((value) => value.trim()))]
    : [];
  return labels.length
    ? `Catalog compatibility: ${labels.join("; ")}. Native terminal certification is not provided by this listing.`
    : "Compatibility: not certified. No terminal or host compatibility is listed for this pack.";
}

function draw(player, now) {
  const { canvas, image, spec } = player;
  if (!paused && !document.hidden && player.lastTick !== null) player.elapsedMs += Math.max(0, now - player.lastTick);
  player.lastTick = now;
  const { frame, next, mix } = previewSample(spec, player.elapsedMs, media.matches, player.blendMs);
  const sample = `${frame}:${next}:${mix}`;
  if (sample === player.last) return;
  player.last = sample;
  const context = canvas.getContext("2d");
  context.clearRect(0, 0, spec.width, spec.height);
  const columns = image.naturalWidth / spec.width;
  const paint = (index) => context.drawImage(image, (index % columns) * spec.width, Math.floor(index / columns) * spec.height,
    spec.width, spec.height, 0, 0, spec.width, spec.height);
  context.globalAlpha = 1 - mix;
  paint(frame);
  if (mix) {
    // Sum premultiplied layers, avoiding a transparency dip on shared silhouettes.
    context.globalCompositeOperation = "lighter";
    context.globalAlpha = mix;
    paint(next);
  }
  context.globalAlpha = 1;
  context.globalCompositeOperation = "source-over";
}

function tick(now) {
  animationId = null;
  for (const player of players) {
    if (!player.canvas.isConnected) { players.delete(player); continue; }
    const box = player.canvas.getBoundingClientRect();
    if (box.bottom > 0 && box.top < window.innerHeight) draw(player, now);
    else player.lastTick = null;
  }
  if (!paused && !document.hidden && players.size) animationId = requestAnimationFrame(tick);
}

function updateMotion() {
  for (const player of players) player.lastTick = null;
  if (animationId !== null) cancelAnimationFrame(animationId);
  animationId = null;
  for (const id of ["motion-toggle", "preview-motion-toggle"]) {
    $(id).textContent = paused ? "Play previews" : "Pause previews";
    $(id).setAttribute("aria-pressed", String(paused));
    $(id).disabled = media.matches || (id === "preview-motion-toggle" && isStillPreview(dialogItem));
  }
  tick(performance.now());
}

async function previewData(item) {
  const sheet = previewPath(item.preview_url);
  const manifestUrl = previewPath(item.preview_manifest_url);
  if (!sheet || !manifestUrl) throw new Error("Artwork preview coming soon.");
  const kind = isStillPreview(item) ? "still" : "animated";
  const cacheKey = `${manifestUrl}|${sheet}|${kind}`;
  if (!images.has(cacheKey)) images.set(cacheKey, (async () => {
    const response = await fetch(manifestUrl, { signal: AbortSignal.timeout(10000) });
    if (!response.ok) throw new Error("Preview could not load. Retry in a moment.");
    const text = await response.text();
    if (text.length > 65536) throw new Error("Preview exceeds supported size.");
    const specs = previewSpecs(JSON.parse(text), kind);
    const spec = Object.values(specs)[0];
    if (!spec) throw new Error("No supported preview states are available.");
    const image = new Image();
    image.src = sheet;
    await Promise.race([image.decode(), new Promise((_, reject) => setTimeout(() => reject(new Error("Preview timed out.")), 10000))]);
    const columns = image.naturalWidth / spec.width;
    const rows = image.naturalHeight / spec.height;
    if (!Number.isInteger(columns) || !Number.isInteger(rows) || columns < 1 || rows < 1 || columns * rows > 256
      || image.naturalWidth * image.naturalHeight > 4194304) {
      throw new Error("Invalid preview sheet dimensions.");
    }
    const valid = Object.fromEntries(Object.entries(specs).filter(([, value]) =>
      Math.max(...value.frames, value.reducedMotionFrame) < columns * rows));
    if (!Object.keys(valid).length) throw new Error("No supported preview states are available.");
    return { image, specs: valid };
  })().catch((error) => { images.delete(cacheKey); throw error; }));
  return images.get(cacheKey);
}

function clearPreview(stage) {
  previewRequests.delete(stage);
  setScene(stage, null);
  for (const player of players) if (stage.contains(player.canvas)) players.delete(player);
  stage.replaceChildren();
}

function updatePreviewStates(clips, selected) {
  const still = isStillPreview(dialogItem);
  if (still) { clips = []; selected = null; }
  dialogClips = clips;
  dialogClip = selected;
  for (const clip of Object.keys(clipLabels)) {
    const button = $(`preview-${clip}`);
    button.hidden = !clips.includes(clip);
    button.setAttribute("aria-pressed", String(clip === selected));
  }
  $("replay-preview").hidden = selected !== "complete";
  $("replay-preview").disabled = media.matches || still;
  $("preview-motion-toggle").hidden = still;
  $("preview-motion-toggle").disabled = media.matches || still;
  $("preview-kind-notice").hidden = !still;
  $("preview-kind-notice").textContent = still ? "Still preview only. No working or completion animation is available." : "";
}

async function mountPreview(stage, item, clip = null, detail = false) {
  clearPreview(stage);
  setScene(stage, item.scene_url);
  const still = isStillPreview(item);
  const request = {};
  previewRequests.set(stage, request);
  // The modal reuses its connected stage when another pack is selected.
  const current = () => stage.isConnected && previewRequests.get(stage) === request;
  const message = node("span", still ? "Loading still preview..." : "Loading animation preview...", "text-mono-xs");
  stage.append(message);
  try {
    const { image, specs } = await previewData(item);
    if (!current()) return;
    const selected = clip || defaultPreviewClip(item, specs, detail);
    const spec = specs[selected];
    if (!spec) throw new Error("This preview state is unavailable.");
    if (detail) updatePreviewStates(Object.keys(specs), selected);
    const canvas = node("canvas");
    canvas.width = spec.width;
    canvas.height = spec.height;
    canvas.classList.toggle("completion-preview", selected === "complete");
    canvas.classList.toggle("portrait-preview", spec.width <= spec.height * 1.25);
    canvas.setAttribute("role", "img");
    canvas.setAttribute("aria-label", still ? `${item.name}: Still preview.` :
      `${item.name}: ${clipLabels[selected]} preview. Use Pause previews to stop motion.`);
    stage.replaceChildren(canvas);
    const player = { canvas, image, spec, blendMs: item.preview_blend_ms, last: null, lastTick: null, elapsedMs: 0 };
    if (!still) players.add(player);
    draw(player, performance.now());
    if (!still && animationId === null) updateMotion();
  } catch (error) {
    if (current()) {
      message.textContent = error.message || "Preview unavailable.";
      if (detail) updatePreviewStates([], null);
    }
  }
}

function openPreview(item, control = "preview") {
  dialogOpener = `${item.id}:${control}`;
  dialogItem = item;
  ++copyRequest;
  updatePreviewStates([], null);
  $("preview-title").textContent = item.name;
  $("preview-description").textContent = item.description || "";
  clearPreview($("preview-stage"));
  const command = previewCommand(item);
  $("preview-command").textContent = command || "Preview command unavailable for this pack.";
  $("copy-preview-command").disabled = !command;
  $("preview-copy-status").textContent = "";
  $("preview-sale").textContent = plannedPriceLabel(item) || (isPurchasable(item)
    ? `Available for ${money(item.cost_v)}. Purchase requires confirmation from the collection.` : "Not yet for sale.");
  $("preview-notice").textContent = `Visual prototype. Previewing does not purchase or equip this pack. ${item.preview_blend_ms > 0 ? "Browser preview blends existing poses; it does not add animation frames to the pack. " : ""}${compatibilityLabel(item)}`;
  $("emote-dialog").showModal();
  void mountPreview($("preview-stage"), item, null, true);
}

function ownershipFailure(error, message) {
  ownershipState = error.status === 401 ? "signed-out" : "stale";
  if (ownershipState === "signed-out") {
    owned.clear();
    purchaseKeys.clear();
    tell("Sign in to check ownership, purchase or equip an emote.");
    const link = node("a", " Sign in");
    link.href = getLoginHref("/ui/emotes");
    $("catalog-status").append(link);
  } else tell(message || "Previews are available. Ownership could not be checked. Actions are disabled until you retry the ownership check.");
}

async function refreshOwned(message) {
  const request = ++ownershipRequest;
  ownershipState = "loading";
  render();
  try {
    const result = await apiJson("/store/emotes/owned");
    if (request !== ownershipRequest) return;
    if (!Array.isArray(result?.entitlements)) throw new Error("Invalid ownership response.");
    owned.clear();
    for (const entry of result.entitlements) {
      if (entry?.effective_status === "active") owned.set(entry.item_id, entry);
    }
    ownershipState = "ready";
    // Only the ownership endpoint can confirm a purchase, not its POST response.
    for (const id of owned.keys()) purchaseKeys.delete(id);
    tell(message || collectionStatus(items));
  } catch (error) {
    if (request !== ownershipRequest) return;
    ownershipFailure(error);
  } finally {
    if (request === ownershipRequest) render();
  }
}

async function transact(item) {
  const entitlement = owned.get(item.id);
  if (ownershipState !== "ready" || pendingItems.has(item.id)
    || entitlement?.activatable === false || entitlement?.equipped) return;
  if (!entitlement && (!isPurchasable(item)
    || !window.confirm(`Purchase ${item.name} for ${money(item.cost_v)} from your OpenVegas balance?`))) return;
  pendingItems.add(item.id);
  render();
  try {
    if (entitlement) {
      await apiJson("/store/emotes/equip", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(equipmentRequest(item)) });
    } else {
      const key = purchaseKeys.get(item.id) || crypto.randomUUID();
      purchaseKeys.set(item.id, key);
      await apiJson("/store/buy", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ item_id: item.id, idempotency_key: key }) });
    }
    await refreshOwned(`${item.name}: request completed. Current ownership and equipment are shown below. Run openvegas emote sync in the updated CLI to restore your account selection on this device.`);
  } catch (error) {
    // An uncertain write or auth failure invalidates reads already in flight.
    ++ownershipRequest;
    ownershipFailure(error, error.status === 400
      ? "Action could not complete. Check your balance; no automatic top-up will be made. Retry the ownership check before another action."
      : "Could not confirm this action. Retry the ownership check first; an unconfirmed purchase keeps the same purchase reference.");
    if (error.status === 400) {
      const link = node("a", " View balance"); link.href = "/ui/balance"; $("catalog-status").append(link);
    }
  } finally { pendingItems.delete(item.id); render(); }
}

function restoreCardFocus(key) {
  const target = cardControls.get(key);
  const preview = cardControls.get(`${key.split(":")[0]}:preview`);
  (target && !target.disabled ? target : preview || $("emote-search")).focus({ preventScroll: true });
}

function render() {
  const grid = $("emote-grid");
  const focused = grid.contains(document.activeElement) ? document.activeElement.getAttribute("data-emote-control") : null;
  const retryFocused = document.activeElement === $("ownership-retry");
  for (const player of players) if (grid.contains(player.canvas)) players.delete(player);
  grid.replaceChildren();
  cardControls.clear();
  $("ownership-retry").hidden = ownershipState === "ready";
  // Keep a retry's keyboard focus while the read is in flight.
  $("ownership-retry").setAttribute("aria-disabled", String(ownershipState === "loading"));
  $("ownership-retry").textContent = ownershipState === "loading" ? "Checking ownership..." : "Retry ownership check";
  const category = $("emote-category").value;
  for (const value of ["all", "companion", "completion", "owned"]) {
    $(`type-${value}`).setAttribute("aria-pressed", String(value === category));
  }
  const matched = sortedItems(matchingItems(items, $("emote-search").value, category, owned), $("emote-sort").value, owned);
  const groups = characterGroups(matched);
  $("result-count").textContent = groups.length === matched.length
    ? `${matched.length} ${matched.length === 1 ? "emote" : "emotes"}`
    : `${groups.length} ${groups.length === 1 ? "character" : "characters"} / ${matched.length} animations`;
  $("empty-state").hidden = matched.length !== 0;
  for (const group of groups) {
    const item = selectedVariant(group, variantSelections);
    const card = node("article", undefined, "emote-card");
    const top = node("div", undefined, "emote-card-top");
    top.append(node("span", isStillPreview(item) ? "STILL PREVIEW" : item.category === "completion" ? "COMPLETION" : "COMPANION", "text-mono-xs"), node("span", owned.has(item.id) ? "Owned" : isPurchasable(item) ? `${money(item.cost_v)}` : "Preview", "availability-badge"));
    const stage = node("button", undefined, "sprite-stage");
    stage.type = "button";
    stage.setAttribute("aria-label", isStillPreview(item) ? `Preview still artwork for ${item.name}` : `Preview ${item.name}`);
    stage.setAttribute("aria-haspopup", "dialog");
    stage.setAttribute("data-emote-control", `${item.id}:artwork`);
    stage.setAttribute("data-ov-product-action", "preview");
    cardControls.set(`${item.id}:artwork`, stage);
    stage.addEventListener("click", () => openPreview(item, "artwork"));
    const body = node("div", undefined, "emote-card-body");
    body.append(node("h3", group.name));
    if (group.items.length > 1) {
      const label = node("label", "Animation", "variant-label");
      const select = node("select");
      select.setAttribute("aria-label", `Animation for ${group.name}`);
      select.setAttribute("data-emote-control", `${group.id}:variant`);
      cardControls.set(`${group.id}:variant`, select);
      for (const variant of group.items) {
        const entitlement = owned.get(variant.id);
        const status = ownershipState === "ready" && entitlement
          ? entitlement.equipped ? "Equipped" : "Owned" : isPurchasable(variant) ? `${money(variant.cost_v)}` : "Preview only";
        const option = node("option", `${animationLabel(variant)} / ${status}`);
        option.value = variant.id;
        select.append(option);
      }
      select.value = item.id;
      select.addEventListener("change", () => {
        if (!group.items.some((variant) => variant.id === select.value)) return;
        variantSelections.set(group.id, select.value);
        render();
      });
      label.append(select);
      body.append(label, node("span", "Separate items, not a character bundle.", "variant-note"));
    } else if (item.animation_name) body.append(node("span", animationLabel(item), "variant-note"));
    body.append(node("p", item.description || "View pack details and available preview states."));
    const actions = node("div", undefined, "emote-card-actions");
    const preview = node("button", "Details", "btn btn-primary");
    preview.setAttribute("aria-label", `Preview details for ${item.name}`);
    preview.setAttribute("aria-haspopup", "dialog");
    preview.setAttribute("data-emote-control", `${item.id}:preview`);
    preview.setAttribute("data-ov-product-action", "preview");
    cardControls.set(`${item.id}:preview`, preview);
    preview.type = "button"; preview.addEventListener("click", () => openPreview(item)); actions.append(preview);
    if (owned.has(item.id) || isPurchasable(item)) {
      const entitlement = owned.get(item.id);
      const unavailable = entitlement && entitlement.activatable === false;
      const label = unavailable ? "Activation unavailable" : entitlement?.equipped ? "Equipped" : entitlement ? "Equip" : `Buy for ${money(item.cost_v)}`;
      const buy = node("button", label, "btn btn-secondary");
      buy.setAttribute("data-emote-control", `${item.id}:action`);
      cardControls.set(`${item.id}:action`, buy);
      buy.disabled = Boolean(unavailable || entitlement?.equipped || pendingItems.has(item.id) || ownershipState !== "ready");
      buy.type = "button"; buy.addEventListener("click", () => void transact(item)); actions.append(buy);
    } else actions.append(node("span", "Not yet for sale", "text-mono-xs"));
    body.append(actions); card.append(top, stage, body); grid.append(card);
    void mountPreview(stage, item);
  }
  if (focused && !$("emote-dialog").open) restoreCardFocus(focused);
  else if (retryFocused && $("ownership-retry").hidden && !$("emote-dialog").open) $("emote-search").focus({ preventScroll: true });
}

async function load() {
  $("result-count").textContent = "Loading collection...";
  try {
    const response = await fetch("/store/emotes/catalog", { signal: AbortSignal.timeout(10000) });
    if (!response.ok) throw new Error("Collection unavailable.");
    items = catalogItems(await response.json());
    tell(collectionStatus(items));
    render();
    await refreshOwned();
  } catch {
    $("result-count").textContent = "Collection unavailable";
    tell("The collection could not load. ");
    const retry = node("button", "Retry", "text-button"); retry.addEventListener("click", () => void load()); $("catalog-status").append(retry);
  }
}

$("emote-search").addEventListener("input", render);
$("emote-category").addEventListener("change", render);
$("emote-sort").addEventListener("change", render);
for (const value of ["all", "companion", "completion", "owned"]) {
  $(`type-${value}`).addEventListener("click", () => { $("emote-category").value = value; render(); });
}
$("clear-filters").addEventListener("click", () => {
  $("emote-search").value = "";
  $("emote-category").value = "all";
  $("emote-sort").value = "default";
  render();
});
for (const clip of Object.keys(clipLabels)) {
  $(`preview-${clip}`).addEventListener("click", () => {
    if (!dialogItem || isStillPreview(dialogItem) || !$("emote-dialog").open || !dialogClips.includes(clip) || clip === dialogClip) return;
    updatePreviewStates(dialogClips, clip);
    void mountPreview($("preview-stage"), dialogItem, clip, true);
  });
}
$("ownership-retry").addEventListener("click", () => { if (ownershipState !== "loading") void refreshOwned(); });
for (const id of ["motion-toggle", "preview-motion-toggle"]) {
  $(id).addEventListener("click", () => {
    if (!media.matches && !(id === "preview-motion-toggle" && isStillPreview(dialogItem))) {
      paused = !paused; updateMotion();
    }
  });
}
media.addEventListener("change", () => {
  paused = media.matches;
  $("replay-preview").disabled = media.matches || isStillPreview(dialogItem);
  updateMotion();
});
document.addEventListener("visibilitychange", updateMotion);
$("emote-dialog").addEventListener("close", () => {
  // A queued close event must not invalidate a modal that has already reopened.
  if (!$("emote-dialog").open) {
    clearPreview($("preview-stage"));
    dialogItem = null;
    ++copyRequest;
    updatePreviewStates([], null);
    if (dialogOpener) restoreCardFocus(dialogOpener);
    dialogOpener = null;
  }
});
$("copy-command").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText("openvegas emote"); $("copy-command").textContent = "Copied"; }
  catch { $("copy-command").textContent = "Select the command below to copy"; }
});
$("copy-preview-command").addEventListener("click", async () => {
  const command = previewCommand(dialogItem);
  if (!command || !$("emote-dialog").open) return;
  const request = ++copyRequest;
  let message;
  try { await navigator.clipboard.writeText(command); message = "Preview command copied."; }
  catch { message = "Copy unavailable. Select the command above to copy it manually."; }
  if (request === copyRequest && $("emote-dialog").open) $("preview-copy-status").textContent = message;
});
window.addEventListener("pagehide", () => {
  clearPreview($("preview-stage"));
  dialogItem = null;
  ++copyRequest;
  if (animationId !== null) cancelAnimationFrame(animationId);
  players.clear();
});
updateMotion();
void load();

$("replay-preview").addEventListener("click", () => {
  if (media.matches || isStillPreview(dialogItem) || dialogClip !== "complete" || !$("emote-dialog").open) return;
  for (const player of players) if ($("preview-stage").contains(player.canvas)) {
    player.elapsedMs = 0; player.last = null; player.lastTick = null;
  }
  paused = false; updateMotion();
});
