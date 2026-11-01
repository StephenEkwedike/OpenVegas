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
if (!args.length && process.stdin.isTTY && process.stdout.isTTY && !process.env.CI) args.push('go');
const result = spawnSync(binary, args, {stdio: 'inherit', shell: false});
if (result.error) fail('native runtime could not start. No fallback command was run.');
if (result.signal) {
  process.kill(process.pid, result.signal);
  process.exit(1);
}
process.exit(result.status ?? 1);
