// Host-owned pointer region. No process, filesystem, network, billing or prompt APIs.
const ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';

function words(encoded) {
  const bytes = [];
  for (let i = 0; i < encoded.length; i += 4) {
    const n = (ALPHABET.indexOf(encoded[i]) << 18) | (ALPHABET.indexOf(encoded[i + 1]) << 12) |
      (ALPHABET.indexOf(encoded[i + 2]) << 6) | ALPHABET.indexOf(encoded[i + 3]);
    bytes.push((n >>> 16) & 255, (n >>> 8) & 255, n & 255);
  }
  const result = [];
  for (let i = 0; i < bytes.length; i += 4) result.push((bytes[i] | (bytes[i + 1] << 8) |
    (bytes[i + 2] << 16) | (bytes[i + 3] << 24)) >>> 0);
  return result;
}

export default function Pet(props, surface) {
  const { Box, Text } = surface.elements;
  const now = performance.now();
  const initial = { lease: props.lease, session: props.session, deadline: now + props.ttl, expired: false, down: null };
  if (!surface.state) {
    surface.setState(initial);
    surface.every(100, () => {
      const state = surface.state;
      if (!state.expired && performance.now() >= state.deadline) {
        surface.setState({ ...state, expired: true, down: null });
      }
    });
  }
  const state = surface.state || initial;
  if (!Number.isInteger(props.columns) || props.columns < 1 || props.columns > 67 ||
      !Number.isInteger(props.rows) || props.rows < 1 || props.rows > 34 ||
      typeof props.cells !== 'string' || props.cells.length !== props.columns * props.rows * 16 ||
      !/^[A-Za-z0-9+/]+$/.test(props.cells) || !Number.isFinite(props.ttl) || props.ttl <= 0 || props.ttl > 30000 ||
      !Number.isFinite(props.position) || props.position < 0 || props.position > 1) return Box({ children: [] });
  if (state.lease !== props.lease || state.session !== props.session) {
    surface.setState({ lease: props.lease, session: props.session, deadline: now + props.ttl, expired: false, down: null });
  }
  const live = () => !surface.state.expired && performance.now() < surface.state.deadline && props.enabled;
  surface.onPointer(event => {
    if (!live() || event.shift || event.alt || event.ctrl || event.button !== 'left') return;
    if (event.type === 'down') {
      surface.setState({ ...surface.state, down: { x: event.x, y: event.y, at: performance.now() } });
    } else if (event.type === 'up') {
      const down = surface.state.down;
      surface.setState({ ...surface.state, down: null });
      if (down && performance.now() - down.at <= 600 && Math.abs(event.x - down.x) <= 1 &&
          Math.abs(event.y - down.y) <= 1 && event.x >= 0 && event.x < surface.columns && event.y >= 0 && event.y < surface.rows) {
        surface.post({ action: 'tap', lease: props.lease, session: props.session, x: event.x / surface.columns });
      }
    }
  });
  surface.onKey(event => {
    if (live() && !event.ctrl && !event.meta && !event.shift && (event.key === 'return' || event.key === ' ')) {
      surface.post({ action: 'tap', lease: props.lease, session: props.session, x: 0.5 });
    }
  });
  const current = surface.state || initial;
  if (current.expired || now >= current.deadline || surface.columns < props.columns || surface.rows < props.rows) {
    return Box({ children: [] });
  }
  const data = words(props.cells);
  const offset = Math.round((Math.min(72, surface.columns) - props.columns) * props.position);
  const rows = [];
  for (let y = 0; y < props.rows; y++) {
    const runs = [];
    for (let x = 0; x < props.columns; x++) {
      const i = (y * props.columns + x) * 3;
      const [cp, fg, bg] = data.slice(i, i + 3);
      // This channel carries only the existing half-cell renderer's glyphs.
      if (!((cp >= 32 && cp <= 126) || [0x2580, 0x2584, 0x2588].includes(cp)) || fg > 0x1000000 || bg > 0x1000000) return Box({ children: [] });
      const last = runs[runs.length - 1];
      if (last && last.fg === fg && last.bg === bg) last.text += String.fromCharCode(cp);
      else runs.push({ text: String.fromCharCode(cp), fg, bg });
    }
    rows.push(Box({ key: 'row-' + y, flexDirection: 'row', children: runs.map((run, i) => Text({
      key: 'cell-' + y + '-' + i, ...(run.fg < 0x1000000 ? { color: '#' + run.fg.toString(16).padStart(6, '0') } : {}),
      ...(run.bg < 0x1000000 ? { backgroundColor: '#' + run.bg.toString(16).padStart(6, '0') } : {}),
      children: [run.text],
    })) }));
  }
  const tree = Box({ key: 'pet-pixels', width: props.columns + offset, paddingLeft: offset,
    flexDirection: 'column', children: rows });
  // Valid raster dimensions can still exceed the Client's serialized tree limit.
  if (JSON.stringify(tree).length > 90000) {
    if (surface.state.rejectedCells !== props.cells) {
      surface.setState({ ...surface.state, rejectedCells: props.cells });
      surface.post({ action: 'raster-fallback', lease: props.lease, session: props.session });
    }
    return Box({ children: [] });
  }
  return tree;
}
