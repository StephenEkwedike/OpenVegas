const SAFE_ID = /^[a-z0-9][a-z0-9._-]{0,63}$/;

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
  ) && `${item.name} ${item.description || ""} ${item.category || ""}`.toLowerCase().includes(needle));
}

export function danceSpec(manifest, clip = "waiting") {
  const { width, height } = manifest?.frame || {};
  if (!["waiting", "complete"].includes(clip)) throw new Error("Invalid preview clip");
  const animation = manifest?.animations?.[clip];
  if (manifest?.schema_version !== 1 || ![width, height].every((n) => Number.isInteger(n) && n > 0 && n <= 512)
    || !Array.isArray(animation?.frames) || !animation.frames.length || animation.frames.length > 64
    || !animation.frames.every((n) => Number.isInteger(n) && n >= 0 && n < 256)
    || !Number.isFinite(animation.frame_ms) || animation.frame_ms < 33 || animation.frame_ms > 2000) {
    throw new Error("Preview format not supported.");
  }
  return { width, height, frames: animation.frames, frameMs: animation.frame_ms, loop: clip === "waiting" && animation.loop !== false };
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
  return `Planned price: ${Number(item.planned_cost_v).toLocaleString("en-US", { maximumFractionDigits: 6 })} $V (US$${Number(item.planned_cost_usd).toFixed(2)}), one-time. Not yet for sale.`;
}

export function previewFrame(spec, elapsedMs, reducedMotion = false) {
  if (reducedMotion) return spec.frames[0];
  const tick = Math.floor(Math.max(0, Number.isFinite(elapsedMs) ? elapsedMs : 0) / spec.frameMs);
  return spec.frames[spec.loop ? tick % spec.frames.length : Math.min(tick, spec.frames.length - 1)];
}
export function collectionStatus(items) {
  const count = items.filter(isPurchasable).length;
  return count ? `${count} packs available with your OpenVegas balance. Other packs are previews.`
    : "Preview original companions and human-player celebrations. Sales remain closed during review.";
}

export function equipmentRequest(item) {
  if (!["companion", "completion"].includes(item?.category) || !SAFE_ID.test(item?.id || "")) {
    throw new Error("Unsupported equipment slot");
  }
  return { item_id: item.id, slot: item.category };
}
