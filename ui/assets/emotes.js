import { renderTopNav, renderFounderLinks, installAssetGuard } from "/ui/assets/site.js?v=20260330";
import { apiJson, getLoginHref } from "/ui/assets/page-auth.js?v=20260330";
import { catalogItems, collectionStatus, equipmentRequest, danceSpec, previewFrame, isPurchasable, matchingItems, previewPath, plannedPriceLabel } from "./emote-view.js?v=3";

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
let ownershipState = "loading";
let ownershipRequest = 0;
let dialogOpener = null;
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
  const index = previewFrame(spec, player.elapsedMs, media.matches);
  if (index === player.last) return;
  player.last = index;
  const context = canvas.getContext("2d");
  context.clearRect(0, 0, spec.width, spec.height);
  const columns = image.naturalWidth / spec.width;
  context.drawImage(image, (index % columns) * spec.width, Math.floor(index / columns) * spec.height,
    spec.width, spec.height, 0, 0, spec.width, spec.height);
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
    $(id).disabled = media.matches;
  }
  tick(performance.now());
}

async function previewData(item) {
  const sheet = previewPath(item.preview_url);
  const manifestUrl = previewPath(item.preview_manifest_url);
  if (!sheet || !manifestUrl) throw new Error("Artwork preview coming soon.");
  const clip = item.category === "completion" ? "complete" : "waiting";
  const cacheKey = `${manifestUrl}|${sheet}|${clip}`;
  if (!images.has(cacheKey)) images.set(cacheKey, (async () => {
    const response = await fetch(manifestUrl, { signal: AbortSignal.timeout(10000) });
    if (!response.ok) throw new Error("Preview could not load. Retry in a moment.");
    const text = await response.text();
    if (text.length > 65536) throw new Error("Preview exceeds supported size.");
    const spec = danceSpec(JSON.parse(text), clip);
    const image = new Image();
    image.src = sheet;
    await Promise.race([image.decode(), new Promise((_, reject) => setTimeout(() => reject(new Error("Preview timed out.")), 10000))]);
    const columns = image.naturalWidth / spec.width;
    const rows = image.naturalHeight / spec.height;
    if (!Number.isInteger(columns) || !Number.isInteger(rows) || columns * rows > 256
      || image.naturalWidth * image.naturalHeight > 4194304 || Math.max(...spec.frames) >= columns * rows) {
      throw new Error("Invalid preview sheet dimensions.");
    }
    return { image, spec };
  })().catch((error) => { images.delete(cacheKey); throw error; }));
  return images.get(cacheKey);
}

function clearPreview(stage) {
  previewRequests.delete(stage);
  for (const player of players) if (stage.contains(player.canvas)) players.delete(player);
  stage.replaceChildren();
}

async function mountPreview(stage, item) {
  const request = {};
  previewRequests.set(stage, request);
  // The modal reuses its connected stage when another pack is selected.
  const current = () => stage.isConnected && previewRequests.get(stage) === request;
  const message = node("p", "Loading animation preview...", "text-mono-xs");
  stage.append(message);
  try {
    const { image, spec } = await previewData(item);
    if (!current()) return;
    const canvas = node("canvas");
    canvas.width = spec.width;
    canvas.height = spec.height;
    canvas.classList.toggle("completion-preview", item.category === "completion");
    canvas.setAttribute("role", "img");
    canvas.setAttribute("aria-label", `${item.name}, ${item.category === "completion" ? "a twelve-pose sports completion preview" : "an eight-pose pixel dance"}. Use Pause previews to stop motion.`);
    stage.replaceChildren(canvas);
    const player = { canvas, image, spec, last: null, lastTick: null, elapsedMs: 0 };
    players.add(player);
    draw(player, performance.now());
    if (animationId === null) updateMotion();
  } catch (error) {
    if (current()) message.textContent = error.message || "Preview unavailable.";
  }
}

