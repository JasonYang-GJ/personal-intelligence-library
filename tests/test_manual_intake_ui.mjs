// Behavioral tests against the real controller and an in-memory File System
// Access implementation. These do not verify browser picker/permission support.
import assert from 'node:assert/strict';
import {randomUUID} from 'node:crypto';
import fs from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../ui/manual-intake.js', import.meta.url), 'utf8');
const PROJECT = '00000000-0000-4000-8000-000000000005';
const OTHER_PROJECT = '00000000-0000-4000-8000-000000000004';
const REQUESTS = '.intake/manual/requests/';
const RESULTS = '.intake/manual/results/';
const canonical = value => JSON.stringify(value, null, 2) + '\n';
const plain = value => JSON.parse(JSON.stringify(value));
const missing = () => Object.assign(new Error('Not found'), {name: 'NotFoundError'});
const now = () => new Date().toISOString();

function pause() {
  let enter, release;
  const entered = new Promise(resolve => { enter = resolve; });
  const blocked = new Promise(resolve => { release = resolve; });
  return {enter, release, entered, blocked};
}

function disk(projectId = PROJECT) {
  const files = new Map([
    ['data/management/project.json', canonical({schema_version: 1, project_id: projectId})],
    ['data/catalog.json', 'original catalog bytes'],
    ['data/originals/intel-20261018-001.md', 'original report bytes'],
    ['data/intake-state.json', 'original ledger bytes'],
    ['个人情报库.html', 'original homepage bytes'],
  ]);
  const directories = new Set(['', 'data', 'data/management', 'data/originals']);
  const writes = [], mutations = [], reads = [];
  const state = {permission: 'granted', failWrite: false, corruptClose: false,
    closedThenThrows: false, failReadback: false, nextWritePause: null, nextReadbackPause: null};

  function fileHandle(path) {
    return {
      kind: 'file', name: path.split('/').at(-1),
      async getFile() {
        reads.push(path);
        if (!files.has(path)) throw missing();
        const isReadback = path.startsWith(REQUESTS) && writes.some(write => write.path === path);
        if (isReadback && state.nextReadbackPause) {
          const held = state.nextReadbackPause;
          state.nextReadbackPause = null;
          held.enter();
          await held.blocked;
        }
        if (isReadback && state.failReadback) throw new Error('Simulated readback failure');
        // Capture a snapshot: later writes cannot change this File's contents.
        const content = files.get(path);
        return {size: Buffer.byteLength(content), text: async () => content};
      },
      async createWritable() {
        mutations.push({operation: 'createWritable', path});
        let pending = '', aborted = false;
        return {
          async write(value) {
            if (state.nextWritePause) {
              const held = state.nextWritePause;
              state.nextWritePause = null;
              held.enter();
              await held.blocked;
            }
            if (state.failWrite) throw new Error('Simulated disk write failure');
            pending = value;
          },
          async close() {
            assert.equal(aborted, false);
            const content = state.corruptClose ? pending + 'unexpected bytes' : pending;
            files.set(path, content);
            writes.push({path, content});
            if (state.closedThenThrows) throw new Error('Close acknowledgement lost');
          },
          async abort() { aborted = true; },
        };
      },
    };
  }

  function directoryHandle(path) {
    const prefix = path ? path + '/' : '';
    return {
      kind: 'directory',
      async queryPermission(options) { assert.equal(options.mode, 'readwrite'); return state.permission; },
      async getDirectoryHandle(name, options) {
        assert.ok(name && !/[\\/]/.test(name), 'directory names must be one segment');
        const child = prefix + name;
        if (!directories.has(child)) {
          if (!options?.create) throw missing();
          directories.add(child);
          mutations.push({operation: 'mkdir', path: child});
        }
        return directoryHandle(child);
      },
      async getFileHandle(name, options) {
        assert.ok(name && !/[\\/]/.test(name), 'file names must be one segment');
        const child = prefix + name;
        if (!files.has(child)) {
          if (!options?.create) throw missing();
          files.set(child, '');
          mutations.push({operation: 'createFile', path: child});
        }
        return fileHandle(child);
      },
      async *entries() {
        for (const child of [...directories]) {
          if (child.startsWith(prefix) && child !== path && !child.slice(prefix.length).includes('/')) {
            yield [child.slice(prefix.length), directoryHandle(child)];
          }
        }
        for (const child of [...files.keys()]) {
          if (child.startsWith(prefix) && !child.slice(prefix.length).includes('/')) {
            yield [child.slice(prefix.length), fileHandle(child)];
          }
        }
      },
    };
  }

  function seed(path, value) {
    const segments = path.split('/');
    for (let index = 1; index < segments.length; index++) directories.add(segments.slice(0, index).join('/'));
    files.set(path, typeof value === 'string' ? value : canonical(value));
  }
  function request(overrides = {}) {
    const value = {schema_version: 1, project_id: PROJECT, id: randomUUID(), action: 'intake', requested_at: now(), ...overrides};
    seed(REQUESTS + value.id + '.json', value);
    return value;
  }
  function result(request, status, overrides = {}) {
    const value = {schema_version: 1, project_id: PROJECT, id: request.id, status,
      message: '后台状态：' + status, updated_at: now(), ...overrides};
    seed(RESULTS + request.id + '.json', value);
    return value;
  }
  return {root: directoryHandle(''), files, writes, mutations, reads, state, seed, request, result};
}

