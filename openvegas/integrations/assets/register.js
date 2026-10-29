// Only non-secret helper argv is bundled. Equipped pixels stay in session memory.
const HELPER = __OPENVEGAS_HELPER__;
const SITE = __OPENVEGAS_SITE__;
const CONTROLS = __OPENVEGAS_CONTROLS__;
const PANE = 'openvegas';
const THEMES = {
  felt: { label: 'Felt', color: '#f5f5f5', accent: '#98c8ff', muted: '#bdbdbd', backgroundColor: '#181b1a' },
  midnight: { label: 'Midnight', color: '#f5f5f5', accent: '#98c8ff', muted: '#bdbdbd', backgroundColor: '#181a1e' },
  ruby: { label: 'Ruby', color: '#f5f5f5', accent: '#98c8ff', muted: '#bdbdbd', backgroundColor: '#1d191b' },
};
// Existing guided-game choices; the human-casino API is not a browser play page.
const GAMES = [
  ['blackjack', 'Blackjack'], ['roulette', 'Roulette'], ['slots', 'Slots'],
  ['poker', 'Poker'], ['baccarat', 'Baccarat'], ['horse', 'Horse'], ['skillshot', 'Skillshot'],
];
const GAME_MARKS = ['21', '00', '777', 'A/K', 'P/B', 'RUN', 'AIM'];
let theme = 'felt';
let gameRequestPending = false;
let gameTable = null;
let gameTableFailed = false;
const gameGraphics = typeof createGameGraphics === 'function' ? createGameGraphics() : null;

function gameServer(name) {
  return ['openvegas', 'plugin:openvegas:openvegas', 'plugin_openvegas_openvegas'].includes(name);
}

function money(value) {
  if (!/^-?\d{1,15}(\.\d{1,6})?$/.test(String(value))) return 'unavailable';
  const negative = String(value).startsWith('-');
  const [whole, fraction = ''] = String(value).replace(/^-/, '').split('.');
  const cents = BigInt(whole) * 100n + BigInt((fraction + '00').slice(0, 2)) +
    (Number(fraction[2] || 0) >= 5 ? 1n : 0n);
  return '$V ' + (negative && cents ? '-' : '') +
    (cents / 100n).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ',') + '.' +
    (cents % 100n).toString().padStart(2, '0');
}

function gameReply(result) {
  const text = typeof result?.text === 'string' ? result.text :
    Array.isArray(result?.content) ? result.content.filter(c => c.type === 'text').map(c => c.text).join('\n') : '';
  // Older MCP processes may still send JSON until the host is restarted.
  if (text.length <= 64000 && text.trim().startsWith('{')) {
    try {
      const data = JSON.parse(text);
      if (data.grant) {
        const g = data.grant;
        const keys = ['chips', 'matches_entered', 'max_matches', 'spent_entry_chips', 'entry_budget'];
        if (g.unit !== 'test_chips' || g.redeemable !== false ||
            !keys.every(key => Number.isSafeInteger(g[key]) && g[key] >= 0)) {
          return 'Arena status unavailable. No $V or billing change.';
        }
        const status = data.blocked ? 'Stopped / refresh your grant status' :
          data.observation?.resolved ? 'Hand finished' :
          data.observation?.status === 'waiting' ? 'Waiting for another agent' :
          data.observation ? 'Hand active' : 'Ready';
        return 'OPENVEGAS ARENA / ' + status + '\n' + g.chips + ' test chips per match | ' +
          g.matches_entered + '/' + g.max_matches + ' entries | ' +
          Math.max(0, g.entry_budget - g.spent_entry_chips) + ' entry chips remaining\n' +
          'Ask your agent to use Arena tools to participate. Test chips only; $V and host billing unchanged.';
      }
      if (data.status === 'resolved') {
        return 'Round settled.\nPayout: ' + money(data.payout_v) + ' | Net: ' + money(data.net_v) + '\nBack to coding. No new wager placed.';
      }
      if (typeof data.message === 'string') return display(data.message).slice(0, 6000);
    } catch { /* Do not dump malformed structured data into the transcript. */ }
    return 'Game request finished. Check wallet history for the result; no automatic retry.';
  }
  return text ? text.replace(/[\x00-\x08\x0b-\x1f\x7f-\x9f]/g, '').slice(0, 6000) :
    'OpenVegas game request finished. Back to coding; no automatic next round.';
}
let panel = null;
let expanded = false;
let selectedGame = null;
let state = null;
let accountId = null;
let paused = false;
let art = null;
let reviewArt = null;
let reviewEpoch = 0;
function selectedArt() { return reviewArt || art; }
let expires = 0;
let issued = 0;
let playback = null;
let activeTurn = null;
let activity = 'idle';
let pendingTools = 0;
let pendingQuestions = 0;
let seen = new Set();
let session = 0;
let closed = true;
let timer = null;
let poll = null;
let initialRefresh = null;
let refreshing = null;
let lastPicture = '';
let openedArtPane = false;
let demo = null;
let hidden = false;
let interaction = null;
let artLease = 0;
let petView = null;
let failedPetFrame = null;
let sparklesEnabled = true;
let sparkleUntil = 0;

function sparkleText() {
  if (closed || paused || hidden || !sparklesEnabled || performance.now() >= sparkleUntil) return '';
  const frames = ['.   +     .', '  *   .    ', ' .    +   *', '    .   *  '];
  return frames[Math.floor(performance.now() / 180) % frames.length];
}

function dispatchAlive(epoch, signal) {
  return !closed && session === epoch && !signal?.aborted;
}

function resetActivity(value = 'idle') {
  sparkleUntil = 0;
  activeTurn = null; pendingTools = pendingQuestions = 0; activity = value;
}

