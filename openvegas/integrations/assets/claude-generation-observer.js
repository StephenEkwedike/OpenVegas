// Private supported observer. Billing is fixed by process startup, not a mod hook.
const BILLING = __OPENVEGAS_BILLING_ARGV__;
const VERSION = '2.1.322';

function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === 'object') return Object.fromEntries(
    Object.keys(value).sort().map(key => [key, canonical(value[key])]));
  return value;
}

async function digest(value) {
  const bytes = new TextEncoder().encode(JSON.stringify(canonical(value)));
  const hash = await crypto.subtle.digest('SHA-256', bytes);
  return Array.from(new Uint8Array(hash), b => b.toString(16).padStart(2, '0')).join('');
}

  let holding = false;
  let untransferable = false;
  let pending = [];
  let editRevision = 0;
  const putbacks = new Map();
  const delivered = new Set();
  const control = text => /^(?:\/|\$)?openvegas(?::openvegas)?[ \t]+use[ \t]+(v|host)$/i.exec(text.trim());
  async function release($) {
    holding = false;
    untransferable = false;
    // These entries NEVER called next(e) in the old engine. This is first
    // admission, not replay of a native queued/in-flight/provider request.
    for (const item of pending) {
      if (delivered.has(item.identifier)) continue;
      delivered.add(item.identifier); // An uncertain admission is never retried.
      await $.prompt.submit({ text: item.text, asUser: true });
    }
    pending = [];
  }
  async function billing($, target) {
    holding = true;
    try {
      const result = await $.process.run(BILLING, { timeoutMs: 4500, stdin: JSON.stringify({
        hook_event_name: 'UserPromptSubmit', prompt: `openvegas use ${target}`,
        session_id: await $.session.id(), model: await $.session.model() }) });
      const reply = JSON.parse(result.stdout);
      const message = String(reply.stopReason ?? reply.reason ?? 'Billing request sent.');
      $.ui.log(message.slice(0, 1000));
      if (message !== 'Changing billing in this chat.') await release($);
    } catch {
      // Unknown ACK stays held, never sends an intercepted prompt to a payer.
      $.ui.log('Billing control unavailable; input admission remains held.');
    }
  }
export function register(on) {
  on('prompt.edit', ($, e, next) => {
    editRevision += 1;
    return next(e);
  });
  on('prompt.change', async ($, e, next) => {
    const result = await next(e);
    if (e.origin.kind === 'history' || e.origin.kind === 'undo') editRevision += 1;
    if (e.origin.kind === 'engine' && putbacks.get(e.text) === editRevision) {
      putbacks.delete(e.text);
      // Native restores a prompt whose hook did not call next. Remove only
      // that exact untouched put-back; its single owner is our pending entry.
      const current = await $.prompt.read();
      if (current.text === e.text) await $.prompt.fill({ text: '', cursor: 0, mode: 'replace' });
    }
    return result;
  });
  on('prompt.submit', async ($, e, next) => {
    const match = control(e.text);
    if (match && !e.command && e.origin.kind === 'composer') {
      putbacks.set(e.text, editRevision);
      await billing($, match[1].toLowerCase());
      return { text: e.text, origin: e.origin };
    }
    if (!holding || e.origin.kind === 'plugin') return next(e);
    if (e.command || e.attachments?.length || e.isMeta || pending.length >= 128
        || new TextEncoder().encode(e.text).length > 1024 * 1024) {
      // Native attachment handles/command provenance cannot be transplanted.
      // Leave this submission on its original native path; refuse handoff.
      untransferable = true;
      return next(e);
    }
    pending.push({ identifier: crypto.randomUUID().replaceAll('-', ''), text: e.text });
    putbacks.set(e.text, editRevision);
    return { text: e.text, origin: e.origin };
  }).catch(($, e, next) => {
    untransferable = true;
    return next.called ? next(e) : { drop: 'Billing input admission could not be verified.' };
  });
  on('session.start', async ($, e, next) => {
    const result = await next(e);
    if ((await $.session.version()).version !== VERSION) return result;
    const requestPath = await $.env.get('OV_BOUNDARY_CONTROL');
    const responsePath = await $.env.get('OV_BOUNDARY_RESPONSE');
    const generation = await $.env.get('OV_BOUNDARY_GENERATION');
    if (!requestPath || !responsePath || !generation) return result;
    await $.command.register({ name: 'openvegas', description: 'OpenVegas local controls',
      argumentHint: '[use v|use host]', immediate: true });
    let answered = null;
    $.clock.every(50, async () => {
      const request = JSON.parse(await $.fs.read(requestPath));
      if (request.generation !== generation || !/^[a-f0-9]{32}$/.test(request.nonce ?? '')
          || request.nonce === answered) return;
      if (request.action === 'release') {
        await release($);
      } else {
        holding = true;
        for (const item of request.pending ?? []) {
          if (!pending.some(existing => existing.identifier === item.identifier)) pending.push(item);
        }
      }
      let draft = await $.prompt.read();
      const target = request.draft ?? draft;
      const filled = await $.prompt.fill({ text: target.text, cursor: target.cursor, mode: 'replace' });
      // Retry readiness, not submission. A modal is never bypassed or approved.
      if (request.draft && !filled.isFilled) return;
      if (!request.draft && !filled.isFilled && filled.refusal === 'no_composer') return;
      draft = await $.prompt.read();
      const settings = await $.settings.read();
      const tools = await $.tool.list();
      const snapshot = { session_id: await $.session.id(), cwd: await $.session.cwd(),
        model: await $.session.model(), effort: null,
        settings_digest: await digest(settings), tools_digest: await digest(tools) };
      await $.fs.write(responsePath, JSON.stringify({ generation, nonce: request.nonce,
        snapshot, draft, composer_ready: filled.isFilled === true, controls_ready: true,
        pending, untransferable }));
      answered = request.nonce;
    });
    return result;
  });
  on('command.run', { command: 'openvegas' }, async ($, e, next) => {
    const match = /^(?:use)[ \t]+(v|host)$/i.exec((e.args ?? '').trim());
    if (!match) return next(e);
    await billing($, match[1].toLowerCase());
    return {};
  });
}
