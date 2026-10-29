// Host-owned cells, not terminal writes. Palette matches home-clarity.css.
export default function GameTable(props, surface) {
  const { Box, Text } = surface.elements;
  if (typeof props.text !== 'string' || props.text.length > 6000 ||
      !/^OPENVEGAS \/ (POKER|BLACKJACK|BACCARAT|ROULETTE|SLOTS)\n/.test(props.text) ||
      /[\x00-\x08\x0b-\x1f\x7f-\x9f]/.test(props.text)) return Box({ children: [] });
  const key = props.text + ':' + props.reducedMotion;
  const now = performance.now();
  if (surface.state?.key !== key) {
    surface.setState({ key, started: now, tick: now });
    if (!props.reducedMotion) {
      const stop = surface.every(40, () => {
        if (surface.state?.key !== key) { stop(); return; }
        const tick = performance.now();
        surface.setState({ ...surface.state, tick });
        if (tick - surface.state.started >= 320) stop();
      });
    }
  }
  const elapsed = props.reducedMotion ? 320 : now - (surface.state?.started ?? now);
  // A small deal-in, not delayed disclosure: all real cards are readable on
  // frame one. No fake reel results or simulated roulette outcomes.
  const offset = Math.max(0, Math.round(2 * (1 - Math.min(elapsed / 320, 1)) ** 3));
  const width = Math.max(1, Math.min(surface.columns, 54));
  const rows = props.text.split('\n').slice(0, 48).map((raw, row) => {
    const line = raw.slice(0, width);
    const pieces = [];
    const re = /[┌│└][─ ?A-Z0-9♠♥♦♣]+[┐│┘]|\b\d{2}[RGB]\b/g;
    let at = 0;
    for (const match of line.matchAll(re)) {
      if (match.index > at) pieces.push(Text({ color: '#fffdf7', backgroundColor: '#153a35', children: [line.slice(at, match.index)] }));
      const value = match[0];
      const red = /[♥♦]|R$/.test(value);
      pieces.push(Text({ color: red ? '#a2382e' : '#153a35', backgroundColor: '#fffdf7',
        bold: true, children: [value] }));
      at = match.index + value.length;
    }
    if (at < line.length) pieces.push(Text({ color: '#fffdf7', backgroundColor: '#153a35', children: [line.slice(at)] }));
    return Box({ key: 'table-row-' + row, flexDirection: 'row',
      paddingLeft: width >= 40 && row > 1 ? offset : 0, children: pieces });
  });
  return Box({ backgroundColor: '#153a35', flexDirection: 'column', children: rows });
}