function openPreview(item) {
  dialogOpener = `${item.id}:preview`;
  $("preview-title").textContent = item.name;
  $("preview-description").textContent = item.description || "";
  clearPreview($("preview-stage"));
  $("preview-command").textContent = `openvegas emote preview ${item.pack_id || item.id}`;
  $("preview-notice").textContent = `Visual prototype. Previewing does not purchase or equip this pack. ${compatibilityLabel(item)}`;
  $("replay-preview").hidden = item.category !== "completion";
  $("replay-preview").disabled = media.matches;
  $("emote-dialog").showModal();
  void mountPreview($("preview-stage"), item);
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
    || !window.confirm(`Purchase ${item.name} for ${item.cost_v} $V from your OpenVegas balance?`))) return;
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
  grid.replaceChildren();
  cardControls.clear();
  $("ownership-retry").hidden = ownershipState === "ready";
  // Keep a retry's keyboard focus while the read is in flight.
  $("ownership-retry").setAttribute("aria-disabled", String(ownershipState === "loading"));
  $("ownership-retry").textContent = ownershipState === "loading" ? "Checking ownership..." : "Retry ownership check";
  const matched = matchingItems(items, $("emote-search").value, $("emote-category").value, owned);
  $("result-count").textContent = `${matched.length} ${matched.length === 1 ? "emote" : "emotes"}`;
  $("empty-state").hidden = matched.length !== 0;
  for (const item of matched) {
    const card = node("article", undefined, "emote-card");
    const top = node("div", undefined, "emote-card-top");
    top.append(node("span", item.category === "completion" ? "COMPLETION" : "COMPANION", "text-mono-xs"), node("span", owned.has(item.id) ? "Owned" : isPurchasable(item) ? `${item.cost_v} $V` : "Preview", "availability-badge"));
    const stage = node("div", undefined, "sprite-stage");
    const body = node("div", undefined, "emote-card-body");
    body.append(node("h3", item.name), node("p", item.description || "Original pixel companion."));
    body.append(node("p", compatibilityLabel(item), "text-mono-xs"));
    const priceLabel = plannedPriceLabel(item);
    if (priceLabel) body.append(node("p", priceLabel, "text-mono-xs"));
    const actions = node("div", undefined, "emote-card-actions");
    const preview = node("button", item.category === "completion" ? "Preview celebration" : "Preview dance", "btn btn-primary");
    preview.setAttribute("data-emote-control", `${item.id}:preview`);
    cardControls.set(`${item.id}:preview`, preview);
    preview.type = "button"; preview.addEventListener("click", () => openPreview(item)); actions.append(preview);
    if (owned.has(item.id) || isPurchasable(item)) {
      const entitlement = owned.get(item.id);
      const unavailable = entitlement && entitlement.activatable === false;
      const label = unavailable ? "Activation unavailable" : entitlement?.equipped ? "Equipped" : entitlement ? "Equip" : `Buy for ${item.cost_v} $V`;
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
  try {
    const response = await fetch("/store/emotes/catalog", { signal: AbortSignal.timeout(10000) });
    if (!response.ok) throw new Error("Collection unavailable.");
    items = catalogItems(await response.json());
    tell(collectionStatus(items));
    render();
    await refreshOwned();
  } catch {
    tell("The collection could not load. ");
    const retry = node("button", "Retry", "text-button"); retry.addEventListener("click", () => void load()); $("catalog-status").append(retry);
  }
}

$("emote-search").addEventListener("input", render);
$("emote-category").addEventListener("change", render);
$("ownership-retry").addEventListener("click", () => { if (ownershipState !== "loading") void refreshOwned(); });
for (const id of ["motion-toggle", "preview-motion-toggle"]) {
  $(id).addEventListener("click", () => { if (!media.matches) { paused = !paused; updateMotion(); } });
}
media.addEventListener("change", () => { paused = media.matches; $("replay-preview").disabled = media.matches; updateMotion(); });
document.addEventListener("visibilitychange", updateMotion);
$("emote-dialog").addEventListener("close", () => {
  // A queued close event must not invalidate a modal that has already reopened.
  if (!$("emote-dialog").open) {
    clearPreview($("preview-stage"));
    if (dialogOpener) restoreCardFocus(dialogOpener);
    dialogOpener = null;
  }
});
$("copy-command").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText("openvegas emote"); $("copy-command").textContent = "Copied"; }
  catch { $("copy-command").textContent = "Select the command below to copy"; }
});
window.addEventListener("pagehide", () => {
  clearPreview($("preview-stage"));
  if (animationId !== null) cancelAnimationFrame(animationId);
  players.clear();
});
updateMotion();
void load();

$("replay-preview").addEventListener("click", () => {
  if (media.matches) return;
  for (const player of players) if ($("preview-stage").contains(player.canvas)) {
    player.elapsedMs = 0; player.last = null; player.lastTick = null;
  }
  paused = false; updateMotion();
});
