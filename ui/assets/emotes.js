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
const owned = new Map();
const purchaseKeys = new Map();
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
  $("motion-toggle").textContent = paused ? "Play previews" : "Pause previews";
  $("motion-toggle").setAttribute("aria-pressed", String(paused));
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

async function mountPreview(stage, item) {
  const message = node("p", "Loading animation preview...", "text-mono-xs");
  stage.append(message);
  try {
    const { image, spec } = await previewData(item);
    if (!stage.isConnected) return;
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
  } catch (error) { message.textContent = error.message || "Preview unavailable."; }
}

function openPreview(item) {
  $("preview-title").textContent = item.name;
  $("preview-description").textContent = item.description || "";
  $("preview-stage").replaceChildren();
  $("preview-command").textContent = `openvegas emote preview ${item.pack_id || item.id}`;
  $("preview-notice").textContent = "Visual prototype. Previewing does not purchase or equip this pack.";
  $("replay-preview").hidden = item.category !== "completion";
  $("replay-preview").disabled = media.matches;
  $("emote-dialog").showModal();
  void mountPreview($("preview-stage"), item);
}

async function refreshOwned() {
  try {
    const result = await apiJson("/store/emotes/owned");
    owned.clear();
    for (const entry of result.entitlements || []) {
      if (entry.effective_status === "active") owned.set(entry.item_id, entry);
    }
  } catch (error) {
    owned.clear();
    if (error.status !== 401) tell("Previews are available. Ownership could not be checked; try again before purchasing.");
  }
}

async function transact(item, button) {
  button.disabled = true;
  try {
    if (owned.has(item.id)) {
      await apiJson("/store/emotes/equip", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(equipmentRequest(item)) });
      tell(`${item.name} equipped on your account. In the updated CLI, run openvegas emote sync to restore it on this device. Existing terminals stop safely when the selection changes.`);
    } else {
      if (!isPurchasable(item)) return;
      if (!window.confirm(`Purchase ${item.name} for ${item.cost_v} $V from your OpenVegas balance?`)) return;
      const key = purchaseKeys.get(item.id) || crypto.randomUUID();
      purchaseKeys.set(item.id, key);
      await apiJson("/store/buy", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ item_id: item.id, idempotency_key: key }) });
      purchaseKeys.delete(item.id);
      tell(`${item.name}: purchase checked. Your current ownership is shown below.`);
    }
    await refreshOwned();
    render();
  } catch (error) {
    if (error.status === 401) {
      tell("Sign in to purchase or equip an emote.");
      const link = node("a", " Sign in");
      link.href = getLoginHref("/ui/emotes");
      $("catalog-status").append(link);
    } else if (error.status === 400) {
      tell("Purchase could not complete. Check your balance; no automatic top-up will be made.");
      const link = node("a", " View balance"); link.href = "/ui/balance"; $("catalog-status").append(link);
    } else tell("Could not confirm this action. Retry uses the same purchase reference to avoid a duplicate charge.");
  } finally { button.disabled = false; }
}

function render() {
  const grid = $("emote-grid");
  grid.replaceChildren();
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
    const priceLabel = plannedPriceLabel(item);
    if (priceLabel) body.append(node("p", priceLabel, "text-mono-xs"));
    const actions = node("div", undefined, "emote-card-actions");
    const preview = node("button", item.category === "completion" ? "Preview celebration" : "Preview dance", "btn btn-primary");
    preview.type = "button"; preview.addEventListener("click", () => openPreview(item)); actions.append(preview);
    if (owned.has(item.id) || isPurchasable(item)) {
      const entitlement = owned.get(item.id);
      const unavailable = entitlement && entitlement.activatable === false;
      const label = unavailable ? "Activation unavailable" : entitlement?.equipped ? "Equipped" : entitlement ? "Equip" : `Buy for ${item.cost_v} $V`;
      const buy = node("button", label, "btn btn-secondary");
      buy.disabled = Boolean(unavailable || entitlement?.equipped);
      buy.type = "button"; buy.addEventListener("click", () => void transact(item, buy)); actions.append(buy);
    } else actions.append(node("span", "Not yet for sale", "text-mono-xs"));
    body.append(actions); card.append(top, stage, body); grid.append(card);
    void mountPreview(stage, item);
  }
}

async function load() {
  try {
    const response = await fetch("/store/emotes/catalog", { signal: AbortSignal.timeout(10000) });
    if (!response.ok) throw new Error("Collection unavailable.");
    items = catalogItems(await response.json());
    tell(collectionStatus(items));
    render();
    await refreshOwned();
    render();
  } catch {
    tell("The collection could not load. ");
    const retry = node("button", "Retry", "text-button"); retry.addEventListener("click", () => void load()); $("catalog-status").append(retry);
  }
}

$("emote-search").addEventListener("input", render);
$("emote-category").addEventListener("change", render);
$("motion-toggle").addEventListener("click", () => { paused = !paused; updateMotion(); });
media.addEventListener("change", () => { paused = media.matches; $("replay-preview").disabled = media.matches; updateMotion(); });
document.addEventListener("visibilitychange", updateMotion);
$("emote-dialog").addEventListener("close", () => { $("preview-stage").replaceChildren(); });
$("copy-command").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText("openvegas emote"); $("copy-command").textContent = "Copied"; }
  catch { $("copy-command").textContent = "Select the command below to copy"; }
});
window.addEventListener("pagehide", () => { if (animationId !== null) cancelAnimationFrame(animationId); players.clear(); });
updateMotion();
void load();

$("replay-preview").addEventListener("click", () => {
  if (media.matches) return;
  for (const player of players) if ($("preview-stage").contains(player.canvas)) {
    player.elapsedMs = 0; player.last = null; player.lastTick = null;
  }
  paused = false; updateMotion();
});