function updateActivity($) {
  if (!activeTurn) return;
  activity = pendingQuestions ? 'waiting_input' : pendingTools ? 'waiting_output' : 'running';
  $.ui.invalidate('ui.render');
}

function activityLabel() {
  return { idle: 'Idle', running: 'Working / thinking', waiting_output: 'Waiting for tool output',
    waiting_input: 'Needs your input', review: 'Ready for review', stopped: 'Stopped', failed: 'Failed' }[activity];
}

function interact($, fraction) {
  if (closed || hidden || paused || activeTurn || demo !== null || playback || panel || expanded ||
      !fresh($) || !selectedArt().slots.companion || !Number.isFinite(fraction)) return;
  interaction = { started: performance.now(), from: interactionPosition(), to: Math.max(0, Math.min(1, fraction)) };
  $.ui.invalidate('ui.render');
}

function interactionPosition() {
  if (!interaction) return 0.5;
  const elapsed = performance.now() - interaction.started;
  const returning = elapsed > 1600;
  const t = Math.max(0, Math.min(1, (returning ? elapsed - 1600 : elapsed) / 700));
  const from = returning ? interaction.to : interaction.from;
  return from + ((returning ? 0.5 : interaction.to) - from) * (t * t * (3 - 2 * t));
}

function petPaneSize() {
  // A user-opened pet pane needs room for the actual leased art, not one third
  // of a terminal by default. The host still honors the user's own resize.
  const assets = selectedArt() ? Object.values(selectedArt().slots).filter(Boolean) : [];
  return assets.length ? { rows: Math.max(...assets.map(asset => asset.rows)) + 11, columns: 72 } : {};
}

function clearArt($) {
  art = null; expires = issued = 0;
  if (!reviewArt) { playback = null; demo = null; interaction = null; petView = null; }
  failedPetFrame = null;
  $.ui.invalidate('ui.render');
}

function fresh($) {
  const now = performance.now();
  if (art && (!Number.isFinite(now) || now < issued || now >= expires)) clearArt($);
  return !closed && !!selectedArt();
}

function validateArt(value, localReview = false) {
  if (!value || value.schema_version !== 1 || value.available !== true ||
      value.presentation?.mode !== 'claude_raster' || typeof value.account_id !== 'string' ||
      !Number.isFinite(value.ttl_ms) || value.ttl_ms <= 0 || value.ttl_ms > 30000 ||
      !value.slots || Object.keys(value.slots).sort().join(',') !== 'companion,completion') throw Error();
  if (localReview && (value.review_id !== 'q50' || value.account_id !== 'local-artwork-review')) throw Error();
  for (const slot of ['companion', 'completion']) {
    const asset = value.slots[slot];
    if (asset === null) continue;
    if (localReview && (asset.pack_id !== 'openvegas.q50-jewelry-motion' || asset.item_id !== 'local-review-q50')) throw Error();
    if (!asset || asset.access !== (localReview ? 'local_artwork_review' : 'entitlement_verified') || asset.slot !== slot ||
        !['item_id', 'pack_id', 'version', 'display_name'].every(key =>
          typeof asset[key] === 'string' && /^[\x20-\x7e]{1,128}$/.test(asset[key])) ||
        !Number.isInteger(asset.columns) || asset.columns < 1 || asset.columns > 67 ||
        !Number.isInteger(asset.rows) || asset.rows < 1 || asset.rows > 34 ||
        !Array.isArray(asset.frames) || !asset.frames.length || asset.frames.length > 128 ||
        !asset.frames.every(frame => typeof frame === 'string' &&
          frame.length === asset.columns * asset.rows * 16 && /^[A-Za-z0-9+/]+$/.test(frame)) ||
        !Number.isInteger(asset.reduced_motion_frame) || !asset.frames[asset.reduced_motion_frame]) throw Error();
    for (const name of slot === 'companion' ? ['idle', 'waiting'] : ['complete']) {
      const clip = asset.clips?.[name];
      if (!clip || !Array.isArray(clip.frames) || !clip.frames.length || clip.frames.length > 4096 ||
          !clip.frames.every(index => Number.isInteger(index) && index >= 0 && index < asset.frames.length) ||
          !Number.isFinite(clip.frame_ms) || clip.frame_ms < 1 || clip.frame_ms > 60000 ||
          !Number.isFinite(clip.duration_ms) || clip.duration_ms !== clip.frame_ms * clip.frames.length ||
          clip.duration_ms > 300000 || (name === 'complete' && (clip.duration_ms < 4000 || clip.duration_ms > 7000))) throw Error();
    }
  }
  return value;
}

function refresh($) {
  if (refreshing || closed) return refreshing;
  const epoch = session;
  const task = (async () => {
    let walletReceived = false;
    try {
      const result = await $.process.run([...HELPER, '--action', 'state'], { timeoutMs: 3000 });
      if (closed || session !== epoch) return;
      const wallet = result.exitCode === 0 && result.stdout.length <= 524288 ? JSON.parse(result.stdout) : null;
      if (!wallet || wallet.available === false) throw Error();
      if (reviewArt) { accountId = wallet.user_id; state = wallet; return; }
      if ((accountId && accountId !== wallet.user_id) || (art && art.account_id !== wallet.user_id)) {
        // Never display another account's art while the new lease is pending.
        resetActivity(); clearArt($);
      }
      accountId = wallet.user_id;
      state = wallet;
      walletReceived = true;
      const started = performance.now();
      const pixels = await $.process.run([...HELPER, '--action', 'cosmetics'], { timeoutMs: 10000 });
      if (closed || session !== epoch) return;
      if (reviewArt) return;
      if (pixels.exitCode !== 0 || pixels.stdout.length > 8388608) throw Error();
      const next = validateArt(JSON.parse(pixels.stdout));
      if (next.account_id !== state.user_id || performance.now() >= started + next.ttl_ms) throw Error();
      const changed = !art || art.account_id !== next.account_id || JSON.stringify(art.slots) !== JSON.stringify(next.slots);
      if (changed) {
        playback = null; demo = null; interaction = null;
        if (art && art.account_id !== next.account_id) resetActivity();
      }
      art = next; issued = started; expires = started + next.ttl_ms; artLease++;
      if (changed && activeTurn) play($, 'companion', 'waiting');
      if (!hidden && !openedArtPane && Object.values(art.slots).some(Boolean)) {
        // The host owns layout/scrolling. Do not take the user's typing focus.
        await $.ui.open({ id: PANE, title: 'OpenVegas', closeOnEscape: true });
        if (!dispatchAlive(epoch)) return;
        openedArtPane = true;
      }
    } catch {
      if (session === epoch && !closed) {
        if (!walletReceived) state = null;
        clearArt($);
      }
    } finally {
      if (session === epoch) { refreshing = null; $.ui.invalidate('ui.render'); }
    }
  })();
  refreshing = task;
  return task;
}

