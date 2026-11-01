#!/usr/bin/env node
import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
import {dirname, join} from 'node:path';
import {spawnSync} from 'node:child_process';

const require = createRequire(import.meta.url);
const release = JSON.parse(readFileSync(new URL('../release.json', import.meta.url), 'utf8'));
const platform = `${process.platform}-${process.arch}`;
const artifact = release.platforms[platform];
function fail(message) {
  console.error(`OpenVegas: ${message}`);
  process.exit(1);
}
if (!artifact) fail(`this release does not support ${platform}. No command was run.`);
let binary;
try {
  const manifestPath = require.resolve(`${artifact.package}/package.json`);
  const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
  if (manifest.name !== artifact.package || manifest.version !== release.version) {
    fail('native package version mismatch. Reinstall this exact OpenVegas version.');
  }
  binary = join(dirname(manifestPath), 'bin', artifact.binary);
  if (createHash('sha256').update(readFileSync(binary)).digest('hex') !== artifact.sha256) {
    fail('native package checksum mismatch. No command was run.');
  }
} catch {
  fail('native package is missing or unreadable. Reinstall with optional dependencies enabled.');
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
      'OPENVEGAS_ENABLE_TOUCHID', 'OPENVEGAS_TOUCHID_REQUIRE_BIOMETRIC',
    ].includes(key)) delete env[key];
  }
  env.HOME = env.USERPROFILE = home;
  env.OPENVEGAS_BACKEND_URL = 'https://app.openvegas.ai';
  env.OPENVEGAS_INTEGRATION_PROFILE = join(home, '.openvegas');
  env.OPENVEGAS_HOSTED_LAUNCH = '1';
}
env.OPENVEGAS_LOAD_PROJECT_ENV = '0';
if (!args.length && process.stdin.isTTY && process.stdout.isTTY && !process.env.CI) args.push('go');
const result = spawnSync(binary, args, {stdio: 'inherit', shell: false, env});
if (result.error) fail('native runtime could not start. No fallback command was run.');
if (result.signal) {
  process.kill(process.pid, result.signal);
  process.exit(1);
}
process.exit(result.status ?? 1);
