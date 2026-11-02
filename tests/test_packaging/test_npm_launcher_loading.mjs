// Run: node --experimental-vm-modules --test tests/test_packaging/test_npm_launcher_loading.mjs
// Offline source-level terminal regression checks, not native UX certification.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {EventEmitter} from 'node:events';
import {readFileSync} from 'node:fs';
import {dirname, join} from 'node:path';
import {test} from 'node:test';
import {createContext, SourceTextModule, SyntheticModule} from 'node:vm';

const source = readFileSync(process.env.OV_TEST_LAUNCHER_SOURCE ||
  new URL('../../npm-cli/bin/release.js', import.meta.url), 'utf8');

class Terminal {
  constructor(columns) {
    this.columns = columns;
    this.rows = [[]];
    this.row = 0;
    this.column = 0;
    this.raw = '';
  }

  write(value) {
    this.raw += value;
    for (let i = 0; i < value.length; i++) {
      if (value[i] === '\x1b') {
        const match = /^\x1b\[([0-9;]*)([mKA])/.exec(value.slice(i));
        assert.ok(match, `Unhandled terminal sequence: ${JSON.stringify(value.slice(i))}`);
        if (match[2] === 'K') {
          assert.equal(match[1], '2');
          this.rows[this.row] = [];
        } else if (match[2] === 'A') {
          this.row = Math.max(0, this.row - Number(match[1] || 1));
        }
        i += match[0].length - 1;
      } else if (value[i] === '\r') {
        this.column = 0;
      } else if (value[i] === '\n') {
        this.row++;
        this.column = 0;
        this.rows[this.row] ||= [];
      } else {
        if (this.column === this.columns) {
          this.row++;
          this.column = 0;
        }
        this.rows[this.row] ||= [];
        this.rows[this.row][this.column++] = value[i];
      }
    }
  }

  get lines() {
    return this.rows.map(row => row.join('').trimEnd());
  }

  reflow(columns) {
    // Resize while only the startup frame owns the current logical line.
    const text = this.rows.flat().join('');
    this.columns = columns;
    this.rows = [[]];
    this.row = this.column = 0;
    const raw = this.raw;
    this.write(text);
    this.raw = raw;
  }
}

async function launch({columns = 80, tty = true, args = ['go'], platform = 'darwin', env = {}} = {}) {
  const screen = new Terminal(columns || 80);
  const proc = new EventEmitter();
  const stderr = new EventEmitter();
  const pendingWrites = [];
  const intervals = new Set();
  const exits = [];
  const acknowledgements = [];
  const child = new EventEmitter();
  const notify = new EventEmitter();
  const calls = [];
  Object.assign(stderr, {isTTY: tty, columns, write(value, callback) {
    screen.write(value);
    if (callback) pendingWrites.push(callback);
    return true;
  }});
  Object.assign(proc, {
    argv: ['node', '/fixture/bin/openvegas.js', ...args],
    env: {HOME: '/fixture/home', ...env}, platform, arch: 'arm64',
    stdin: {isTTY: tty}, stdout: {isTTY: tty}, stderr,
    exit: code => exits.push(code),
  });
  child.stdio = [null, null, null, notify];
  notify.end = value => acknowledgements.push(value);
  const bytes = Buffer.from('offline mock runtime');
  const artifact = {package: '@fixture/native', binary: 'runtime',
    sha256: createHash('sha256').update(bytes).digest('hex')};
  let releaseVerification;
  const verification = new Promise(resolve => { releaseVerification = resolve; });
  const modules = {
    'node:crypto': {createHash},
    'node:fs': {
      readFileSync: path => JSON.stringify(String(path).endsWith('release.json') ?
        {version: '1.0.0', platforms: {[`${platform}-arm64`]: artifact}} :
        {name: artifact.package, version: '1.0.0'}),
      createReadStream: async function* () { await verification; yield bytes; },
    },
    'node:module': {createRequire: () => ({resolve: () => '/fixture/native/package.json'})},
    'node:path': {dirname, join},
    'node:child_process': {spawn: (...call) => { calls.push(call); return child; }},
  };
  const context = createContext({process: proc, URL, performance: {now: () => 0},
    console: {error: value => screen.write(value + '\n')},
    setInterval: callback => { intervals.add(callback); return callback; },
    clearInterval: callback => intervals.delete(callback),
  });
  const module = new SourceTextModule(source, {context,
    initializeImportMeta: meta => { meta.url = 'file:///fixture/bin/openvegas.js'; },
  });
  await module.link(specifier => {
    assert.ok(modules[specifier], `Unexpected dependency: ${specifier}`);
    return new SyntheticModule(Object.keys(modules[specifier]), function () {
      for (const [name, value] of Object.entries(modules[specifier])) this.setExport(name, value);
    }, {context});
  });
  const evaluated = module.evaluate();
  return {screen, proc, child, stderr, notify, acknowledgements, calls, exits,
    tick: () => { for (const callback of intervals) callback(); },
    flush: () => { while (pendingWrites.length) pendingWrites.shift()(); },
    start: async () => { releaseVerification(); await evaluated; },
    resize: value => { screen.reflow(value); stderr.columns = value; stderr.emit('resize'); },
  };
}

test('long verification label becomes a short startup label with no stale suffix', async () => {
  const run = await launch();
  assert.deepEqual(run.screen.lines, ['| Checking OpenVegas runtime...']);
  run.tick();
  await run.start();
  run.tick();
  assert.match(run.screen.lines[0], /^[|/\\-] Starting OpenVegas\.\.\.$/);
  assert.equal(run.screen.lines.length, 1);
  assert.equal(run.calls.length, 1);
  assert.deepEqual(Array.from(run.calls[0][2].stdio), ['inherit', 'inherit', 'inherit', 'pipe']);
});

for (const columns of [1, 2, 12, 22, 30, 31, 80, 0, null]) {
  test(`spinner stays on one row at ${columns ?? 'unknown'} columns`, async () => {
    const run = await launch({columns});
    run.tick();
    assert.equal(run.screen.row, 0);
    assert.ok(run.screen.lines[0].length < (columns || 80));
    await run.start();
    run.tick();
    assert.equal(run.screen.row, 0);
    run.notify.emit('data', Buffer.from('ready\n'));
    run.flush();
    assert.deepEqual(run.screen.lines, ['']);
  });
}

test('terminal shrink erases reflowed rows before redraw and handoff', async () => {
  const run = await launch();
  run.resize(12);
  assert.deepEqual(run.screen.lines.filter(Boolean), ['/ Checking']);
  assert.equal(run.screen.row, 0);
  await run.start();
  run.resize(8);
  assert.equal(run.screen.row, 0);
  assert.equal(run.screen.lines.filter(Boolean).length, 1);
  run.notify.emit('data', Buffer.from('ready\n'));
  run.flush();
  assert.deepEqual(run.screen.lines.filter(Boolean), []);
});

test('fragmented readiness waits for the clear to flush; child stdout remains untouched', async () => {
  const run = await launch();
  await run.start();
  run.tick();
  run.notify.emit('data', Buffer.from('rea'));
  run.flush();
  assert.deepEqual(run.acknowledgements, []);
  run.notify.emit('data', Buffer.from('dy\n'));
  assert.deepEqual(run.acknowledgements, [], 'runtime must wait until the clear has flushed');
  assert.deepEqual(run.screen.lines, ['']);
  run.flush();
  assert.deepEqual(run.acknowledgements, ['1']);
  const before = run.screen.raw;
  run.tick();
  run.stderr.emit('resize');
  assert.equal(run.screen.raw, before, 'all spinner writers must stop at handoff');
  run.screen.write('Continue? y\r\n\x1b[34mOpenVegas ready.\x1b[0m\r\n');
  run.notify.emit('data', Buffer.from('ready\n'));
  run.child.emit('close', 0, null);
  run.flush();
  assert.deepEqual(run.screen.lines, ['Continue? y', 'OpenVegas ready.', '']);
  assert.deepEqual(run.exits, [0]);
  assert.deepEqual(run.acknowledgements, ['1']);
  assert.equal(run.stderr.listenerCount('resize'), 0);
});

test('readiness handoff does not erase a child failure without a trailing newline', async () => {
  const run = await launch();
  await run.start();
  run.notify.emit('data', Buffer.from('ready\n'));
  run.flush();
  run.screen.write('SETUP FAILED');
  run.child.emit('close', 19, null);
  run.tick();
  assert.deepEqual(run.screen.lines, ['SETUP FAILED']);
  assert.deepEqual(run.exits, [19]);
  assert.ok(!run.screen.raw.includes('OpenVegas ready.'));
});

for (const options of [{tty: false}, {env: {CI: '1'}}, {args: ['go', '--dry-run']}]) {
  test(`noninteractive mode remains plain: ${JSON.stringify(options)}`, async () => {
    const run = await launch(options);
    await run.start();
    run.tick();
    run.child.emit('close', 0, null);
    assert.equal(run.screen.raw, '');
  });
}
