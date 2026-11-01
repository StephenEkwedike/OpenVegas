import {money} from './currency.js';
const SAFE_ID = /^[a-z0-9][a-z0-9._-]{0,63}$/;
const SCENES = new Set([
  "/ui/assets/emotes/previews/scenes/konoha-village.png",
  "/ui/assets/emotes/previews/scenes/basketball-arena.png",
]);

export function scenePath(value) {
  return typeof value === "string" && SCENES.has(value) ? value : null;
}

export function isStillPreview(item) {
  return item?.preview_kind === "still";
}

export function previewPath(value) {
  return typeof value === "string" && /^\/ui\/assets\/emotes\/[a-zA-Z0-9_./-]+$/.test(value)
    && !value.split("/").some((part) => part === "." || part === "..") ? value : null;
}

export function catalogItems(payload) {
  if (!Array.isArray(payload?.items)) throw new Error("Collection unavailable. Please retry.");
  const seen = new Set();
  return payload.items.slice(0, 256).filter((item) => {
    if (!SAFE_ID.test(item?.id || "") || seen.has(item.id)) return false;
    seen.add(item.id);
    return typeof item.name === "string" && item.name.length <= 128;
  });
}

export function matchingItems(items, query, category, owned) {
  const needle = String(query || "").trim().toLowerCase();
  return items.filter((item) => (
    category === "all" || (category === "owned" ? owned.has(item.id) : item.category === category)
  ) && `${item.name} ${item.character_name || ""} ${item.animation_name || ""} ${item.description || ""} ${item.category || ""}`.toLowerCase().includes(needle));
}

function label(value) {
  return typeof value === "string" && value.trim().length > 0 && value.length <= 128
    && !/[\u0000-\u001f\u007f]/.test(value) ? value : null;
}

export function characterGroups(items) {
  const groups = new Map();
  for (const item of items) {
    const explicit = typeof item.character_id === "string" && SAFE_ID.test(item.character_id)
      && label(item.character_name);
    const id = explicit ? `character.${item.character_id}` : `item.${item.id}`;
    if (!groups.has(id)) groups.set(id, { id, name: explicit || item.name, items: [] });
    groups.get(id).items.push(item);
  }
  return [...groups.values()];
}

export function animationLabel(item) {
  return label(item?.animation_name) || item.name;
}

export function selectedVariant(group, selections) {
  return group.items.find((item) => item.id === selections.get(group.id)) || group.items[0];
}

export function sortedItems(items, order, owned) {
  const result = items.slice();
  if (order === "name") result.sort((a, b) => a.name.localeCompare(b.name, "en", { sensitivity: "base", numeric: true }));
  if (order === "owned") result.sort((a, b) => Number(owned.has(b.id)) - Number(owned.has(a.id)));
  return result;
}

export function previewCommand(item) {
  const id = item?.pack_id ?? item?.id;
  return typeof id === "string" && SAFE_ID.test(id)
    ? `openvegas emote preview ${id}${isStillPreview(item) ? " --reduced-motion" : ""}` : null;
}

export function danceSpec(manifest, clip = "waiting") {
  const { width, height } = manifest?.frame || {};
  if (!["idle", "waiting", "complete"].includes(clip)) throw new Error("Invalid preview clip");
  const animation = manifest?.animations?.[clip];
  if (manifest?.schema_version !== 1 || ![width, height].every((n) => Number.isInteger(n) && n > 0 && n <= 512)
    || !Array.isArray(animation?.frames) || !animation.frames.length || animation.frames.length > 64
    || !animation.frames.every((n) => Number.isInteger(n) && n >= 0 && n < 256)
    || !Number.isFinite(animation.frame_ms) || animation.frame_ms < 33 || animation.frame_ms > 2000) {
    throw new Error("Preview format not supported.");
  }
  const reduced = manifest.reduced_motion_frame;
  return { width, height, frames: clip === "idle" ? [animation.frames[0]] : animation.frames.slice(),
    frameMs: animation.frame_ms, loop: clip === "waiting" && animation.loop !== false,
    reducedMotionFrame: Number.isInteger(reduced) && reduced >= 0 && reduced < 256 ? reduced : animation.frames[0] };
}

