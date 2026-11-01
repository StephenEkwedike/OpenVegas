// Uses only host-owned Image/Box/Text elements and the bundled public artwork.
// It observes the already-sanitized form. No bets, network, keys or TTY writes.
// Keep the shared native-terminal pacing in roulette_renderer.py in sync.
const ROULETTE_MOTION = {durationMs: 8000, turns: 1};
function rouletteMotionPosition(elapsed, target, start = 0) {
  const t = Math.max(0, Math.min(1, elapsed / ROULETTE_MOTION.durationMs));
  return start + (37 * ROULETTE_MOTION.turns + (target-start+37)%37) * t * (2-t);
}
function createGameGraphics() {
  let text = null, timer = null, started = null, lastFrame = -1;
  let denied = false, view = null, busy = false, generation = 0;
  const reset = () => {
    timer?.cancel(); timer = null; text = null; started = null;
    denied = false; view = null; busy = false; lastFrame = -1; generation++;
  };
  const parse = value => {
    if (typeof value !== 'string' || value.length > 6000 ||
        /[\x00-\x08\x0b-\x1f\x7f-\x9f]/.test(value)) return null;
    const lines = value.split('\n');
    const game = /^OPENVEGAS \/ (POKER|BLACKJACK|BACCARAT|ROULETTE|SLOTS)$/.exec(lines[0])?.[1];
    if (!game) return null;
    const result = {game, settled: lines[1] === 'Round settled', hands: [],
      notes: lines.filter(line => /^(Payout:|Net:|Outcome:|Bet:|Selected number:|Wheel \/ landed:)/.test(line))};
    let hand = null;
    for (const line of lines) {
      if (/^(Dealer|Player|Banker|Community)(: waiting for deal)?$/.test(line)) {
        hand = {name: line.split(':')[0], cards: []}; result.hands.push(hand);
      } else if (hand) {
        for (const match of line.matchAll(/│\s*((?:10|[2-9AJQK])[♠♥♦♣]|\?\s*\?)\s*│/g)) {
          const value = match[1];
          hand.cards.push(value.startsWith('?') ? '?' : value.slice(0,-1) + ({'♠':'S','♥':'H','♦':'D','♣':'C'}[value.at(-1)]));
        }
      }
    }
    result.number = /^Wheel \/ landed: (\d{2})[RGB]$/m.exec(value)?.[1];
    if (result.number !== undefined) result.number = Number(result.number);
    if (result.number > 36) return null;
    result.reels = /\|\s*(7|BAR|CHERRY|LEMON|BELL|STAR|\?)\s*\|\s*(7|BAR|CHERRY|LEMON|BELL|STAR|\?)\s*\|\s*(7|BAR|CHERRY|LEMON|BELL|STAR|\?)\s*\|/.exec(value)?.slice(1);
    return result;
  };
  const stop = () => {timer?.cancel(); timer = null;};

  function draw(api, value, elements, columns, rows, paused) {
    const {Box, Text, Image} = elements;
    if (text !== value) {reset(); text = value;}
    if (denied || !Image || columns < 44 || rows < 12) {stop(); return null;}
    const model = parse(value);
    if (!model) {stop(); return null;}
    const cardGame = model.hands.length > 0;
    const rowCount = cardGame ? model.hands.reduce((n, hand) => n + Math.max(1, Math.ceil(hand.cards.length / 6)), 0) : 0;
    // Never crop a hand or suppress cards to fit a small pane.
    const compactPoker = model.game === 'POKER' && model.hands.length === 3 &&
      model.hands.every(hand => hand.cards.length <= (hand.name === 'Community' ? 5 : 2)) && columns >= 56;
    const requiredRows = (compactPoker ? 14 : cardGame ? rowCount*7 : model.game === 'ROULETTE' ? 14 : 9) + 3;
    if (rows < requiredRows) {stop(); return null;}
    if (started === null) started = performance.now();
    const duration = model.game === 'ROULETTE' ? ROULETTE_MOTION.durationMs : cardGame ? 200 : 720;
    const endFrame = duration/40;
    const elapsed = paused ? duration : performance.now()-started;
    const frame = Math.min(endFrame, Math.floor(elapsed/40));
    const spin = !paused && frame < endFrame && model.settled && ['ROULETTE','SLOTS'].includes(model.game);
    const dealing = !paused && cardGame && frame < 5;
    const leaves = [];
    const picture = (key, png, width, height, alt) => {
      const leaf = {key, source: {png}, columns: width, rows: height, alt};
      leaves.push(leaf); return Image(leaf);
    };
    const label = (key, content) => Text({key, color: '#fffdf7', backgroundColor: '#153a35', children: [content]});
    const hand = h => {
      const children = [label('label-'+h.name, h.name)];
      if (!h.cards.length) children.push(label('empty-'+h.name, 'Waiting for deal'));
      for (let start=0; start<h.cards.length; start+=6) children.push(Box({key: h.name+start,
        flexDirection: 'row', columnGap: 1, children: h.cards.slice(start,start+6).map((card,i) =>
          picture('card-'+h.name+'-'+(start+i),
            (dealing && GAME_ART.cardDeal[card]?.[Math.min(2, Math.floor(frame*3/5))]) || GAME_ART.cards[card] || GAME_ART.cards['?'], 8, 6,
            card === '?' ? 'Face-down card' : card))}));
      return Box({key: 'hand-'+h.name, flexDirection: 'column', children});
    };
    const children = [label('table-title', 'OPENVEGAS / '+model.game)];
    if (cardGame) {
      if (compactPoker) {
        children.push(Box({flexDirection: 'row', columnGap: 4,
          children: [hand(model.hands.find(h => h.name==='Dealer')), hand(model.hands.find(h => h.name==='Player'))]}));
        children.push(hand(model.hands.find(h => h.name==='Community')));
      } else children.push(...model.hands.map(hand));
    } else if (model.game === 'ROULETTE') {
      const final = model.number === undefined ? GAME_ART.wheel : GAME_ART.wheelLanded[String(model.number)];
      // The outcome remains visible throughout the interruptible coast.
      // It is never sampled from the animation or selected on the client.
      const pocket = model.number === undefined ? 0 : GAME_ART.wheelOrder.indexOf(model.number);
      const replayPocket = GAME_ART.wheelOrder[Math.floor(rouletteMotionPosition(elapsed, pocket))%37];
      const png = spin && model.number !== undefined ? GAME_ART.wheelLanded[String(replayPocket)] : final;
      children.push(Box({flexDirection: 'row', columnGap: 2, children: [
        picture('wheel', png, 28, 14, model.number === undefined ? 'Roulette wheel: awaiting your spin' : 'Landed: '+model.number),
        Box({flexDirection:'column', children: [label('wheel-kind', 'European / single zero'),
          label('wheel-state', model.number === undefined ? 'Awaiting your spin' : 'Landed: '+model.number),
          label('wheel-red', 'Red / black / green'), label('wheel-bet', 'Choose in the form below')]}),
      ]}));
    } else {
      const reels = model.reels || ['?','?','?'];
      children.push(Box({flexDirection: 'row', columnGap: 1, children: reels.map((reel,i) => {
        const active = spin && frame < 10+i*3 && reel !== '?';
        const symbol = active ? GAME_ART.symbols[(frame+i*2)%GAME_ART.symbols.length] : reel;
        return picture('reel-'+i, GAME_ART.reels[symbol], 10, 7, 'Reel '+(i+1)+': '+reel);
      })}));
    }
    children.push(label('table-motion', spin ? 'Result replay / controls remain live' : model.settled ? 'Round settled' : 'Your move'));
    // Winnings remain in the authoritative native form, not decorative imagery.
    view = {leaves, model, paused, elements, columns, rows};
    const lease = generation;
    if (!timer && !paused && lastFrame < endFrame) timer = api.every(40, async () => {
      if (lease !== generation || !view || busy) return;
      const index = Math.min(endFrame, Math.floor((performance.now()-started)/40));
      if (index === lastFrame) return;
      lastFrame = index; busy = true;
      try {
        // A denied Image blit means this surface cannot paint pictures. Fall
        // back to the readable table instead of leaving an empty image box.
        const current = view;
        draw(api, value, current.elements, current.columns, current.rows, current.paused);
        for (const leaf of view.leaves) {
          const {key, source, columns, rows} = leaf;
          const result = await api.blit({requestId:'openvegas', key, source, columns, rows});
          if (lease !== generation) return;
          if (result?.deny) {denied = true; stop(); api.invalidate(); return;}
        }
        if (index >= endFrame || (!spin && !dealing) || (cardGame && index >= 5)) {
          lastFrame = endFrame; stop(); api.invalidate();
        }
      } catch {
        if (lease === generation) {denied = true; stop(); api.invalidate();}
      } finally {if (lease === generation) busy = false;}
    });
    if (paused) {stop(); lastFrame = endFrame;}
    return Box({key: 'game-graphics', backgroundColor:'#153a35', flexDirection:'column', children});
  }
  return {draw, reset, parse};
}
