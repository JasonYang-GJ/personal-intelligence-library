// Behavioral tests with an in-memory File System Access implementation.
// These verify controller behavior, not browser picker/permission support.
import assert from 'node:assert/strict';
import {webcrypto, randomUUID, createHash} from 'node:crypto';
import fs from 'node:fs';
import {spawnSync} from 'node:child_process';
import test from 'node:test';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../ui/management.js', import.meta.url), 'utf8');
const PROJECT = '00000000-0000-4000-8000-000000000005';
const RECORD = 'intel-20261018-001';
const REVIEW = 'review-20261025';
const canonical = value => JSON.stringify(value, null, 2) + '\n';
const sha = bytes => createHash('sha256').update(bytes).digest('hex');
const plain = value => JSON.parse(JSON.stringify(value));
const missing = () => Object.assign(new Error('Not found'), {name: 'NotFoundError'});

function disk(projectId = PROJECT) {
  const files = new Map(), writes = [], accesses = [];
  const state = {permission: 'granted', permissionResponse: null, permissionRequests: 0, projectId,
    failWrite: false, corruptClose: false, closedThenThrows: false};
  function fileHandle(name) {
    return {
      kind: 'file',
      async getFile() {
        // File is a snapshot, as with FileSystemFileHandle.getFile().
        const content = files.get(name), bytes = Buffer.from(content);
        return {size: bytes.length, text: async () => content,
          arrayBuffer: async () => bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength)};
      },
      async createWritable() {
        let pending = '';
        return {
          async write(value) {
            if (state.failWrite) throw new Error('Simulated disk write failure');
            pending = value;
          },
          async close() {
            const value = state.corruptClose ? pending + 'unexpected bytes' : pending;
            files.set(name, value);
            writes.push({path: 'data/management/events/' + name, value});
            if (state.closedThenThrows) throw new Error('Close acknowledgement lost');
          },
          async abort() {},
        };
      },
    };
  }
  const events = {
    async *entries() { for (const name of [...files.keys()]) yield [name, fileHandle(name)]; },
    async getFileHandle(name, options) {
      assert.match(name, /^[0-9a-f-]+\.json$/);
      accesses.push('data/management/events/' + name);
      if (!files.has(name)) {
        if (!options?.create) throw missing();
        files.set(name, '');
      }
      return fileHandle(name);
    },
  };
  const management = {
    async getFileHandle(name) {
      assert.equal(name, 'project.json');
      accesses.push('data/management/project.json');
      return {getFile: async () => ({text: async () => JSON.stringify({schema_version: 1, project_id: state.projectId})})};
    },
    async getDirectoryHandle(name) { assert.equal(name, 'events'); return events; },
  };
  const root = {
    async queryPermission(options) { assert.equal(options.mode, 'readwrite'); return state.permission; },
    async requestPermission(options) {
      assert.equal(options.mode, 'readwrite');
      state.permissionRequests++;
      state.permission = state.permissionResponse ?? state.permission;
      return state.permission;
    },
    async getDirectoryHandle(name) {
      assert.equal(name, 'data');
      return {async getDirectoryHandle(child) { assert.equal(child, 'management'); return management; }};
    },
  };
  return {root, files, writes, accesses, state};
}

