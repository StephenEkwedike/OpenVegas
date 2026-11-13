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
  const turns = new Set();
  const toolsRunning = new Set();
  let background = new Set();
  let workUnknown = false;
  let workRevision = 0;
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
  function refuse($) {
    pending = [];
    holding = false;
    untransferable = false;
    $.ui.log('Waiting prompts were not sent; re-enter after switch.');
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
      // Only the owner's explicit refusal acknowledges a safe original payer.
      // An error/timeout reply alone cannot release held input or submit it.
      if (message === 'Billing is already on the requested route.') refuse($);
    } catch {
      // Unknown ACK stays held, never sends an intercepted prompt to a payer.
      $.ui.log('Billing control unavailable; input admission remains held.');
    }
  }
export function register(on) {
  // Observe native work, never cancel/approve it or change the offered tools.
  // These records cover engine-owned work, not arbitrary detached OS children.
  on('turn.start', ($, e, next) => {
    turns.add(e.turnId);
    workRevision += 1;
    return next(e);
  });
  on('turn.complete', async ($, e, next) => {
    const result = await next(e);
    turns.delete(e.turnId);
    workRevision += 1;
    return result;
  });
  on('tool.call', async ($, e, next) => {
    toolsRunning.add(e.tool_use_id);
    workRevision += 1;
    try {
      const result = await next(e);
      toolsRunning.delete(e.tool_use_id);
      workRevision += 1;
      return result;
    } catch (error) {
      workUnknown = true;
      workRevision += 1;
      throw error;
    }
  });
  on('classic.Stop', ($, e, next) => {
    if (!e.agent_id) {
      const tasks = e.background_tasks;
      background = Array.isArray(tasks)
        && tasks.every(task => typeof task?.id === 'string' && task.id.length > 0)
        && new Set(tasks.map(task => task.id)).size === tasks.length
        ? new Set(tasks.map(task => task.id)) : null;
      workRevision += 1;
    }
    return next(e);
  });
  on('ui.render', { component: 'UserMessage' }, ($, e, next) => {
    // Native read-only provenance, never notification text or an inferred PID.
    // Unseen completions stay blocked; rendering unrelated/history text cannot
    // establish that a task observed running has stopped.
    const { origin, task } = e.props;
    if (origin?.kind === 'task-notification'
        && ['completed', 'failed', 'killed'].includes(task?.status)
        && background?.delete(task.id)) workRevision += 1;
    return next(e);
  });
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
    if (!holding) return next(e);
    if (e.origin.kind !== 'composer' || e.command || e.attachments?.length || e.isMeta || pending.length >= 128
        || new TextEncoder().encode(e.text).length > 1024 * 1024) {
      // Native notification/peer/plugin origins, attachment handles and command
      // provenance cannot be transplanted as a human's prompt.
      // Leave this submission on its original native path; refuse handoff.
      untransferable = true;
      workRevision += 1;
      return next(e);
    }
    pending.push({ identifier: crypto.randomUUID().replaceAll('-', ''), text: e.text });
    workRevision += 1;
    putbacks.set(e.text, editRevision);
    return { text: e.text, origin: e.origin };
  }).catch(($, e, next) => {
    untransferable = true;
    workRevision += 1;
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
    let observing = false;
    $.clock.every(50, async () => {
      if (observing) return;
      observing = true;
      try {
      const request = JSON.parse(await $.fs.read(requestPath));
      if (request.generation !== generation || !/^[a-f0-9]{32}$/.test(request.nonce ?? '')
          || request.nonce === answered) return;
      if (request.action === 'refuse') {
        refuse($);
        await $.fs.write(responsePath, JSON.stringify({ generation, nonce: request.nonce,
          action: 'refuse', refused: true, pending: [] }));
        answered = request.nonce;
        return;
      }
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
      const revision = workRevision;
      const settings = await $.settings.read();
      const tools = await $.tool.list();
      const agents = await $.agent.list();
      // Stop's scheduler inventory is historical. Read current jobs without a
      // model request; a denied/failed/malformed read cannot establish idle.
      let scheduled = null;
      try {
        const listing = await $.tool.call({ tool: 'CronList' });
        const jobs = listing?.result?.jobs;
        if (listing && !('deny' in listing) && Array.isArray(jobs)
            && jobs.every(job => typeof job?.id === 'string' && job.id.length > 0)
            && new Set(jobs.map(job => job.id)).size === jobs.length) scheduled = jobs.length;
      } catch { /* Unknown scheduler state refuses handoff, not native work. */ }
      const work = { turns: turns.size, tools: toolsRunning.size, background: background?.size ?? null, scheduled,
        agents: agents.filter(agent => !['completed', 'failed', 'killed'].includes(agent.status)).length,
        unknown: workUnknown };
      const boundaryReady = !work.unknown && work.turns === 0 && work.tools === 0
        && work.background === 0 && work.scheduled === 0 && work.agents === 0;
      const snapshot = { session_id: await $.session.id(), cwd: await $.session.cwd(),
        model: await $.session.model(), effort: null,
        settings_digest: await digest(settings), tools_digest: await digest(tools) };
      // Async getters can interleave with native task/turn/tool events. Retry
      // observation rather than publishing an internally inconsistent snapshot.
      if (revision !== workRevision) return;
      await $.fs.write(responsePath, JSON.stringify({ generation, nonce: request.nonce,
        snapshot, draft, composer_ready: filled.isFilled === true, controls_ready: true,
        boundary_ready: boundaryReady, work, pending, untransferable }));
      answered = request.nonce;
      } finally {
        observing = false;
      }
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