export function previewSpecs(manifest, kind = "animated") {
  const specs = {};
  for (const clip of ["idle", "waiting", "complete"]) {
    try { specs[clip] = danceSpec(manifest, clip); } catch { /* Omit unsupported or absent states. */ }
  }
  if (kind === "still") {
    const spec = specs.idle || specs.waiting || specs.complete;
    if (!spec) return {};
    const frame = specs.idle ? spec.frames[0] : spec.reducedMotionFrame;
    return { idle: { ...spec, frames: [frame], reducedMotionFrame: frame, loop: false } };
  }
  return specs;
}

export function isPurchasable(item) {
  return item?.purchasable === true && item?.preview_only === false
    && typeof item.cost_v === "string" && /^\d+(\.\d{1,6})?$/.test(item.cost_v);
}

export function plannedPriceLabel(item) {
  if (isPurchasable(item) || typeof item?.planned_cost_usd !== "string"
    || typeof item?.planned_cost_v !== "string"
    || !/^\d+(\.\d{1,2})?$/.test(item.planned_cost_usd)
    || !/^\d+(\.\d{1,6})?$/.test(item.planned_cost_v)) return "";
  return `Planned price: ${money(item.planned_cost_v)} (US$${Number(item.planned_cost_usd).toFixed(2)}), one-time. Not yet for sale.`;
}

export function previewFrame(spec, elapsedMs, reducedMotion = false) {
  if (reducedMotion) return spec.reducedMotionFrame ?? spec.frames[0];
  const tick = Math.floor(Math.max(0, Number.isFinite(elapsedMs) ? elapsedMs : 0) / spec.frameMs);
  return spec.frames[spec.loop ? tick % spec.frames.length : Math.min(tick, spec.frames.length - 1)];
}

export function previewSample(spec, elapsedMs, reducedMotion = false, blendMs = 0) {
  const frame = previewFrame(spec, elapsedMs, reducedMotion);
  const elapsed = Math.max(0, Number.isFinite(elapsedMs) ? elapsedMs : 0);
  const tick = Math.floor(elapsed / spec.frameMs);
  const duration = Number.isFinite(blendMs) ? Math.max(0, Math.min(200, spec.frameMs / 2, blendMs)) : 0;
  if (reducedMotion || !duration || (!spec.loop && tick >= spec.frames.length - 1)) return { frame, next: frame, mix: 0 };
  const next = spec.frames[(tick + 1) % spec.frames.length];
  // Blend only at an authored pose boundary, preserving repeated holds and timing.
  const progress = Math.max(0, (elapsed % spec.frameMs - (spec.frameMs - duration)) / duration);
  const mix = next === frame ? 0 : progress * progress * (3 - 2 * progress);
  return { frame, next: mix ? next : frame, mix };
}

export function defaultPreviewClip(item, specs, detail = false) {
  if (typeof item.preview_clip === "string" && Object.hasOwn(specs, item.preview_clip)) return item.preview_clip;
  return detail && specs.idle && item.category !== "completion" ? "idle" :
    item.category === "completion" && specs.complete ? "complete" : specs.waiting ? "waiting" : Object.keys(specs)[0];
}

export function collectionStatus(items) {
  const count = items.filter(isPurchasable).length;
  return count ? `${count} packs available with your OpenVegas balance. Other packs are previews.`
    : "Preview companions and completion celebrations. Sales remain closed during review.";
}

export function equipmentRequest(item) {
  if (!["companion", "completion"].includes(item?.category) || !SAFE_ID.test(item?.id || "")) {
    throw new Error("Unsupported equipment slot");
  }
  return { item_id: item.id, slot: item.category };
}