async function controller(storage = disk()) {
  const statuses = [], snapshots = [], listeners = {}, pickerCalls = [];
  let chosen = storage.root, nextPause = null;
  const context = {
    URL, TextEncoder, TextDecoder, Date, Math,
    location: {href: 'file:///test-project/library.html'},
    window: {addEventListener(type, callback) { listeners[type] = callback; }},
    document: {hidden: false, addEventListener(type, callback) { listeners[type] = callback; }},
    showDirectoryPicker: async options => { pickerCalls.push(options); return chosen; },
    crypto: {
      randomUUID,
      subtle: {async digest(algorithm, bytes) {
        if (nextPause && bytes.byteLength === 0) {
          const pause = nextPause;
          nextPause = null;
          pause.enter();
          await pause.blocked;
        }
        return webcrypto.subtle.digest(algorithm, bytes);
      }},
    },
  };
  context.globalThis = context;
  vm.createContext(context);
  vm.runInContext(source, context, {filename: 'ui/management.js'});
  const api = context.window.IntelManagement.create({
    library: {records: [{id: RECORD}], weekly_reviews: [{id: REVIEW}],
      management: {project_id: PROJECT, states: {}, event_count: 0, empty_files: []}},
    onStatus(value) { statuses.push(plain(value)); },
    onChange(value) { snapshots.push(plain(value)); },
  });
  await api.ready;
  return {api, storage, statuses, snapshots, listeners, pickerCalls,
    pick(other) { chosen = other.root; },
    pauseNextEmptyDigest() {
      let enter, release;
      const entered = new Promise(resolve => { enter = resolve; });
      const blocked = new Promise(resolve => { release = resolve; });
      nextPause = {enter, blocked};
      return {entered, release};
    },
  };
}

test('default folder is chosen once and repeated use reuses the authorized handle', async () => {
  const h = await controller();
  await h.api.useDefault();
  assert.equal(h.pickerCalls.length, 1);
  assert.equal(h.api.getRoot(), h.storage.root);
  await h.api.useDefault();
  await h.api.useDefault();
  assert.equal(h.pickerCalls.length, 1);
  assert.equal(h.api.getRoot(), h.storage.root);
  assert.equal(h.statuses.at(-1).connected, true);
  assert.equal(h.storage.writes.length, 0);
});

test('default folder requests renewed permission synchronously and never opens another picker', async () => {
  const h = await controller();
  await h.api.connect();
  h.storage.state.permission = 'prompt';
  await h.api.refresh();
  assert.equal(h.api.getRoot(), null);
  h.storage.state.permissionResponse = 'granted';
  const reconnecting = h.api.useDefault();
  // Permission must be requested during the click, before any asynchronous read.
  assert.equal(h.storage.state.permissionRequests, 1);
  await reconnecting;
  assert.equal(h.pickerCalls.length, 1);
  assert.equal(h.api.getRoot(), h.storage.root);
  assert.equal(h.statuses.at(-1).connected, true);
  assert.equal(h.storage.writes.length, 0);
});

test('declining remembered-folder permission never opens a picker or permits writes', async () => {
  const h = await controller();
  await h.api.connect();
  h.storage.state.permission = 'prompt';
  await h.api.refresh();
  h.storage.state.permissionResponse = 'denied';
  await h.api.useDefault();
  assert.equal(h.pickerCalls.length, 1);
  assert.equal(h.storage.state.permissionRequests, 1);
  assert.equal(h.api.getRoot(), null);
  assert.equal(h.statuses.at(-1).connected, false);
  assert.ok(h.statuses.some(value => ['warning', 'error'].includes(value.level) &&
    /权限|授权|允许/.test(value.message) && /未提交|没有提交/.test(value.message)));
  await assert.rejects(h.api.change(RECORD, 'archive'), /连接资料文件夹/);
  assert.equal(h.storage.files.size, 0);
  assert.equal(h.storage.writes.length, 0);
});

test('default folder validates project identity again before exposing the handle', async () => {
  const h = await controller();
  await h.api.connect();
  h.storage.state.projectId = '00000000-0000-4000-8000-000000000004';
  await h.api.useDefault();
  assert.equal(h.pickerCalls.length, 1);
  assert.equal(h.api.getRoot(), null);
  assert.equal(h.statuses.at(-1).connected, false);
  assert.ok(h.statuses.some(value => value.level === 'error' && /不是当前情报库文件夹/.test(value.message)));
  await assert.rejects(h.api.change(RECORD, 'archive'), /连接资料文件夹/);
  assert.equal(h.storage.writes.length, 0);
});

test('explicit reconnect still opens a picker even when a default handle is available', async () => {
  const h = await controller();
  await h.api.useDefault();
  await h.api.connect();
  assert.equal(h.pickerCalls.length, 2);
  assert.equal(h.api.getRoot(), h.storage.root);
});