function balance() {
  return state && state.balance !== undefined ? money(state.balance) : 'wallet unavailable';
}

function billing() {
  if (!state) return 'billing unavailable';
  if (state?.billing_mode === 'openvegas_v') {
    return '$V API billing | ' + money(state.spending?.used_or_reserved_v) + '/' +
      money(state.spending?.budget_v) + ' used/reserved';
  }
  if (state?.billing_mode === 'openvegas_v_unavailable') return '$V session unavailable; no billing fallback';
  return 'host subscription/API billing';
}

function display(value) {
  return String(value).replace(/[^\x20-\x7e]/g, '').slice(0, 128);
}

function shortBilling() {
  if (!state) return 'billing unavailable';
  if (state.billing_mode === 'openvegas_v') return '$V API billing';
  if (state.billing_mode === 'openvegas_v_unavailable') return '$V unavailable; no fallback';
  return 'host billing';
}

function selectPanel($, value) {
  panel = panel === value ? null : value;
  $.ui.invalidate('ui.render');
}

function picture($) {
  if (!fresh($)) return null;
  if (hidden) return null;
  const now = performance.now();
  const art = selectedArt();
  // Waiting for a person is not ongoing computation; leave a calm, still pose.
  if (activeTurn && activity === 'waiting_input') {
    const asset = art.slots.companion;
    return asset ? { asset, slot: 'companion', index: asset.reduced_motion_frame } : null;
  }
  if (interaction && (paused || activeTurn || now < interaction.started || now >= interaction.started + 2400)) interaction = null;
  if (interaction) {
    const asset = art.slots.companion;
    const clip = asset.clips.waiting;
    return { asset, slot: 'companion', index: clip.frames[Math.floor((now - interaction.started) / clip.frame_ms) % clip.frames.length],
      position: interactionPosition() };
  }
  if (demo !== null) {
    const elapsed = now - demo;
    if (paused || activeTurn || elapsed < 0 || elapsed >= 3360 + art.slots.completion.clips.complete.duration_ms) demo = null;
    else {
      const slot = elapsed < 3360 ? 'companion' : 'completion';
      const asset = art.slots[slot];
      const clip = asset.clips[slot === 'companion' ? 'waiting' : 'complete'];
      const tick = Math.floor((elapsed < 3360 ? elapsed : elapsed - 3360) / clip.frame_ms);
      return { asset, slot, index: clip.frames[tick % clip.frames.length] };
    }
  }
  if (playback && (paused || now < playback.started ||
      (playback.loop ? !activeTurn : now >= playback.until))) playback = null;
  const slot = playback ? playback.slot : 'companion';
  const asset = art.slots[slot];
  if (!asset) return null;
  let index = paused ? asset.reduced_motion_frame : asset.clips.idle?.frames[0];
  if (playback) {
    const clip = asset.clips[playback.clip];
    const elapsed = Math.floor((now - playback.started) / clip.frame_ms);
    index = clip.frames[playback.loop ? elapsed % clip.frames.length : Math.min(clip.frames.length - 1, elapsed)];
  }
  return { asset, index, slot };
}

function play($, slot, clip) {
  playback = null;
  const art = selectedArt();
  if (!paused && fresh($) && art.slots[slot]) {
    const now = performance.now();
    const duration = Math.min(10000, art.slots[slot].clips[clip].duration_ms);
    playback = { slot, clip, started: now, until: reviewArt ? now + duration : Math.min(now + duration, expires),
      loop: slot === 'companion' && clip === 'waiting' && !!activeTurn };
  }
  $.ui.invalidate('ui.render');
}

function nativeEvent(next) {
  return next.origin?.plugin === 'engine' && next.origin?.tier === 'core';
}

function stopClocks() {
  for (const clock of [timer, poll, initialRefresh]) clock?.cancel();
  timer = poll = initialRefresh = null;
}

function startClocks($) {
  stopClocks();
  timer = $.clock.every(50, () => {
    const current = picture($);
    const signature = (current ? current.slot + ':' + current.asset.item_id + ':' + current.index + ':' +
      Math.round((current.position ?? 0.5) * 72) : '') + ':' + sparkleText();
    if (signature !== lastPicture) { lastPicture = signature; $.ui.invalidate('ui.render'); }
  });
  poll = $.clock.every(10000, () => {
    // Local studies have no ownership lease; renew only their client-view lifetime.
    if (reviewArt) { artLease++; $.ui.invalidate('ui.render'); }
    return refresh($);
  });
  initialRefresh = $.clock.after(0, () => refresh($));
}

async function togglePause($) {
  sparkleUntil = 0;
  paused = !paused; playback = null; demo = null; interaction = null;
  if (!paused && activeTurn) play($, 'companion', 'waiting');
  await $.store.set('paused', paused);
  $.ui.invalidate('ui.render');
}

