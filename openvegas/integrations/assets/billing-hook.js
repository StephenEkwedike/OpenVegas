// Pre-model control notification only: no wallet, provider, or credential-store access.
const fs = require('node:fs');
const path = require('node:path');
const net = require('node:net');
const host = process.argv[1];
const missing = __OPENVEGAS_MISSING__;
let ended = false;
let failure = {};
function finish(value) {
  if (!ended) { ended = true; process.stdout.write(JSON.stringify(value) + '\n'); }
  process.exit(0);
}
const deadline = setTimeout(() => finish({}), 4000);
(async () => {
  let input = Buffer.alloc(0);
  for await (const chunk of process.stdin) {
    if (input.length + chunk.length > 131072) return finish({});
    input = Buffer.concat([input, chunk]);
  }
  let data;
  try { data = JSON.parse(input.toString()); } catch { return finish({}); }
  if (!data || typeof data !== 'object' || data.agent_id || !['claude', 'codex'].includes(host)) return finish({});
  const prompt = data.hook_event_name === 'UserPromptSubmit' ? String(data.prompt || '').trim() : '';
  const billing = prompt.match(/^(?:\/|\$)?openvegas(?::openvegas)? use (v|host)$/i);
  const play = prompt.match(/^(?:\/|\$)?openvegas(?::openvegas)? play(?: (blackjack|roulette|poker|baccarat|slots|horse))?$/i);
  const explicit = Boolean(billing || play);
  if (explicit) failure = {decision: 'block', reason: missing};
  if (!explicit && data.hook_event_name !== 'SessionStart') return finish({});
  const reply = (message, handled = false) => finish(!explicit ? {} : handled
    ? {continue: false, stopReason: message, suppressOutput: true}
    : {decision: 'block', reason: message});
  clearTimeout(deadline);
  setTimeout(() => reply(missing), 3000);
  const socketPath = process.env.OPENVEGAS_SWITCH_SOCKET;
  const token = process.env.OPENVEGAS_SWITCH_TOKEN;
  if (!socketPath || !token || !process.getuid) return reply(missing);
  try {
    const socket = fs.lstatSync(socketPath), parent = fs.statSync(path.dirname(socketPath));
    if (!socket.isSocket() || socket.uid !== process.getuid() || parent.uid !== process.getuid() || (parent.mode & 0o077)) return reply(missing);
  } catch { return reply(missing); }
  const connection = net.createConnection(socketPath);
  let response = '';
  connection.on('error', () => reply(missing));
  connection.on('end', () => reply(missing));
  connection.on('connect', () => connection.write(JSON.stringify({
    token, host, action: play ? 'play' : billing ? billing[1].toLowerCase() : 'register',
    session_id: data.session_id || null, model: data.model || null,
    game: play && play[1] ? play[1].toLowerCase() : null,
    transcript_path: data.transcript_path || null, rate_limits: null,
  }) + '\n'));
  connection.on('data', chunk => {
    response += chunk.toString();
    if (Buffer.byteLength(response) > 8192) return reply(missing);
    if (!response.includes('\n')) return;
    try {
      const result = JSON.parse(response.split('\n')[0]);
      const valid = result && typeof result.message === 'string';
      reply(valid ? result.message : missing, valid && result.ok === true);
    } catch { reply(missing); }
  });
})().catch(() => finish(failure));
