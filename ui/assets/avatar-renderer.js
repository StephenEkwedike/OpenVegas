import { stateRowIndex } from "/ui/assets/avatar-engine.js";

function colorForState(state) {
  switch (state) {
    case "typing": return "#44c6dd";
    case "reading": return "#7cdf7c";
    case "waiting": return "#ffd166";
    case "success": return "#67d389";
    case "error": return "#ff7b7b";
    default: return "#f5f5f5";
  }
}

function drawFallback(ctx, frame, state, label) {
  const w = ctx.canvas.width;
  const h = ctx.canvas.height;
  ctx.clearRect(0, 0, w, h);

  const tone = colorForState(state);
  const bob = frame % 2 === 0 ? 0 : 2;

  ctx.fillStyle = "#0c0f14";
  ctx.fillRect(0, 0, w, h);

  ctx.fillStyle = tone;
  ctx.fillRect(26, 18 + bob, 20, 20);
  ctx.fillRect(22, 40 + bob, 28, 28);
  ctx.fillRect(18, 70 + bob, 12, 30);
  ctx.fillRect(42, 70 + bob, 12, 30);

  ctx.fillStyle = "#040404";
  ctx.fillRect(30, 24 + bob, 3, 3);
  ctx.fillRect(39, 24 + bob, 3, 3);

  ctx.fillStyle = "#9a9a9a";
  ctx.font = "10px monospace";
  ctx.fillText(String(label || "dealer"), 8, h - 10);
}

function drawOfficeBackdrop(ctx, state) {
  const w = ctx.canvas.width;
  const h = ctx.canvas.height;
  const grad = ctx.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, "#10192a");
  grad.addColorStop(0.62, "#0b1322");
  grad.addColorStop(1, "#070d16");
  ctx.fillStyle = grad;
  ctx.fillRect(0, 0, w, h);

  const tile = 12;
  const floorTop = Math.floor(h * 0.64);
  for (let y = floorTop; y < h; y += tile) {
    for (let x = 0; x < w; x += tile) {
      const alt = (((x / tile) + (y / tile)) & 1) === 0;
      ctx.fillStyle = alt ? "#122035" : "#0f1a2c";
      ctx.fillRect(x, y, tile, tile);
    }
  }
  ctx.fillStyle = "rgba(99, 216, 255, 0.08)";
  ctx.fillRect(0, floorTop - 8, w, 8);

  ctx.fillStyle = "#1b2a42";
  ctx.fillRect(8, 12, 28, 18);
  ctx.fillStyle = state === "success" ? "#67d389" : state === "error" ? "#ff7b7b" : "#44c6dd";
  ctx.fillRect(12, 16, 20, 10);
  ctx.fillStyle = "#8ca4c6";
  ctx.fillRect(w - 34, 16, 22, 4);
  ctx.fillRect(w - 30, 22, 14, 4);
}

function resolveSheetModel(sheet, frameMeta) {
  if (!sheet) return null;
  const fw = Number(frameMeta?.w || 16);
  const fh = Number(frameMeta?.h || 32);
  if (!Number.isFinite(fw) || !Number.isFinite(fh) || fw <= 0 || fh <= 0) return null;
  const rows = Math.floor(Number(sheet.height || 0) / fh);
  const cols = Math.floor(Number(sheet.width || 0) / fw);
  if (rows < 1 || cols < 1) return null;
  if (rows >= 7) return { model: "state_rows", rows, cols, fw, fh };
  if (rows >= 3) return { model: "direction_rows", rows, cols, fw, fh };
  return { model: "single_row", rows, cols, fw, fh };
}

function mapDirectionRowFromState(state, rowCount) {
  const token = String(state || "idle");
  if (token === "walk") return Math.min(1, rowCount - 1);
  if (token === "typing" || token === "reading" || token === "waiting") return Math.min(2, rowCount - 1);
  return 0;
}

function frameIndexForModel(model, frame, colCount, state) {
  const n = Math.max(1, Number(colCount || 1));
  const f = Number(frame || 0);
  if (model === "direction_rows") {
    const token = String(state || "idle");
    if (token === "walk") return f % n;
    const idleLoop = [0, 1, 2, 1];
    return idleLoop[f % idleLoop.length] % n;
  }
  return f % n;
}

export function renderAvatarFrame(ctx, opts) {
  const {
    sprite,
    frameMeta,
    state,
    frame,
    label,
  } = opts || {};

  const sheet = sprite?.image;
  const model = resolveSheetModel(sheet, frameMeta);
  if (!model) {
    drawFallback(ctx, Number(frame || 0), String(state || "idle"), label);
    return { mode: "fallback" };
  }

  const fw = model.fw;
  const fh = model.fh;
  let row = 0;
  if (model.model === "state_rows") {
    row = Math.min(stateRowIndex(state), model.rows - 1);
  } else if (model.model === "direction_rows") {
    row = mapDirectionRowFromState(state, model.rows);
  }
  const col = frameIndexForModel(model.model, frame, model.cols, state);

  ctx.clearRect(0, 0, ctx.canvas.width, ctx.canvas.height);
  ctx.imageSmoothingEnabled = false;
  drawOfficeBackdrop(ctx, String(state || "idle"));

  const bob = String(state || "idle") === "walk" ? (Number(frame || 0) % 2 === 0 ? 0 : 1) : 0;
  const dstX = Math.floor((ctx.canvas.width - fw * 4) / 2);
  const dstY = 8 + bob;
  const dstW = fw * 4;
  const dstH = fh * 4;

  ctx.fillStyle = "rgba(0,0,0,0.35)";
  ctx.beginPath();
  ctx.ellipse(
    Math.floor(ctx.canvas.width / 2),
    Math.floor(dstY + dstH - 6),
    Math.floor(dstW * 0.22),
    4,
    0,
    0,
    Math.PI * 2,
  );
  ctx.fill();

  ctx.drawImage(
    sheet,
    col * fw,
    row * fh,
    fw,
    fh,
    dstX,
    dstY,
    dstW,
    dstH,
  );
  return { mode: "sprite" };
}