async function startDemo($, signal) {
  const epoch = session;
  if (!dispatchAlive(epoch, signal)) return {};
  await refresh($);
  if (!dispatchAlive(epoch, signal)) return {};
  if (activeTurn) return { text: 'A Claude turn is active. Wait for it to finish before testing animation.' };
  if (paused) return { text: 'Animation is paused. Use /openvegas pause to enable motion, then /openvegas demo.' };
  if (!fresh($) || !selectedArt().slots.companion || !selectedArt().slots.completion) return {
    text: 'Equip an owned companion and completion first. No preview bypass or purchase was made.',
  };
  // This visual test never emits host lifecycle events, inference or rewards.
  hidden = false; await $.store.set('hidden', false);
  if (!dispatchAlive(epoch, signal)) return {};
  panel = null; expanded = false;
  playback = null; interaction = null; demo = performance.now();
  await $.ui.open({ id: PANE, title: 'OpenVegas', closeOnEscape: true, ...petPaneSize() });
  if (!dispatchAlive(epoch, signal)) return {};
  $.ui.invalidate('ui.render');
  return { text: 'Animation test: dance, then one completion, then idle. No AI request or credits spent.' };
}

export function register(on) {
  on('session.start', async ($, e, next) => {
    gameGraphics?.reset();
    gameTable = null; gameTableFailed = false; gameRequestPending = false;
    reviewArt = null; reviewEpoch++;
    session++; closed = false; state = null; accountId = null; resetActivity(); seen = new Set();
    const epoch = session;
    stopClocks();
    refreshing = null; clearArt($); lastPicture = '';
    openedArtPane = false;
    panel = null; expanded = false; selectedGame = null;
    const savedTheme = await $.store.get('hudTheme');
    if (!dispatchAlive(epoch, next.signal)) return next(e);
    theme = typeof savedTheme === 'string' && Object.hasOwn(THEMES, savedTheme) ? savedTheme : 'felt';
    const savedPaused = await $.store.get('paused');
    if (!dispatchAlive(epoch, next.signal)) return next(e);
    paused = savedPaused === true;
    const savedHidden = await $.store.get('hidden');
    if (!dispatchAlive(epoch, next.signal)) return next(e);
    hidden = savedHidden === true;
    sparklesEnabled = (await $.store.get('sparklesEnabled')) !== false;
    if (!dispatchAlive(epoch, next.signal)) return next(e);
    startClocks($);
    await $.command.register({ name: 'openvegas', description: 'OpenVegas controls and animations',
      argumentHint: '[' + CONTROLS.actions.map(action => action.name).join('|') + '|demo|pause]', immediate: true });
    await $.command.register({ name: 'pets', description: 'Open or hide OpenVegas animations',
      argumentHint: '[off]', immediate: true });
    return next(e);
  });
  on('turn.start', async ($, e, next) => {
    if (!closed && nativeEvent(next) && e.agentId === undefined && typeof e.turnId === 'string' && e.turnId.length <= 128 &&
        e.turnId && !seen.has(e.turnId) && seen.size < 10000) {
      resetActivity('running');
      seen.add(e.turnId); activeTurn = e.turnId; demo = null; interaction = null;
      play($, 'companion', 'waiting');
    }
    return next(e);
  });
  on('tool.call', async ($, e, next) => {
    if (closed || !nativeEvent(next) || e.agentId !== undefined || !activeTurn) return next(e);
    const epoch = session, turn = activeTurn;
    const question = e.tool === 'AskUserQuestion';
    pendingTools++; if (question) pendingQuestions++;
    updateActivity($);
    try {
      // Observe only the lifetime: never retain/rewrite arguments or tool results.
      return await next(e);
    } finally {
      if (dispatchAlive(epoch) && activeTurn === turn) {
        pendingTools--; if (question) pendingQuestions--;
        updateActivity($);
      }
    }
  });
  on('turn.complete', async ($, e, next) => {
    if (!closed && nativeEvent(next) && e.agentId === undefined && activeTurn && e.turnId === activeTurn) {
      const answered = e.reason === 'answer' && e.isAborted === false;
      resetActivity(answered ? 'review' : e.isAborted || e.reason === 'aborted' ? 'stopped' : e.reason === 'error' ? 'failed' : 'idle');
      // One roll per authenticated top-level answer, never per frame or tool result.
      if (answered && sparklesEnabled && !paused && !hidden && Math.random() < 0.17) {
        sparkleUntil = performance.now() + 5000;
      }
      playback = null;
      // An answer is only an observed host lifecycle, never verified task success.
      if (answered) play($, 'completion', 'complete');
      else $.ui.invalidate('ui.render');
    }
    return next(e);
  });
  on('session.end', async ($, e, next) => {
    if (!nativeEvent(next)) return next(e);
    gameGraphics?.reset();
    gameTable = null; gameRequestPending = false;
    reviewArt = null; reviewEpoch++;
    const reset = !closed && ['clear', 'resume'].includes(e.reason);
    closed = true; session++; resetActivity(); state = null; accountId = null; refreshing = null; clearArt($);
    const epoch = session;
    stopClocks();
    const result = await next(e);
    // /clear, /resume and /branch do not emit another session.start. Re-arm
    // only this completed conversation reset, never logout/exit or stale work.
    if (reset && closed && session === epoch && !next.signal?.aborted) {
      closed = false; seen = new Set(); lastPicture = '';
      panel = null; expanded = false; selectedGame = null;
      startClocks($);
      $.ui.invalidate('ui.render');
    }
    return result;
  });
  on('classic.Elicitation', async ($, e, next) => {
    if (!closed && nativeEvent(next) && !e.agent_id && gameServer(e.mcp_server_name) && e.mode !== 'url') {
      gameGraphics?.reset();
      const text = typeof e.message === 'string' ? e.message.split('\nChoose a move,')[0]
        .split('\nDone or Esc')[0] : '';
      gameTable = text.length <= 6000 &&
        /^OPENVEGAS \/ (POKER|BLACKJACK|BACCARAT|ROULETTE|SLOTS)\n/.test(text) &&
        !/[\x00-\x08\x0b-\x1f\x7f-\x9f]/.test(text) ? text : null;
      if (gameTable) {
        // Open a supported pane without taking focus from the human's form.
        // This observer never supplies or changes a wager/action response.
        void $.ui.open({ id: PANE, title: 'OpenVegas', closeOnEscape: true, rows: 24, columns: 72 }).catch(() => {});
      }
      $.ui.invalidate('ui.render');
    }
    return next(e);
  });
  on('classic.ElicitationResult', async ($, e, next) => {
    if (nativeEvent(next) && !e.agent_id && gameServer(e.mcp_server_name)) {
      gameGraphics?.reset();
      gameTable = null;
      $.ui.invalidate('ui.render');
    }
    return next(e);
  });
  on('command.run', { command: 'openvegas' }, async ($, e, next) => {
    const epoch = session;
    const requested = (e.args || '').trim().toLowerCase();
    const action = CONTROLS.aliases[requested] || requested;
    if (action === 'use v' || action === 'use host') {
      if (activeTurn || gameRequestPending) return { text: 'Finish the active turn or game before switching billing.' };
      try {
        const result = await $.process.run([...HELPER, '--action', action === 'use v' ? 'use-v' : 'use-host'], { timeoutMs: 4000 });
        const value = JSON.parse(result.stdout);
        return { text: display(value.text || 'Billing unchanged.') };
      } catch { return { text: 'Billing switch unavailable in this session. No billing change or prompt sent.' }; }
    }
    if (action === 'pvp' || action === 'arena' || action === 'play' || /^play (blackjack|roulette|poker|baccarat|slots|horse)$/.test(action)) {
      if (!dispatchAlive(epoch, next?.signal) || activeTurn || gameRequestPending) return { text: 'Finish the active turn or game form before opening another game.' };
      gameRequestPending = true;
      resetActivity('waiting_input');
      $.ui.invalidate('ui.render');
      try {
        const verb = ['pvp', 'arena'].includes(action) ? action : 'play';
        const names = ['mcp__plugin_openvegas_openvegas__' + verb, 'mcp__openvegas__' + verb];
        // Bound discovery only. A tool call may be waiting for human wager input.
        let discoveryTimer;
        let tools;
        try {
          tools = await Promise.race([
            $.tool.list(),
            new Promise(resolve => { discoveryTimer = $.clock.after(5000, () => resolve(null)); }),
          ]);
        } finally { discoveryTimer?.cancel(); }
        if (!dispatchAlive(epoch, next?.signal) || activeTurn) return {};
        if (tools === null) return { text: 'OpenVegas game tool lookup timed out after 5 seconds. No game was started. Check /mcp and restart Claude if OpenVegas is disconnected; no automatic retry.' };
        const matches = tools.filter(tool => tool.mcp === true && names.includes(tool.name));
        if (matches.length !== 1) return { text: ['pvp', 'arena'].includes(action) ? 'This local test-chip mode is not configured. PvP requires a human seat; Arena requires an operator-issued agent grant. No $V was spent.' : 'OpenVegas play tool is missing or duplicated. Refresh the OpenVegas plugin and restart Claude; no wager was placed.' };
        // Native permission checks and elicitation remain in the host; no AI prompt.
        const game = action.split(' ')[1];
        const result = await $.tool.call({ tool: matches[0].name, ...(game ? { input: { game } } : {}) });
        if (!dispatchAlive(epoch, next?.signal)) return {};
        return { text: gameReply(result) };
      } catch {
        if (['pvp', 'arena'].includes(action)) return { text: 'Local multiplayer stopped. No automatic retry or $V charge; test-chip turn deadlines continue.' };
        return { text: 'OpenVegas play stopped. No automatic retry. If a wager was confirmed, use /openvegas play to review recovery; quitting does not refund it.' };
      } finally {
        if (dispatchAlive(epoch)) {
          gameRequestPending = false;
          if (!activeTurn) resetActivity();
          gameTable = null; gameGraphics?.reset(); $.ui.invalidate('ui.render');
        }
      }
    }
    if (['sparkles on', 'sparkles off', 'sparkles test'].includes(action)) {
      if (!dispatchAlive(epoch, next?.signal)) return {};
      if (action === 'sparkles test') {
        if (activeTurn || paused || hidden || !sparklesEnabled) return { text: 'Finish the turn and enable/resume the visible HUD before testing sparkles.' };
        sparkleUntil = performance.now() + 5000;
      } else {
        sparklesEnabled = action === 'sparkles on'; sparkleUntil = 0;
        await $.store.set('sparklesEnabled', sparklesEnabled);
        if (!dispatchAlive(epoch, next?.signal)) return {};
      }
      $.ui.invalidate('ui.render');
      return { text: action === 'sparkles test' ? 'Five-second HUD sparkle preview; no AI request or credits spent.' :
        'HUD sparkles ' + (sparklesEnabled ? 'enabled: 17% chance per completed answer.' : 'disabled.') };
    }
    if (action === 'preview off') {
      reviewArt = null; reviewEpoch++; clearArt($);
      await refresh($);
      return { text: 'Local artwork review stopped. Owned-cosmetic verification restored; billing unchanged.' };
    }
    if (action === 'preview q50') {
      if (!dispatchAlive(epoch, next?.signal) || activeTurn) return { text: 'Wait for the active turn to finish before selecting a local review.' };
      const request = ++reviewEpoch;
      try {
        const result = await $.process.run([...HELPER, '--action', 'review-q50'], { timeoutMs: 10000 });
        if (!dispatchAlive(epoch, next?.signal) || request !== reviewEpoch || activeTurn) return {};
        if (result.exitCode !== 0 || result.stdout.length > 8388608) throw Error();
        const candidate = validateArt(JSON.parse(result.stdout), true);
        if (!candidate.slots.companion || !candidate.slots.completion) throw Error();
        await $.ui.open({ id: PANE, title: 'OpenVegas - Local Q50 Review', closeOnEscape: true,
          rows: Math.max(...Object.values(candidate.slots).map(asset => asset.rows)) + 11, columns: 72 });
        if (!dispatchAlive(epoch, next?.signal) || request !== reviewEpoch || activeTurn) return {};
        reviewArt = candidate; artLease++; playback = null; demo = null; interaction = null;
        hidden = false; panel = null; expanded = false;
        $.ui.invalidate('ui.render');
        return { text: 'Q50 local artwork review selected, not purchased or owned. Use /openvegas demo to play; /openvegas preview off restores owned art. No AI request or credits spent.' };
      } catch { return { text: 'Q50 local review unavailable. Existing selection and billing unchanged.' }; }
    }
    if (action === 'help') return { text: CONTROLS.help + '\n/openvegas play: choose a game, then submit an amount to place one wager. No second confirmation; you can lose your stake.\n/openvegas sparkles on|off|test: five-second HUD shimmer, 17% per completed answer; does not replace native input/output.' };
    if (action && !['hud', 'games', 'demo', 'pause', ...CONTROLS.actions.map(item => item.name)].includes(action)) {
      return { text: 'Unknown OpenVegas action.\n' + CONTROLS.help };
    }
    if (action === 'demo') return startDemo($, next?.signal);
    if (action === 'status') return { text: JSON.stringify({
      wallet_available: !!(state && state.balance !== undefined),
      artwork_loaded: fresh($) && Object.values(selectedArt().slots).some(Boolean),
      local_review: reviewArt?.review_id || null,
      equipped: art ? Object.fromEntries(Object.entries(art.slots).map(([slot, asset]) => [slot, asset?.item_id || null])) : {},
      paused, hidden, sparkles_enabled: sparklesEnabled, sparkles_active: !!sparkleText(),
      hud_theme: theme, hud_panel: panel, hud_expanded: expanded,
      activity, activity_label: activityLabel(),
      animation_test: demo !== null, pet_interaction: interaction !== null, pet_client_failed: failedPetFrame !== null,
      automatic_rewards: false, native_verified: false,
    }) };
    if (!dispatchAlive(epoch, next?.signal)) return {};
    if (action === 'billing') {
      await refresh($);
      if (!dispatchAlive(epoch, next?.signal)) return {};
      if (!state) return { text: 'Billing information unavailable. No billing settings were changed.' };
      return { text: billing() + '. $V cannot refill subscription quota. ' + (state?.gateway_available ?
        'Use /openvegas use v for confirmed direct API billing, or /openvegas use host to switch back.' :
        'This backend has not enabled $V native inference.') + ' No billing settings were changed.',
      };
    }
    if (action === 'pause') {
      await togglePause($);
      return { text: paused ? 'OpenVegas animation paused; status tracking remains active.' :
        'OpenVegas motion enabled; idle stays still and active work animates.' };
    }
    await refresh($);
    if (!dispatchAlive(epoch, next?.signal)) return {};
    if (action === 'wallet') return { text: balance() + '. ' + billing() + '.' };
    if (action === 'cosmetics') return { text: JSON.stringify(state?.cosmetics || {
      available: false, message: 'Owned cosmetics unavailable. No empty inventory or ownership is inferred.',
    }) };
    hidden = false; await $.store.set('hidden', false);
    if (!dispatchAlive(epoch, next?.signal)) return {};
    panel = action === 'games' ? 'games' : null;
    expanded = false;
    await $.ui.open({ id: PANE, title: 'OpenVegas', focus: true, closeOnEscape: true, ...petPaneSize() });
    if (!dispatchAlive(epoch, next?.signal)) return {};
    $.ui.invalidate('ui.render');
    return {};
  });
  on('command.run', { command: 'pets' }, async ($, e, next) => {
    const epoch = session;
    if (!dispatchAlive(epoch, next?.signal)) return {};
    const action = (e.args || '').trim();
    if (action && action !== 'off') return { text: 'Use /pets to open the animation controls or /pets off to hide them. The pane has a no-AI animation test button.' };
    hidden = action === 'off';
    await $.store.set('hidden', hidden);
    if (!dispatchAlive(epoch, next?.signal)) return {};
    if (hidden) {
      sparkleUntil = 0;
      // Keep real work timing so every reveal path resumes the same turn.
      if (!activeTurn) playback = null;
      demo = null; interaction = null; petView = null;
      await $.ui.close({ id: PANE });
      if (!dispatchAlive(epoch, next?.signal)) return {};
      $.ui.invalidate('ui.render');
      return { text: 'OpenVegas animations hidden. /pets opens them again. Billing and equipment are unchanged.' };
    }
    await refresh($);
    if (!dispatchAlive(epoch, next?.signal)) return {};
    await $.ui.open({ id: PANE, title: 'OpenVegas', focus: true, closeOnEscape: true, ...petPaneSize() });
    if (!dispatchAlive(epoch, next?.signal)) return {};
    $.ui.invalidate('ui.render');
    return {};
  });
  on('ui.message', async ($, e, next) => {
    if (e.surface !== 'terminal' || e.component !== 'Pane' || e.requestId !== PANE || e.element !== 'pet-canvas') return next(e);
    const data = e.data;
    if (!petView || data?.lease !== artLease || data?.session !== session || !fresh($)) return {};
    if (data.action === 'raster-fallback') {
      failedPetFrame = petView.frame; interaction = null; petView = null;
      $.ui.invalidate('ui.render'); return {};
    }
    if (data.action !== 'tap' || !Number.isFinite(data?.x) || data.x < 0 || data.x > 1) return {};
    const room = petView.width - petView.columns;
    const position = room > 0 ? (data.x * petView.width - petView.columns / 2) / room : 0.5;
    interact($, position);
    return {};
  });
  on('ui.fault', async ($, e, next) => {
    if (e.requestId === PANE && e.element === 'game-table') {
      gameTableFailed = true; $.ui.invalidate('ui.render');
    }
    if (e.requestId === PANE && e.element === 'pet-canvas' && petView) {
      failedPetFrame = petView.frame; interaction = null; petView = null; $.ui.invalidate('ui.render');
    }
    return next(e);
  });
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const { Box, Text, Button } = $.ui.resolve(e);
    const other = await next(e);
    if (closed || e.props?.hasSurvey) return other;
    const columns = e.props?.bodyColumns || 40;
    const palette = THEMES[theme];
    const summary = (columns < 52 ? 'OV / ' : 'OPENVEGAS / ') + activityLabel() + ' / ' + balance() + ' / ' + shortBilling() +
      (paused ? ' | paused' : '');
    const shimmer = sparkleText();
    return Box({ flexDirection: 'column', children: [
      ...(other ? [other] : []),
      Box({ key: 'hud-band', flexDirection: 'row', columnGap: 1, children: [
        Box({ width: Math.max(1, columns - 8), children: [
          Text({ color: palette.color, backgroundColor: palette.backgroundColor,
            bold: true, wrap: 'truncate-end', children: [shimmer ? shimmer + '  ' + summary : summary] }),
        ] }),
        Button({ key: 'hud-open', label: 'HUD', onPress: async () => {
          const epoch = session;
          if (!dispatchAlive(epoch)) return;
          hidden = false; await $.store.set('hidden', false);
          if (!dispatchAlive(epoch)) return;
          await $.ui.open({ id: PANE, title: 'OpenVegas', focus: true, closeOnEscape: true, ...petPaneSize() });
          if (!dispatchAlive(epoch)) return;
          $.ui.invalidate('ui.render');
        } }),
      ] }),
    ] });
  });
  on('ui.render', { component: 'Pane' }, async ($, e, next) => {
    if (e.requestId !== PANE) return next(e);
    const elements = $.ui.resolve(e);
    const { Box, Text, Button, Link, Raster, Client } = elements;
    if (closed) return Box({ children: [] });
    const columns = e.props?.bodyColumns || 40;
    // New hosts report scroll.bodyRows; older builds exposed bodyRows directly.
    const rows = e.props?.scroll?.bodyRows ?? e.props?.bodyRows ?? 0;
    const narrow = columns < 40;
    const inset = columns >= 24 ? 1 : 0;
    const contentColumns = Math.max(1, columns - inset * 2);
    if (gameTable) {
      const graphics = !gameTableFailed && gameGraphics?.draw({
        every: (ms, fn) => $.clock.every(ms, fn), blit: args => $.ui.blit(args),
        invalidate: () => $.ui.invalidate('ui.render'),
      }, gameTable, elements, columns, rows, paused);
      if (graphics) return Box({flexDirection: 'column', children: [graphics,
        Text({children: ['Use the game form below; quit or Esc returns to coding.']})]});
      const height = Math.min(48, gameTable.split('\n').length);
      const board = Client && !gameTableFailed && columns >= 40 && rows >= height ?
        Client({ key: 'game-table', module: './game-table.js', width: Math.min(56, columns), height,
          props: { text: gameTable, reducedMotion: paused } }) :
        Text({ key: 'game-table-static', children: [gameTable] });
      return Box({ flexDirection: 'column', children: [board,
        Text({ children: ['Use the game form below; quit or Esc returns to coding.'] })] });
    }
    const palette = THEMES[theme];
    const row = (key, children) => Box({ key, flexDirection: narrow ? 'column' : 'row', columnGap: 1, children });
    // Pair custom ink with its surface; unstyled host buttons keep native contrast.
    const line = (key, text, props = {}) => Text({ key,
      ...(props.color ? { backgroundColor: palette.backgroundColor } : {}), ...props, children: [text] });
    const link = (key, path, label) => Link({ key, href: SITE + path, label });
    const button = (key, label, onPress) => Button({ key, label, onPress });
    const current = picture($);
    if (e.surface === 'terminal') petView = null;
    const pixels = [];
    if (e.surface === 'terminal' && Raster && current && contentColumns >= current.asset.columns &&
        rows >= current.asset.rows + (narrow ? 11 : 8) && !panel && !expanded) {
      const frame = artLease + ':' + current.slot + ':' + current.index;
      if (Client && failedPetFrame !== frame) {
        failedPetFrame = null;
        const width = Math.min(contentColumns, 72);
        petView = { width, columns: current.asset.columns, frame };
        pixels.push(Client({ key: 'pet-canvas', module: './pet.js', width, height: current.asset.rows,
          props: { columns: current.asset.columns, rows: current.asset.rows, cells: current.asset.frames[current.index],
            position: current.position ?? 0.5, lease: artLease, session, ttl: reviewArt ? 30000 : Math.max(0, expires - performance.now()),
            enabled: !paused && !activeTurn && demo === null && !playback && current.slot === 'companion' } }));
        pixels.push(line('pet-hint', paused ? 'Motion paused; status stays live.' : demo !== null ?
          'Explicit animation demo; not task status.' : activeTurn ? activityLabel() + '.' : playback ?
          'Ready for review; completion plays once.' : 'Click your emote or beside it to play. Esc returns to typing.', { color: palette.muted }));
      } else pixels.push(Raster({ key: 'equipped', columns: current.asset.columns,
        rows: current.asset.rows, cells: current.asset.frames[current.index] }));
    }
    const details = [];
    if (panel === 'games') {
      details.push(line('games-heading', 'GAMES', { bold: true, color: palette.color }),
        line('games-safety', 'Opening this lobby never wagers or spends.', { color: palette.muted }));
      for (let index = 0; index < GAMES.length; index += 2) {
        details.push(Box({ key: 'games-row-' + index,
          flexDirection: columns < 52 ? 'column' : 'row', columnGap: 1,
          children: GAMES.slice(index, index + 2).map(([id, label], offset) =>
          button('game-' + id, (selectedGame === id ? '> ' : '') +
            (narrow ? '' : GAME_MARKS[index + offset] + ' / ') + label, () => {
            selectedGame = id; $.ui.invalidate('ui.render');
          })) }));
      }
      const game = GAMES.find(([id]) => id === selectedGame);
      details.push(line('game-entry', selectedGame === 'skillshot' ?
        'Standalone only: openvegas ui > Play > ' + game[1] :
        'In this session: /openvegas play' + (game ? ' - choose ' + game[1] + ' in the menu.' : ' - choose a game.')),
        line('game-consent', 'Choose a game, enter an amount, then submit to place one wager. Wagers can lose $V. No AI prompt is needed for /openvegas play.'),
        link('game-guide', '/ui/how-to-play', 'Existing game guide'));
    } else if (panel === 'wallet') {
      details.push(line('wallet-heading', 'WALLET', { bold: true, color: palette.color }),
        line('wallet-billing', state ? billing() : 'Wallet and billing information unavailable.'),
        line('wallet-separation', '$V cannot refill subscription quota. Activity earns no credits.'),
        link('wallet-link', '/ui/balance', 'Wallet and history'));
    } else if (panel === 'cosmetics') {
      details.push(line('cosmetics-heading', 'YOUR COLLECTION', { bold: true, color: palette.color }));
      if (fresh($)) {
        for (const slot of ['companion', 'completion']) {
          details.push(line('owned-' + slot, slot + ': ' + (art?.slots[slot]?.display_name || 'none')));
        }
      } else details.push(line('owned-unavailable', 'Owned artwork unavailable; nothing rendered.'));
      details.push(link('cosmetics-link', '/ui/emotes', 'Cosmetics and shop'),
        line('cosmetics-consent', 'Use the skill to select an owned item; confirm changes. No automatic purchase.'));
    }
    if (expanded) {
      details.push(line('controls-heading', 'HUD SETTINGS', { bold: true, color: palette.color }),
        button('refresh', narrow ? 'Refresh' : 'Refresh wallet / owned art', () => refresh($)),
        button('demo', narrow ? 'Test (no AI)' : 'Test animation (no AI)', async () => {
          const epoch = session;
          const result = await startDemo($);
          if (dispatchAlive(epoch) && result.text) $.ui.toast(result.text);
        }),
        button('pause', narrow ? (paused ? 'Resume' : 'Pause') :
          (paused ? 'Resume animation' : 'Pause / reduced motion'), () => togglePause($)),
        line('theme-heading', 'Casino theme: ' + palette.label + ' (local appearance only)'),
        row('theme-choices', Object.entries(THEMES).map(([id, value]) => button('theme-' + id,
          (theme === id ? '> ' : '') + value.label, async () => {
            theme = id; $.ui.invalidate('ui.render'); await $.store.set('hudTheme', id);
          }))),
        line('animation-disclaimer', 'Answer animation is not verified task success. Activity earns no credits.'));
    }
    return Box({ key: 'hud-deck', flexDirection: 'column', paddingX: inset, children: [
      line('hud-title', contentColumns < 24 ? 'OPENVEGAS' : 'OPENVEGAS / PLAYER DECK', {
        bold: true, color: palette.color, backgroundColor: palette.backgroundColor, wrap: 'truncate-end',
      }),
      Box({ key: 'wallet-readout', flexDirection: 'row', columnGap: 1, children: [
        ...(contentColumns >= 22 ? [line('wallet-label', 'WALLET', { color: palette.muted })] : []),
        line('hud-wallet', balance(), { bold: true, color: palette.color, wrap: 'truncate-end' }),
      ] }),
      line('hud-billing', shortBilling() + ' | ' + activityLabel() + (paused ? ' | motion paused' : ''), { color: palette.muted }),
      row('hud-navigation', ['games', 'wallet', 'cosmetics'].map(name => button('nav-' + name,
        (panel === name ? '> ' : '') + name[0].toUpperCase() + name.slice(1), () => selectPanel($, name)))),
      button('hud-expand', expanded ? 'HUD settings -' : 'HUD settings +', () => {
        expanded = !expanded; $.ui.invalidate('ui.render');
      }),
      ...pixels,
      ...(reviewArt ? [line('review-state', 'Q50 local artwork review - not owned or for sale.')] : []),
      ...(demo !== null ? [line('demo-state', 'Animation test only; not a completed task.')] : []),
      ...(!panel && !expanded && !pixels.length ? [line('art-hint', current ?
        (e.surface === 'terminal' ? 'More room needed for owned artwork.' : 'Owned animation needs a terminal.') :
        'Owned artwork unavailable or hidden.', { color: palette.muted })] : []),
      ...(details.length ? [Box({ key: 'hud-details', flexDirection: 'column', borderStyle: 'quote',
        paddingLeft: 1, marginTop: 1, children: details })] : []),
    ] });
  });
}
