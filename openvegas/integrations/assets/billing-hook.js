// Pre-model control notification only: no wallet, provider, or credential-store access.
const fs = require('node:fs');
const path = require('node:path');
const net = require('node:net');
const {TextDecoder} = require('node:util');
const host = process.argv[1];
const missing = __OPENVEGAS_MISSING__;
const MAX_INPUT = 131072;
const MAX_FRAME = 8192;
const started = Date.now();
let ended = false;
let failure = {};
let input = Buffer.alloc(0);
function finish(value) {
  if (!ended) { ended = true; process.stdout.write(JSON.stringify(value) + '\n'); }
  process.exit(0);
}
function envelope(raw) {
  try { return JSON.parse(new TextDecoder('utf-8', {fatal: true, ignoreBOM: true}).decode(raw)); }
  catch { return null; }
}
function command(data) {
  if (!data || Array.isArray(data) || typeof data !== 'object'
      || (data.agent_id !== undefined && data.agent_id !== null && data.agent_id !== '')
      || !['claude', 'codex'].includes(host)) return null;
  if (data.hook_event_name === 'SessionStart') return ['register', null];
  if (data.hook_event_name !== 'UserPromptSubmit' || typeof data.prompt !== 'string') return null;
  // Match Python's ASCII-only grammar: horizontal separators, no embedded newlines.
  const prompt = data.prompt.replace(/^[ \t\r\n]+|[ \t\r\n]+$/g, '');
  const billing = prompt.match(/^(?:\/|\$)?openvegas(?::openvegas)?[ \t]+use[ \t]+(v|host)$/i);
  const play = prompt.match(/^(?:\/|\$)?openvegas(?::openvegas)?[ \t]+play(?:[ \t]+(blackjack|roulette|poker|baccarat|slots|horse))?$/i);
  const lobby = prompt.match(/^(?:\/|\$)?openvegas(?::openvegas)?$/i);
  // JavaScript's $ also matches before a final Unicode line separator; reject it.
  if (billing && billing[0] === prompt) return [billing[1].toLowerCase(), null];
  if ((play && play[0] === prompt) || (lobby && lobby[0] === prompt)) return ['play', play?.[1]?.toLowerCase() ?? null];
  return null;
}
function inputFailure() {
  const control = command(envelope(input));
  return finish(control && control[0] !== 'register' ? {decision: 'block', reason: missing} : {});
}
const deadline = setTimeout(inputFailure, 4000);
(async () => {
  for await (const chunk of process.stdin) {
    input = Buffer.concat([input, chunk.subarray(0, MAX_INPUT + 1 - input.length)]);
    if (input.length > MAX_INPUT) return inputFailure();
  }
  const data = envelope(input);
  const control = command(data);
  if (!control) return finish({});
  const explicit = control[0] !== 'register';
  if (explicit) failure = {decision: 'block', reason: missing};
  const reply = (message, handled = false) => finish(!explicit ? {} : handled
    ? {continue: false, stopReason: message, suppressOutput: true}
    : {decision: 'block', reason: message});
  clearTimeout(deadline);
  // Leave time for the native host's five-second hook deadline, even after slow stdin.
  setTimeout(() => reply(missing), Math.max(1, Math.min(3000, 4000 - (Date.now() - started))));
  const socketPath = process.env.OPENVEGAS_SWITCH_SOCKET;
  const token = process.env.OPENVEGAS_SWITCH_TOKEN;
  if (!socketPath || !token || !process.getuid) return reply(missing);
  const payload = Buffer.from(JSON.stringify({
    token, host, action: control[0],
    session_id: data.session_id ?? null, model: data.model ?? null, game: control[1],
    transcript_path: data.transcript_path ?? null, rate_limits: null,
  }) + '\n');
  if (payload.length > MAX_FRAME) return reply(missing);
  try {
    const socket = fs.lstatSync(socketPath), parent = fs.statSync(path.dirname(socketPath));
    if (!socket.isSocket() || socket.uid !== process.getuid() || parent.uid !== process.getuid() || (parent.mode & 0o077)) return reply(missing);
  } catch { return reply(missing); }
  const connection = net.createConnection(socketPath);
  let response = Buffer.alloc(0);
  connection.on('error', () => reply(missing));
  connection.on('end', () => reply(missing));
  connection.on('connect', () => connection.write(payload));
  connection.on('data', chunk => {
    const newline = chunk.indexOf(10);
    const part = newline < 0 ? chunk : chunk.subarray(0, newline + 1);
    if (response.length + part.length > MAX_FRAME) return reply(missing);
    response = Buffer.concat([response, part]);
    if (newline < 0) return;
    try {
      const result = envelope(response);
      const valid = result && typeof result.message === 'string';
      reply(valid ? result.message : missing, valid && result.ok === true);
    } catch { reply(missing); }
  });
})().catch(() => finish(failure));