test('archive, trash, and restore retain the original position and survive reopening', async () => {
  const h = await controller();
  await h.api.connect();
  await h.api.change(RECORD, 'archive');
  assert.equal(h.api.getState(RECORD).state, 'archived');
  await h.api.change(RECORD, 'trash');
  assert.equal(h.api.getState(RECORD).state, 'trashed');
  assert.equal(h.api.getState(RECORD).restore_to, 'archived');
  await h.api.change(RECORD, 'restore');
  assert.equal(h.api.getState(RECORD).state, 'archived');
  await h.api.change(RECORD, 'unarchive');
  await h.api.change(REVIEW, 'trash');
  await h.api.change(REVIEW, 'restore');
  assert.equal(h.api.getState(REVIEW).state, 'active');
  assert.equal(h.storage.files.size, 6);
  assert.equal(h.storage.writes.length, 6);
  assert.equal(new Set(h.storage.writes.map(value => value.path)).size, 6);
  const reopened = await controller(h.storage);
  await reopened.api.connect();
  assert.equal(reopened.api.getState(RECORD).state, 'active');
  assert.equal(reopened.api.getState(REVIEW).state, 'active');
  assert.equal(reopened.snapshots.at(-1).event_count, 6);
  assert.ok(h.storage.accesses.every(path => path.startsWith('data/management/')));
});

test('an in-flight focus refresh blocks writes; retry preserves the later archive', {timeout: 3000}, async () => {
  const h = await controller();
  await h.api.connect();
  const pause = h.pauseNextEmptyDigest();
  const refreshing = h.api.refresh();
  try {
    await pause.entered;
    await assert.rejects(h.api.change(RECORD, 'archive'), /请等当前整理操作/);
    // Focus and visibility can both fire; a second refresh must not reenter.
    await h.api.refresh();
    assert.equal(h.storage.writes.length, 0);
  } finally { pause.release(); }
  await refreshing;
  await h.api.change(RECORD, 'archive');
  assert.equal(h.api.getState(RECORD).state, 'archived');
  assert.equal(h.snapshots.at(-1).states[RECORD].state, 'archived');
  assert.equal(h.storage.writes.length, 1);
  assert.equal(h.statuses.at(-1).busy, false);
});

test('two pages append independent events and converge after refreshing', async () => {
  const storage = disk(), first = await controller(storage), second = await controller(storage);
  await first.api.connect();
  await second.api.connect();
  await Promise.all([first.api.change(RECORD, 'archive'), second.api.change(RECORD, 'trash')]);
  assert.equal(storage.files.size, 2);
  assert.equal(storage.writes.length, 2);
  assert.notEqual(storage.writes[0].path, storage.writes[1].path);
  await first.api.refresh();
  await second.api.refresh();
  assert.deepEqual(plain(first.api.getState(RECORD)), plain(second.api.getState(RECORD)));
  assert.equal(first.snapshots.at(-1).event_count, 2);
  assert.equal(first.snapshots.at(-1).fingerprint, second.snapshots.at(-1).fingerprint);
});

test('wrong project and denied/revoked permission never create an event', async () => {
  const h = await controller(), wrong = disk('00000000-0000-4000-8000-000000000004');
  await h.api.connect();
  h.pick(wrong);
  await h.api.connect();
  assert.equal(h.statuses.at(-1).connected, false);
  await assert.rejects(h.api.change(RECORD, 'archive'), /连接资料文件夹/);
  assert.equal(wrong.files.size, 0);
  h.pick(h.storage);
  h.storage.state.permission = 'denied';
  await h.api.connect();
  assert.equal(h.statuses.at(-1).connected, false);
  await assert.rejects(h.api.change(RECORD, 'archive'), /连接资料文件夹/);
  h.storage.state.permission = 'granted';
  await h.api.connect();
  h.storage.state.permission = 'denied';
  await assert.rejects(h.api.change(RECORD, 'archive'), /授权已失效/);
  assert.equal(h.storage.files.size, 0);
  assert.equal(h.storage.writes.length, 0);
});

