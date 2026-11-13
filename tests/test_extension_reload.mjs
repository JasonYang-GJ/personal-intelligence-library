import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';

const script = await readFile(new globalThis.URL('../browser-extension/collector-init.js', import.meta.url), 'utf8');
const EXTENSION_ID = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';
const RUN_ID = 'extension-probe-000000000000000000000001';
const URL = `chrome-extension://${EXTENSION_ID}/collector.html`;
const clone = value => structuredClone(value);

function fixture(options = {}) {
  let now = 0, reloads = 0, reads = 0, windowReads = 0;
  const actions = [], diagnostics = [];
  const owned = {runId: RUN_ID, tabId: 7, windowId: 8, url: URL};
  const storage = {activeRun: RUN_ID, owned: clone(owned), ...clone(options.storage || {})};
  const current = {id: 7, windowId: 8, url: URL, ...clone(options.tab || {})};
  const win = {id: 8, state: 'minimized', focused: false, tabs: [clone(current)], ...clone(options.window || {})};
  const location = {href: URL, pathname: '/collector.html', hash: '', search: '', ...clone(options.location || {})};
  const chrome = {
    runtime: {id: options.extensionId || EXTENSION_ID, async sendNativeMessage(host, message) {
      assert.equal(host, 'com.personal_intel.collector'); assert.equal(message.run_id, RUN_ID);
      diagnostics.push(clone(message));
      if (options.diagnosticFailure) throw new Error('diagnostics_unavailable');
      if (options.pendingDiagnostic) await new Promise(() => {});
      return {ok: true};
    }, reload() {
      assert.equal(storage.collectorReloadConsumed, RUN_ID, 'consumption must be durable before reload');
      actions.push('reload'); reloads++;
    }},
    storage: {local: {
      async get() {
        reads++;
        if (options.ownedAfterReads && reads < options.ownedAfterReads) return {...clone(storage), owned: null};
        if (options.delayRead) now += options.delayRead;
        return clone(storage);
      },
      async set(values) {
        if (options.storageFailure) throw new Error('storage_unavailable');
        Object.assign(storage, clone(values)); actions.push('consume');
        options.afterConsume?.({storage, current, win, location});
      },
    }},
    tabs: {async getCurrent() { return clone(current); }},
    windows: {async get(id, params) {
      assert.equal(id, current.windowId); assert.equal(params.populate, true); windowReads++;
      if (options.minimizeAfterWindowReads && windowReads >= options.minimizeAfterWindowReads) win.state = 'minimized';
      if (options.minimizeAtMs != null && now >= options.minimizeAtMs) win.state = 'minimized';
      return clone(win);
    }},
  };
  const context = vm.createContext({chrome, location, performance: {now: () => now},
    setTimeout(callback, delay) {
      now += delay; options.onTimeAdvance?.({now, storage, current, win, location});
      queueMicrotask(callback); return 1;
    }});
  return {storage, current, win, location, actions, diagnostics, run: () => vm.runInContext(script, context),
    get reloads() { return reloads; }, get elapsed() { return now; }, get reads() { return reads; }};
}

test('fixed owned minimized singleton shell consumes approval before exactly one extension reload', async () => {
  const f = fixture();
  assert.equal((await f.run()).status, 'reloaded');
  assert.deepEqual(f.actions, ['consume', 'reload']); assert.equal(f.reloads, 1);
  assert.equal((await f.run()).status, 'already_consumed'); assert.equal(f.reloads, 1);
});

test('short polling can observe owned storage appearing after shell creation', async () => {
  const f = fixture({ownedAfterReads: 3});
  assert.equal((await f.run()).status, 'reloaded'); assert.equal(f.reloads, 1);
  assert.equal(f.elapsed, 40);
});

test('matching singleton shell waits briefly for its minimized state without changing window state', async () => {
  const f = fixture({window: {state: 'normal'}, minimizeAfterWindowReads: 3});
  assert.equal((await f.run()).status, 'reloaded'); assert.equal(f.reloads, 1);
  assert.equal(f.elapsed, 40); assert.deepEqual(f.actions, ['consume', 'reload']);
});

