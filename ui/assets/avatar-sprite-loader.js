let manifestPromise = null;
const spriteCache = new Map();

export async function loadAvatarManifest() {
  if (!manifestPromise) {
    manifestPromise = fetch("/ui/assets/avatar-manifest.json", { cache: "no-store" })
      .then((r) => {
        if (!r.ok) throw new Error(`avatar_manifest_http_${r.status}`);
        return r.json();
      })
      .catch(() => ({ version: 1, frame: { w: 16, h: 32, frames_per_row: 7 }, dealer: [], users: [] }));
  }
  return manifestPromise;
}

export function resolveSpriteFromManifest(manifest, kind, id) {
  const list = Array.isArray(manifest?.[kind]) ? manifest[kind] : [];
  return list.find((item) => String(item?.id || "") === String(id || "")) || null;
}

export async function loadSpriteSheet(url) {
  const key = String(url || "");
  if (!key) return null;
  if (spriteCache.has(key)) return spriteCache.get(key);

  const p = new Promise((resolve) => {
    const img = new Image();
    img.decoding = "async";
    img.onload = () => resolve({ image: img, ok: true });
    img.onerror = () => resolve({ image: null, ok: false });
    img.src = key;
  });
  spriteCache.set(key, p);
  return p;
}