test('failed writes leave an incomplete event, never a success or changed state', async () => {
  const h = await controller();
  await h.api.connect();
  h.statuses.length = 0;
  h.storage.state.failWrite = true;
  await assert.rejects(h.api.change(RECORD, 'archive'), /disk write failure/);
  assert.equal(h.api.getState(RECORD).state, 'active');
  assert.ok(h.statuses.some(value => value.level === 'error'));
  assert.ok(!h.statuses.some(value => value.level === 'success'));
  assert.equal(h.storage.writes.length, 0);
  assert.deepEqual([...h.storage.files.values()], ['']);
  await h.api.refresh();
  assert.equal(h.snapshots.at(-1).empty_files.length, 1);
  assert.equal(h.snapshots.at(-1).event_count, 0);
  h.storage.state.failWrite = false;
  await h.api.change(RECORD, 'archive');
  assert.equal(h.api.getState(RECORD).state, 'archived');
});

test('a successful close with mismatched readback is not reported as saved', async () => {
  const h = await controller();
  await h.api.connect();
  h.statuses.length = 0;
  h.storage.state.corruptClose = true;
  await assert.rejects(h.api.change(RECORD, 'archive'), /回读不一致/);
  assert.equal(h.api.getState(RECORD).state, 'active');
  assert.ok(!h.statuses.some(value => value.level === 'success'));
});

test('lost close acknowledgement is successful only when exact bytes were persisted', async () => {
  const h = await controller();
  await h.api.connect();
  h.storage.state.closedThenThrows = true;
  await h.api.change(RECORD, 'archive');
  assert.equal(h.api.getState(RECORD).state, 'archived');
  assert.equal(h.snapshots.at(-1).event_count, 1);
  assert.ok(h.statuses.some(value => value.level === 'success' && /已保存到本机/.test(value.message)));
});

test('a malformed disk event blocks writes and preserves the last known snapshot', async () => {
  const h = await controller();
  await h.api.connect();
  await h.api.change(RECORD, 'archive');
  const [name, content] = [...h.storage.files][0];
  h.storage.files.set(name, content + 'broken');
  await h.api.refresh();
  assert.equal(h.statuses.at(-1).connected, false);
  assert.equal(h.api.getState(RECORD).state, 'archived');
  await assert.rejects(h.api.change(RECORD, 'trash'), /连接资料文件夹/);
  assert.equal(h.storage.writes.length, 1);
});

test('event bytes and snapshot fingerprint agree with Python canonical formatting', async t => {
  const h = await controller();
  await h.api.connect();
  await h.api.change(RECORD, 'archive');
  await h.api.change(REVIEW, 'trash');
  const sorted = [...h.storage.files].sort(([a], [b]) => a.localeCompare(b, 'en'));
  const fingerprint = sha(sorted.map(([name, text]) => name + ':' + sha(text)).join('\n'));
  assert.equal(h.snapshots.at(-1).fingerprint, fingerprint);
  for (const [, text] of sorted) assert.equal(text, canonical(JSON.parse(text)));
  const pythonCandidates = [process.env.PYTHON, 'python3', 'python'].filter(Boolean);
  const script = 'import json,sys,hashlib\nsys.stdin.reconfigure(encoding="utf-8")\nsys.stdout.reconfigure(encoding="utf-8")\nitems=json.load(sys.stdin)\nfor name,raw in items:\n assert json.dumps(json.loads(raw),ensure_ascii=False,indent=2)+"\\n" == raw\nprint(hashlib.sha256("\\n".join(name+":"+hashlib.sha256(raw.encode()).hexdigest() for name,raw in sorted(items)).encode()).hexdigest())\n';
  let python;
  for (const candidate of pythonCandidates) {
    const result = spawnSync(candidate, ['-B', '-c', script], {input: JSON.stringify(sorted), encoding: 'utf8', windowsHide: true, timeout: 10000});
    if (result.error?.code === 'ENOENT') continue;
    python = result;
    break;
  }
  if (!python) { t.diagnostic('Python unavailable; JS byte/fingerprint checks passed, cross-language check skipped.'); return; }
  assert.equal(python.status, 0, python.stderr || String(python.error || 'Python failed'));
  assert.equal(python.stdout.trim(), fingerprint);
});