function controller(storage = disk(), options = {}) {
  const events = [], listeners = new Map(), timers = new Map();
  let root = options.connected === false ? null : storage.root, current = {}, connectCount = 0, timerId = 0;
  const context = {
    Date, Math, crypto: {randomUUID}, document: {hidden: false},
    window: {
      setInterval(callback, ms) { timers.set(++timerId, {callback, ms}); return timerId; },
      clearInterval(id) { timers.delete(id); },
      addEventListener(type, callback) { listeners.set(type, callback); },
      removeEventListener(type, callback) { if (listeners.get(type) === callback) listeners.delete(type); },
    },
  };
  context.globalThis = context;
  vm.createContext(context);
  vm.runInContext(source, context, {filename: 'ui/manual-intake.js'});
  const api = context.window.IntelManualIntake.create({
    projectId: PROJECT,
    getRoot: () => root,
    async connect() {
      connectCount++;
      if (options.connect) return options.connect();
      root = storage.root;
    },
    onStatus(value) { const event = plain(value); events.push(event); current = {...current, ...event}; },
  });
  return {api, storage, events, listeners, timers,
    get status() { return current; }, get connectCount() { return connectCount; },
    setRoot(value) { root = value; },
  };
}

function assertNoConfirmedSubmission(h) {
  assert.ok(!h.events.some(event => ['waiting', 'running', 'complete'].includes(event.status)),
    'failure must not be represented as a submitted, running, or successful request');
}

