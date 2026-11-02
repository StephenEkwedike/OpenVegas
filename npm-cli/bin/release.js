#!/usr/bin/env node
import {createHash} from 'node:crypto';
import {createReadStream, readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
import {dirname, join} from 'node:path';
import {spawn} from 'node:child_process';

const require = createRequire(import.meta.url);
let timer;
let shown = false;
let shownWidth = 0;
let child;
let interrupted;
const signals = ['SIGINT', 'SIGTERM'];
let handlers = [];
function terminalColumns() {
  const columns = process.stderr.columns;
  return Number.isInteger(columns) && columns > 0 ? columns : 80;
}
function eraseLoading() {
  // A terminal resize can reflow the previous single-line frame onto more rows.
  const rows = Math.max(1, Math.ceil(shownWidth / terminalColumns()));
  return '\r\x1b[2K' + '\x1b[1A\r\x1b[2K'.repeat(rows - 1);
}
function clearLoading(done) {
  clearInterval(timer);
  process.stderr.removeListener('resize', drawLoading);
  const clear = shown ? eraseLoading() : '';
  shown = false;
  shownWidth = 0;
  if (clear) process.stderr.write(clear, done);
  else done?.();
}
function finish(code, signal) {
  clearLoading();
  signals.forEach((name, i) => { if (handlers[i]) process.removeListener(name, handlers[i]); });
  if (signal) {
    process.kill(process.pid, signal);
    return;
  }
  process.exit(code ?? 1);
}
function fail(message) {
  clearLoading();
  console.error(`OpenVegas: ${message}`);
  finish(1);
}
const args = process.argv.slice(2);
// A terminal installed for development can export a localhost account. The
// public launcher must not silently adopt it (or a project's backend .env).
const local = args[0] === '--local';
if (local) args.shift();
const env = {...process.env};
if (local) {
  let url;
  try { url = new URL(env.OPENVEGAS_BACKEND_URL || ''); } catch {}
  if (!url || !['localhost', '127.0.0.1', '[::1]'].includes(url.hostname)) {
    fail('--local requires OPENVEGAS_BACKEND_URL pointing to localhost. Nothing was changed.');
  }
} else {
  const home = env.OPENVEGAS_HOST_HOME || env.HOME || env.USERPROFILE;
  if (!home) fail('home directory unavailable. Nothing was changed.');
  for (const key of Object.keys(env)) {
    if (key.startsWith('OPENVEGAS_') && ![
      'OPENVEGAS_TERMINAL_SOCKET', 'OPENVEGAS_TERMINAL_TOKEN',
    ].includes(key)) delete env[key];
  }
  env.HOME = env.USERPROFILE = home;
  env.OPENVEGAS_BACKEND_URL = 'https://app.openvegas.ai';
  env.OPENVEGAS_INTEGRATION_PROFILE = join(home, '.openvegas');
  env.OPENVEGAS_HOSTED_LAUNCH = '1';
}
env.OPENVEGAS_LOAD_PROJECT_ENV = '0';
if (!args.length && process.stdin.isTTY && process.stdout.isTTY && !process.env.CI) args.push('go');
delete env.OPENVEGAS_LAUNCH_NOTIFY_FD;
const verbose = args[0] === 'go' && args.includes('--verbose');
const loading = args[0] === 'go' && !args.includes('--dry-run') && !verbose &&
  process.stderr.isTTY && !process.env.CI;
let label = 'Checking OpenVegas runtime...';
let frame = 0;
function drawLoading() {
  const clear = eraseLoading();
  // Labels and frames are ASCII. Leave the last terminal cell unused so no
  // delayed auto-wrap can strand a suffix when the next label is shorter.
  const text = `${['|', '/', '-', '\\'][frame++ % 4]} ${label}`.slice(0, terminalColumns() - 1);
  shown = true;
  shownWidth = text.length;
  process.stderr.write(`${clear}\x1b[34m${text}\x1b[0m`);
}
const verificationStart = performance.now();
if ((loading || verbose) && process.platform !== 'win32') env.OPENVEGAS_LAUNCH_NOTIFY_FD = '3';
if (loading && process.platform !== 'win32') {
  drawLoading();
  timer = setInterval(drawLoading, 100);
  process.stderr.on('resize', drawLoading);
} else if (loading) {
  process.stderr.write('Starting OpenVegas...\n');
}
handlers = signals.map(signal => {
  const handler = () => {
    interrupted = signal;
    clearLoading();
    if (child) child.kill(signal);
    else finish(null, signal);
  };
  process.on(signal, handler);
  return handler;
});
let release;
try {
  release = JSON.parse(readFileSync(new URL('../release.json', import.meta.url), 'utf8'));
} catch {
  fail('release metadata is missing or unreadable. No command was run.');
}
const platform = `${process.platform}-${process.arch}`;
const artifact = release?.platforms?.[platform];
if (!artifact) fail(`this release does not support ${platform}. No command was run.`);
let binary;
try {
  const manifestPath = require.resolve(`${artifact.package}/package.json`);
  const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
  if (manifest.name !== artifact.package || manifest.version !== release.version) {
    fail('native package version mismatch. Reinstall this exact OpenVegas version.');
  }
  binary = join(dirname(manifestPath), 'bin', artifact.binary);
  // Stream the large native artifact: synchronous reading/hashing freezes the
  // animation before PyInstaller has even started its local extraction.
  const hash = createHash('sha256');
  for await (const chunk of createReadStream(binary)) hash.update(chunk);
  if (hash.digest('hex') !== artifact.sha256) {
    fail('native package checksum mismatch. No command was run.');
  }
} catch {
  fail('native package is missing or unreadable. Reinstall with optional dependencies enabled.');
}
label = 'Starting OpenVegas...';
if (shown) drawLoading();
if (verbose) console.error(`OpenVegas runtime verified (${((performance.now() - verificationStart) / 1000).toFixed(2)}s); starting.`);
const startupStart = performance.now();
child = spawn(binary, args, {stdio: ['inherit', 'inherit', 'inherit', 'pipe'], shell: false, env});
let ready = '';
let acknowledged = false;
child.stdio[3].on('data', data => {
  ready = (ready + data.toString()).slice(0, 64);
  if (!acknowledged && ready === 'ready\n') {
    acknowledged = true;
    clearLoading(() => {
      if (verbose) console.error(`OpenVegas runtime ready (${((performance.now() - startupStart) / 1000).toFixed(2)}s startup).`);
      // Flush the clear before letting the runtime print consent or success.
      child.stdio[3].end('1');
    });
  }
});
child.stdio[3].on('error', () => {});
child.once('error', () => {
  fail('native runtime could not start. No fallback command was run.');
});
child.once('close', (code, signal) => {
  finish(code, interrupted || signal);
});