test('wrong extension, report URL, hash, or query never reads state or reloads', async () => {
  for (const options of [
    {extensionId: 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'},
    {location: {href: 'https://chatgpt.com/c/private', pathname: '/c/private'}},
    {location: {href: URL + '#personal-intel-probe=other', hash: '#personal-intel-probe=other'}},
    {location: {href: URL + '?other', search: '?other'}},
  ]) {
    const f = fixture(options); assert.equal((await f.run()).status, 'ignored');
    assert.equal(f.reads, 0); assert.equal(f.reloads, 0);
  }
});

test('unrelated run or mismatched owned tab cannot reload and polling stays bounded', async () => {
  for (const options of [
    {storage: {activeRun: 'other'}},
    {storage: {owned: {runId: 'other', tabId: 7, windowId: 8, url: URL}}},
    {storage: {owned: {runId: RUN_ID, tabId: 99, windowId: 8, url: URL}}},
    {storage: {owned: {runId: RUN_ID, tabId: 7, windowId: 8, url: URL + '#other'}}},
    {tab: {pendingUrl: 'https://chatgpt.com/'}},
  ]) {
    const f = fixture(options); await f.run();
    assert.equal(f.reloads, 0); assert.equal(f.actions.length, 0); assert.ok(f.elapsed <= 1500); assert.ok(f.reads <= 75);
    assert.equal(f.elapsed, 0, 'explicit identity mismatch stops immediately');
  }
});

test('focused, normal, or multiple-tab windows are never reloaded', async () => {
  for (const window of [{focused: true}, {state: 'normal'},
    {tabs: [{id: 7, windowId: 8, url: URL}, {id: 9, windowId: 8, url: 'https://example.test'}]}]) {
    const f = fixture({window}); await f.run(); assert.equal(f.reloads, 0); assert.equal(f.actions.length, 0);
  }
});

test('focus, navigation, or ownership changes after consuming cancel reload permanently', async () => {
  for (const afterConsume of [
    ({win}) => { win.focused = true; },
    ({current}) => { current.url = 'https://chatgpt.com/'; },
    ({storage}) => { storage.owned.tabId = 99; },
    ({location}) => { location.hash = '#changed'; location.href += '#changed'; },
  ]) {
    const f = fixture({afterConsume});
    assert.equal((await f.run()).status, 'changed_after_consume'); assert.equal(f.reloads, 0);
    assert.deepEqual(f.actions, ['consume']); assert.equal(f.storage.collectorReloadConsumed, RUN_ID);
    await f.run(); assert.equal(f.reloads, 0);
  }
});

test('storage failure or API response beyond 1500ms cannot cause a late reload', async () => {
  const failure = fixture({storageFailure: true});
  assert.equal((await failure.run()).status, 'unavailable'); assert.equal(failure.reloads, 0);
  const late = fixture({delayRead: 1501}); await late.run();
  assert.equal(late.reloads, 0); assert.equal(late.actions.length, 0); assert.equal(late.reads, 1);
});

test('a matching window minimized at 400ms can reload but minimization after 1500ms cannot', async () => {
  const timely = fixture({window: {state: 'normal'}, minimizeAtMs: 400});
  assert.equal((await timely.run()).status, 'reloaded');
  assert.equal(timely.elapsed, 400); assert.equal(timely.reloads, 1);
  const late = fixture({window: {state: 'normal'}, minimizeAtMs: 1501});
  assert.equal((await late.run()).status, 'no_matching_owned_shell');
  assert.equal(late.elapsed, 1500); assert.equal(late.reloads, 0); assert.equal(late.actions.length, 0);
});

test('navigation before delayed minimization immediately ends polling without reload', async () => {
  const f = fixture({window: {state: 'normal'}, minimizeAtMs: 400,
    onTimeAdvance({now, current, location}) {
      if (now === 300) { current.url = 'https://chatgpt.com/'; location.href = current.url; location.pathname = '/'; }
    }});
  assert.equal((await f.run()).status, 'identity_mismatch');
  assert.equal(f.elapsed, 300); assert.equal(f.reloads, 0); assert.equal(f.actions.length, 0);
  assert.equal(f.diagnostics.length, 0, 'no diagnostics after leaving the exact collector shell');
});

test('diagnostics contain only fixed reasons and cannot delay or authorize reload', async () => {
  const pending = fixture({pendingDiagnostic: true});
  assert.equal((await pending.run()).status, 'reloaded'); assert.equal(pending.reloads, 1);
  assert.deepEqual(pending.diagnostics[0], {op: 'observe', run_id: RUN_ID, event: 'phase',
    meta: {phase: 'reload_bootstrap', detail: 'reload_requested'}});
  const unavailable = fixture({diagnosticFailure: true});
  assert.equal((await unavailable.run()).status, 'reloaded'); assert.equal(unavailable.reloads, 1);
  const unrelated = fixture({storage: {activeRun: 'other'}}); await unrelated.run();
  assert.equal(unrelated.reloads, 0); assert.equal(unrelated.diagnostics.length, 0);
});