test('one click appends exactly five request fields, verifies persisted bytes, and changes no library files', async () => {
  const h = controller(disk(), {connected: false}), before = new Map(h.storage.files);
  await h.api.submit();
  assert.equal(h.connectCount, 1);
  assert.equal(h.storage.writes.length, 1);
  const write = h.storage.writes[0], request = JSON.parse(write.content);
  assert.deepEqual(Object.keys(request).sort(), ['action', 'id', 'project_id', 'requested_at', 'schema_version']);
  assert.equal(request.schema_version, 1);
  assert.equal(request.project_id, PROJECT);
  assert.equal(request.action, 'intake');
  assert.match(request.id, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  assert.ok(Number.isFinite(Date.parse(request.requested_at)));
  assert.equal(write.path, REQUESTS + request.id + '.json');
  assert.equal(h.storage.files.get(write.path), write.content);
  assert.ok(h.storage.reads.includes(write.path));
  assert.equal(h.status.status, 'waiting');
  assert.equal(h.status.busy, true);
  assert.ok(!h.events.some(event => event.status === 'complete'));
  for (const [path, content] of before) assert.equal(h.storage.files.get(path), content, path);
  assert.ok(h.storage.mutations.every(({path, operation}) => operation === 'mkdir'
    ? ['.intake', '.intake/manual', '.intake/manual/requests'].includes(path)
    : path === write.path), 'the page may append only its manual request');
});

test('the UI reports waiting only after close and a successful readback', {timeout: 3000}, async () => {
  const h = controller(), held = pause();
  h.storage.state.nextReadbackPause = held;
  const submitting = h.api.submit();
  try {
    await held.entered;
    assert.equal(h.storage.writes.length, 1);
    assert.equal(h.status.status, 'submitting');
    assertNoConfirmedSubmission(h);
  } finally { held.release(); }
  await submitting;
  assert.equal(h.status.status, 'waiting');
});

test('rapid repeated clicks and refresh during a write do not append a second request', {timeout: 3000}, async () => {
  const h = controller(), held = pause();
  h.storage.state.nextWritePause = held;
  const submitting = h.api.submit();
  try {
    await held.entered;
    await Promise.all([h.api.submit(), h.api.submit(), h.api.refresh()]);
    assert.equal(h.storage.writes.length, 0);
    assert.equal(h.storage.mutations.filter(value => value.operation === 'createFile').length, 1);
  } finally { held.release(); }
  await submitting;
  await h.api.submit();
  assert.equal(h.storage.writes.length, 1);
  assert.equal(h.status.status, 'waiting');
});

test('an existing pending request survives page reload and blocks duplicate submission', async () => {
  const storage = disk(), request = storage.request();
  const reopened = controller(storage);
  await reopened.api.refresh();
  assert.equal(reopened.status.status, 'waiting');
  assert.equal(reopened.status.updatedAt, request.requested_at);
  await reopened.api.submit();
  assert.equal(storage.writes.length, 0);
  assert.equal(reopened.status.busy, true);
});

test('an older unfinished request takes precedence over a newer completed request', async () => {
  const storage = disk();
  const pending = storage.request({requested_at: '2026-10-18T08:00:00+08:00'});
  storage.result(pending, 'running', {message: 'existing run'});
  const completed = storage.request({requested_at: '2026-10-19T08:00:00+08:00'});
  storage.result(completed, 'complete');
  const h = controller(storage);
  await h.api.refresh();
  await h.api.submit();
  assert.equal(h.status.status, 'running');
  assert.equal(h.status.message, 'existing run');
  assert.equal(storage.writes.length, 0);
});

test('picker cancellation or returning without a connection never submits', async () => {
  for (const connect of [async () => {}, async () => { throw Object.assign(new Error('Picker canceled'), {name: 'AbortError'}); }]) {
    const h = controller(disk(), {connected: false, connect});
    await h.api.submit();
    assert.equal(h.storage.writes.length, 0);
    assertNoConfirmedSubmission(h);
    assert.equal(h.status.busy, false);
    assert.ok(['error', 'disconnected'].includes(h.status.status));
  }
});

test('a wrong project or revoked write permission fails before creating request directories', async () => {
  for (const storage of [disk(OTHER_PROJECT), disk()]) {
    if (storage.files.get('data/management/project.json').includes(PROJECT)) storage.state.permission = 'denied';
    const h = controller(storage);
    await h.api.submit();
    assert.equal(h.status.status, 'error');
    assertNoConfirmedSubmission(h);
    assert.equal(storage.mutations.length, 0);
    assert.equal(storage.writes.length, 0);
  }
});

test('a failed write does not claim saved or successful intake', async () => {
  const h = controller();
  h.storage.state.failWrite = true;
  await h.api.submit();
  assert.equal(h.status.status, 'error');
  assert.equal(h.storage.writes.length, 0);
  assert.equal(h.status.busy, false);
  assertNoConfirmedSubmission(h);
});

test('mismatched readback and unreadable persisted requests never claim submission success', async () => {
  for (const setting of ['corruptClose', 'failReadback']) {
    const h = controller();
    h.storage.state[setting] = true;
    await h.api.submit();
    assert.equal(h.storage.writes.length, 1);
    assert.equal(h.status.status, 'error', setting);
    assertNoConfirmedSubmission(h);
  }
});

test('lost close acknowledgement is accepted only after exact persisted bytes can be read', async () => {
  const h = controller();
  h.storage.state.closedThenThrows = true;
  await h.api.submit();
  assert.equal(h.storage.writes.length, 1);
  assert.equal(h.status.status, 'waiting');
  assert.ok(h.storage.reads.filter(path => path.startsWith(REQUESTS)).length >= 2);
});

test('malformed or mismatched backend results never show complete or permit another request', async () => {
  const invalidResults = [
    '{broken json',
    {schema_version: 2},
    {project_id: OTHER_PROJECT},
    {id: randomUUID()},
    {status: 'success'},
    {message: null},
    {updated_at: 'not a date'},
  ];
  for (const invalid of invalidResults) {
    const storage = disk(), request = storage.request();
    if (typeof invalid === 'string') storage.seed(RESULTS + request.id + '.json', invalid);
    else storage.result(request, 'complete', invalid);
    const h = controller(storage);
    await h.api.refresh();
    assert.equal(h.status.status, 'error', JSON.stringify(invalid));
    await h.api.submit();
    assert.equal(h.status.status, 'error', JSON.stringify(invalid));
    assert.equal(storage.writes.length, 0);
    assertNoConfirmedSubmission(h);
  }
});

test('requests with wrong identity or extra fields cannot be treated as pending or bypassed', async () => {
  for (const overrides of [{project_id: OTHER_PROJECT}, {extra: 'untrusted'}, {action: 'modify'}, {requested_at: 'bad'}]) {
    const storage = disk();
    storage.request(overrides);
    const h = controller(storage);
    await h.api.submit();
    assert.equal(h.status.status, 'error');
    assertNoConfirmedSubmission(h);
    assert.equal(storage.writes.length, 0);
  }
});

test('a trusted failed receipt releases a malformed historical request and permits a new one', async () => {
  for (const damaged of ['{broken json', {action: 'not-an-intake'}, {extra: 'unexpected field'}]) {
    const storage = disk(), request = storage.request();
    const requestPath = REQUESTS + request.id + '.json';
    storage.seed(requestPath, typeof damaged === 'string' ? damaged : {...request, ...damaged});
    storage.result(request, 'failed', {message: '该请求损坏，后台已拒绝处理。'});
    const originalRequest = storage.files.get(requestPath);
    const originalResult = storage.files.get(RESULTS + request.id + '.json');
    const h = controller(storage);
    await h.api.refresh();
    assert.equal(h.status.status, 'failed', JSON.stringify(damaged));
    assert.equal(h.status.busy, false);
    assert.match(h.status.message, /已拒绝处理/);
    await h.api.submit();
    assert.equal(h.status.status, 'waiting', JSON.stringify(damaged));
    assert.equal(storage.writes.length, 1);
    assert.notEqual(JSON.parse(storage.writes[0].content).id, request.id);
    assert.equal(storage.files.get(requestPath), originalRequest);
    assert.equal(storage.files.get(RESULTS + request.id + '.json'), originalResult);
    assert.ok(!h.events.some(event => event.status === 'complete'));
  }
});

test('a malformed request can only be released by a trusted failed receipt, not other states', async () => {
  for (const status of ['waiting', 'running', 'partial', 'complete']) {
    const storage = disk(), request = storage.request();
    storage.seed(REQUESTS + request.id + '.json', {...request, action: 'not-an-intake'});
    storage.result(request, status);
    const h = controller(storage);
    await h.api.refresh();
    assert.equal(h.status.status, 'error', status);
    await h.api.submit();
    assert.equal(h.status.status, 'error', status);
    assert.equal(storage.writes.length, 0);
    assertNoConfirmedSubmission(h);
  }
});

test('failed receipts with invalid identity or format cannot release a damaged request', async () => {
  for (const invalid of [{project_id: OTHER_PROJECT}, {id: randomUUID()}, {schema_version: 2},
    {message: null}, {updated_at: 'invalid'}]) {
    const storage = disk(), request = storage.request();
    storage.seed(REQUESTS + request.id + '.json', '{broken json');
    storage.result(request, 'failed', invalid);
    const h = controller(storage);
    await h.api.refresh();
    assert.equal(h.status.status, 'error', JSON.stringify(invalid));
    await h.api.submit();
    assert.equal(h.status.status, 'error', JSON.stringify(invalid));
    assert.equal(storage.writes.length, 0);
    assertNoConfirmedSubmission(h);
  }
});

test('running, partial, failed, and complete are recovered from disk with accurate busy state', async () => {
  for (const status of ['running', 'partial', 'failed', 'complete']) {
    const storage = disk(), request = storage.request();
    const result = storage.result(request, status, {summary: '新增 2 篇，完成 6/6 类检查。'});
    const h = controller(storage);
    await h.api.refresh();
    assert.equal(h.status.status, status);
    assert.equal(h.status.message, result.message);
    assert.equal(h.status.summary, result.summary);
    assert.equal(h.status.updatedAt, result.updated_at);
    assert.equal(h.status.busy, status === 'running');
    assert.equal(h.status.canRefresh, ['partial', 'complete'].includes(status));
    assert.equal(storage.writes.length, 0);
    const reopened = controller(storage);
    await reopened.api.refresh();
    assert.deepEqual(reopened.status, h.status);
  }
});

test('refresh tracks a saved request from waiting through running to partial without writing results', async () => {
  const h = controller();
  await h.api.submit();
  const request = JSON.parse(h.storage.writes[0].content);
  assert.equal(h.status.status, 'waiting');
  h.storage.result(request, 'running');
  await h.api.refresh();
  assert.equal(h.status.status, 'running');
  h.storage.result(request, 'partial', {summary: '完成 2/6 类检查；未完成类别保留断点。'});
  await h.api.refresh();
  assert.equal(h.status.status, 'partial');
  assert.equal(h.status.busy, false);
  assert.equal(h.storage.writes.length, 1);
  assert.ok(h.storage.writes.every(write => write.path.startsWith(REQUESTS)));
  assert.ok(!h.events.some(event => event.status === 'complete'));
});

test('a terminal failure allows one new request without overwriting earlier request or result', async () => {
  const storage = disk(), previous = storage.request();
  storage.result(previous, 'failed');
  const oldRequest = storage.files.get(REQUESTS + previous.id + '.json');
  const oldResult = storage.files.get(RESULTS + previous.id + '.json');
  const h = controller(storage);
  await h.api.refresh();
  await h.api.submit();
  assert.equal(storage.writes.length, 1);
  assert.notEqual(JSON.parse(storage.writes[0].content).id, previous.id);
  assert.equal(storage.files.get(REQUESTS + previous.id + '.json'), oldRequest);
  assert.equal(storage.files.get(RESULTS + previous.id + '.json'), oldResult);
  assert.equal(h.status.status, 'waiting');
});

test('stale requests remain pending and explicitly avoid claiming that intake started', async () => {
  const storage = disk();
  storage.request({requested_at: '2000-01-01T00:00:00Z'});
  const h = controller(storage);
  await h.api.refresh();
  assert.equal(h.status.status, 'waiting');
  assert.equal(h.status.busy, true);
  assert.match(h.status.message, /尚未确认开始/);
  await h.api.submit();
  assert.equal(storage.writes.length, 0);
});

test('disconnected refresh is honest and disposal removes recurring refresh handlers', async () => {
  const h = controller(disk(), {connected: false});
  await h.api.refresh();
  assert.equal(h.status.status, 'disconnected');
  assert.equal(h.storage.writes.length, 0);
  assert.equal(h.timers.size, 1);
  assert.equal(h.listeners.has('focus'), true);
  h.api.dispose();
  assert.equal(h.timers.size, 0);
  assert.equal(h.listeners.has('focus'), false);
  const count = h.events.length;
  await h.api.refresh();
  assert.equal(h.events.length, count);
});
