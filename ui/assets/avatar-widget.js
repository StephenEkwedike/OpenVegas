import { AvatarEngine, AvatarState, mapToolEventToState } from "/ui/assets/avatar-engine.js";
import { renderAvatarFrame } from "/ui/assets/avatar-renderer.js";
import { loadAvatarManifest, loadSpriteSheet, resolveSpriteFromManifest } from "/ui/assets/avatar-sprite-loader.js";

function emitMetric(name, tags = {}) {
  if (typeof window?.openvegasEmitMetric === "function") {
    try { window.openvegasEmitMetric(name, tags); } catch (_) {}
  }
}

function defaultNode(containerId) {
  const host = document.getElementById(containerId);
  if (!host) return null;

  host.classList.add("ov-avatar-widget");
  host.innerHTML = `
    <div class="ov-avatar-framebox">
      <canvas class="ov-avatar-canvas" width="120" height="180" aria-label="OpenVegas animated avatar"></canvas>
      <div class="ov-avatar-state" aria-live="polite"></div>
    </div>
  `;
  return host;
}

async function loadPreferences() {
  try {
    const res = await fetch("/ui/profile/preferences", {
      method: "GET",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
    });
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

export function emitAvatarEvent(detail) {
  window.dispatchEvent(new CustomEvent("openvegas:avatar-event", { detail: detail || {} }));
}

export async function mountDealerWidget({ containerId = "dealerWidget", kind = "dealer", label = "dealer" } = {}) {
  const node = defaultNode(containerId);
  if (!node) return null;

  const canvas = node.querySelector("canvas");
  const stateNode = node.querySelector(".ov-avatar-state");
  if (!canvas || !stateNode) return null;

  const ctx = canvas.getContext("2d");
  if (!ctx) return null;

  const manifest = await loadAvatarManifest();
  const frameMeta = manifest?.frame || { w: 16, h: 32, frames_per_row: 7 };
  const prefs = await loadPreferences();

  const dealerId = prefs?.dealer_skin_id || "ov_dealer_female_tux_v1";
  const avatarId = prefs?.avatar_id || "ov_user_01";
  const spriteRef = resolveSpriteFromManifest(manifest, kind === "dealer" ? "dealer" : "users", kind === "dealer" ? dealerId : avatarId);
  const sprite = await loadSpriteSheet(spriteRef?.sheet || "");

  const engine = new AvatarEngine({
    onTransition: ({ from, to }) => {
      stateNode.textContent = `${label}: ${to}`;
      emitMetric("avatar_state_transition_total", { surface: "web", from, to });
    },
  });

  const stateLabelPrefix = kind === "dealer" ? "Dealer" : "Avatar";
  stateNode.textContent = `${stateLabelPrefix}: ${AvatarState.IDLE}`;

  if (!sprite?.ok) {
    emitMetric("avatar_asset_load_fail_total", { reason: "sprite_load_failed" });
  }
  const emittedRenderModes = new Set();

  let raf = 0;
  const loop = () => {
    const tick = engine.tick(Date.now(), Number(frameMeta?.frames_per_row || 7));
    const draw = renderAvatarFrame(ctx, {
      sprite,
      frameMeta,
      state: tick.state,
      frame: tick.frame,
      label: `${stateLabelPrefix.toLowerCase()}`,
    });
    const mode = String(draw?.mode || "fallback");
    if (!emittedRenderModes.has(mode)) {
      emitMetric("avatar_render_mode_total", { surface: "web", mode });
      emittedRenderModes.add(mode);
    }
    raf = window.requestAnimationFrame(loop);
  };
  loop();

  const onEvt = (evt) => {
    const next = mapToolEventToState(evt?.detail || {});
    engine.setState(next);
  };
  window.addEventListener("openvegas:avatar-event", onEvt);

  return {
    setState: (state) => engine.setState(state),
    destroy: () => {
      window.removeEventListener("openvegas:avatar-event", onEvt);
      if (raf) window.cancelAnimationFrame(raf);
    },
  };
}
