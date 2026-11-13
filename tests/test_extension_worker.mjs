/**
 * Local logic regression tests using a fake Chrome API and in-memory storage.
 * These are NOT live Chrome, login, restart, foreground, or clipboard acceptance.
 * Run: node --test tests/test_extension_worker.mjs
 */
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const workerText = readFileSync(new URL('../browser-extension/worker.js', import.meta.url), 'utf8');
const RUN_ID = 'extension-probe-worker-test';
const EXTENSION_ID = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';
const SHELL_URL = `chrome-extension://${EXTENSION_ID}/collector.html`;
const RECENT = '2026-10-17T12:00:00+08:00';
const OLDER = '2026-10-15T12:00:00+08:00';
const sourceRows = [
  ['ai-models', 'AI模型新发布', '00000000-0000-4000-8000-000000000010'],
  ['ai-products', 'AI产品机会雷达', '00000000-0000-4000-8000-000000000008'],
  ['platform-income', '平台变现机会雷达', '00000000-0000-4000-8000-000000000009'],
  ['user-pains', '真实用户痛点雷达', '00000000-0000-4000-8000-000000000007'],
  ['solo-business', '个人创业机会雷达', '00000000-0000-4000-8000-000000000011'],
  ['cross-border', '跨境电商入场雷达', '00000000-0000-4000-8000-000000000012']
];
const sources = sourceRows.map(([category_id, task_name, conversation_id]) =>
  ({category_id, task_name, conversation_id}));
const clone = value => structuredClone(value);
const urlFor = source => `https://chatgpt.com/c/${source.conversation_id}#personal-intel-probe=${RUN_ID}`;

function eventEmitter() {
  const listeners = [];
  return {addListener: fn => listeners.push(fn), emit: (...args) => listeners.forEach(fn => fn(...args))};
}

function snapshot(source, {boundary = true, notices = false} = {}) {
  const report = {
    turn_key: `${source.category_id}-report`, is_report: true, is_pause_notice: false,
    assistant_count: 1, report_time: RECENT, has_copy: true, text_length: 120,
    state: 'complete'
  };
  const turns = [report];
  if (notices) turns.unshift(
    {turn_key: `${source.category_id}-prompt`, is_report: false, is_pause_notice: false,
      assistant_count: 0, report_time: RECENT, has_copy: false, text_length: 0},
    {turn_key: `${source.category_id}-pause`, is_report: false, is_pause_notice: true,
      assistant_count: 1, report_time: RECENT, has_copy: false, text_length: 90}
  );
  if (boundary) turns.unshift({turn_key: `${source.category_id}-older`, is_report: true,
    is_pause_notice: false, assistant_count: 1, report_time: OLDER, has_copy: true,
    text_length: 90, state: 'complete'});
  return {ok: true, title: source.task_name, path: `/c/${source.conversation_id}`,
    turns, dates: turns.map(turn => turn.report_time), loading: false,
    viewport: {width: 1000, height: 800, visibility: 'visible'}, atStartEvidence: {complete: false}};
}

function createHarness(options = {}) {
  let clock = Date.parse('2026-10-18T12:00:00+08:00');
  let nextId = 100;
  const storage = clone(options.storage || {});
  const nativeCalls = [], browserActions = [], captures = [], scriptCalls = [], timeline = [];
  const windows = new Map(), tabs = new Map(), categories = new Map();
  let hostStatus = options.hostStatus || 'pending';
  const scrollCounts = new Map();
  const focusEvent = eventEmitter();
  const plan = {ok: true, status: 'pending', run_id: RUN_ID,
    scan_start: '2026-10-16T00:00:00+08:00', scan_end: '2026-10-18T12:00:00+08:00',
    sources: clone(sources), completed_categories: [], ...clone(options.plan || {})};
  for (const category of plan.completed_categories) categories.set(category, {complete: true});
  const getSource = expected => sources.find(source => source.conversation_id === expected.conversation_id);
  function seedOwned({focused = false, state = 'minimized'} = {}) {
    const saved = storage.owned;
    assert.ok(saved, 'seedOwned requires a saved owned identity');
    const tab = {id: saved.tabId, windowId: saved.windowId, url: saved.url};
    tabs.set(tab.id, tab);
    windows.set(saved.windowId, {id: saved.windowId, focused, state, tabs: [tab]});
  }
  if (options.existingOwned) seedOwned(options.existingOwned);
  const stats = {storage, nativeCalls, browserActions, captures, categories, scrollCounts,
    windows, tabs, scriptCalls, timeline, plan, focusedAtActionCount: null,
    emitFocus: id => focusEvent.emit(id ?? storage.owned.windowId)};
  const chrome = {
    runtime: {
      id: EXTENSION_ID,
      getURL: path => `chrome-extension://${EXTENSION_ID}/${path}`,
      onInstalled: eventEmitter(), onStartup: eventEmitter(), onMessage: eventEmitter(),
      async sendNativeMessage(host, message) {
        assert.equal(host, 'com.personal_intel.collector');
        nativeCalls.push(clone(message));
        const override = await options.native?.(message, stats);
        if (override !== undefined) return override;
        if (message.op === 'get_plan') return clone(plan);
        if (message.op === 'status') return {ok: true, run_id: RUN_ID, status: hostStatus,
          completed_categories: sources.filter(source => categories.get(source.category_id)?.complete === true)
            .map(source => source.category_id)};
        assert.equal(message.run_id, RUN_ID);
        if (message.op === 'observe') {
          if (options.preOpenDelay && message.event === 'phase' && message.meta.phase === 'reading' && !windows.size) clock += options.preOpenDelay;
          // This diagnostic endpoint has no coverage or browser-state effects.
          timeline.push({op: 'observe', event: message.event, meta: clone(message.meta)});
          return {ok: true};
        }
        if (message.op === 'save_report') {
          assert.equal(typeof message.report.is_report, 'boolean', 'host requires an explicit report classification');
          return {ok: true, status: message.report.is_report ? 'saved' : 'skipped'};
        }
        if (message.op === 'finish_category') {
          const allowed = ['complete', 'reason', 'boundary_time', 'dates', 'page_count', 'error', 'reached_start'];
          assert.ok(Object.keys(message.coverage).every(key => allowed.includes(key)), 'coverage must match the native host protocol');
          categories.set(message.category_id, clone(message.coverage));
          return {ok: true, complete: message.coverage.complete};
        }
        if (message.op === 'finish_run') {
          hostStatus = sources.every(source => categories.get(source.category_id)?.complete === true)
            ? 'complete' : 'partial';
          return {ok: true, status: hostStatus, run_id: RUN_ID,
            completed_categories: sources.filter(source => categories.get(source.category_id)?.complete === true)
              .map(source => source.category_id)};
        }
        throw new Error(`unexpected operation ${message.op}`);
      }
    },
    alarms: {async get() { return {name: 'personal-intel-probe'}; },
      async create() {}, onAlarm: eventEmitter()},
    action: {async setBadgeText() {}},
    storage: {local: {
      async get(keys) {
        if (keys == null) return clone(storage);
        const names = typeof keys === 'string' ? [keys] : keys;
        return clone(Object.fromEntries(names.filter(key => key in storage).map(key => [key, storage[key]])));
      },
      async set(value) { Object.assign(storage, clone(value)); await options.stored?.(value, stats); },
      async remove(keys) { for (const key of Array.isArray(keys) ? keys : [keys]) delete storage[key]; }
    }},
    windows: {
      onFocusChanged: focusEvent,
      async create(properties) {
        browserActions.push({op: 'create', ...clone(properties)});
        timeline.push({op: 'create', ...clone(properties)});
        assert.equal(properties.focused, false);
        assert.equal(properties.state, plan.diagnostic ? 'normal' : 'minimized');
        assert.equal(properties.url, SHELL_URL + (plan.diagnostic ? `#personal-intel-probe=${RUN_ID}` : ''), 'initial window must contain only the local blank collector shell');
        const windowId = nextId++, tabId = nextId++;
        const tab = {id: tabId, windowId, url: properties.url};
        const win = {id: windowId, focused: options.createdFocused ?? false,
          state: options.createdState || (plan.diagnostic ? 'normal' : 'minimized'), tabs: [tab]};
        tabs.set(tabId, tab); windows.set(windowId, win);
        return clone(win);
      },
      async update(id, properties) {
        browserActions.push({op: 'window-update', id, ...clone(properties)});
        timeline.push({op: 'window-update', id, ...clone(properties)});
        assert.ok(windows.has(id), 'must only minimize an existing own window');
        assert.equal(Object.hasOwn(properties, 'focused'), false, 'minimization must not request any focus change');
        assert.deepEqual(clone(properties), {state: 'minimized'});
        if (!options.ignoreMinimize) Object.assign(windows.get(id), properties);
        return clone(windows.get(id));
      },
      async get(id) {
        const override = options.windowGet?.(id, stats);
        if (override !== undefined) return override;
        if (!windows.has(id)) throw new Error('No window with id');
        timeline.push({op: 'window-get', id, state: windows.get(id).state, focused: windows.get(id).focused});
        return clone(windows.get(id));
      }
    },
    tabs: {
      async get(id) {
        if (!tabs.has(id)) throw new Error('No tab with id');
        return clone(tabs.get(id));
      },
      async update(id, properties) {
        browserActions.push({op: 'update', id, ...clone(properties)});
        timeline.push({op: 'update', id, ...clone(properties)});
        assert.ok(tabs.has(id), 'must only navigate an existing own tab');
        assert.equal(properties.active, undefined, 'must not activate the tab');
        const win = windows.get(tabs.get(id).windowId);
        assert.equal(win.state, plan.diagnostic ? 'normal' : 'minimized', 'no report navigation before the explicitly authorized state is effective');
        assert.equal(win.focused, false, 'must not navigate a focused collector');
        Object.assign(tabs.get(id), properties);
        return clone(tabs.get(id));
      },
      async remove(id) {
        browserActions.push({op: 'remove', id});
        timeline.push({op: 'remove', id});
        assert.ok(tabs.has(id), 'must only close an existing own tab');
        windows.delete(tabs.get(id).windowId); tabs.delete(id);
      }
    },
    scripting: {async executeScript({target, world, func, args}) {
      assert.equal(world, 'MAIN');
      assert.ok(tabs.has(target.tabId));
      scriptCalls.push({name: func.name, tabId: target.tabId});
      return [{result: await func(...args)}];
    }}
  };
  function focusOwned() {
    const owned = storage.owned;
    assert.ok(owned && windows.has(owned.windowId));
    windows.get(owned.windowId).focused = true;
    windows.get(owned.windowId).state = 'normal';
    stats.focusedAtActionCount = browserActions.length;
    timeline.push({op: 'user-focused', id: owned.windowId});
    focusEvent.emit(owned.windowId);
  }
  function inspectPage(expected) {
    const source = getSource(expected);
    assert.ok(source, 'reader must receive one of the six fixed sources');
    return options.inspect?.(source, stats) || snapshot(source, options.snapshotOptions);
  }
  function installClipboardGuard() { return {ok: true}; }
  function captureTurn(expected, turnKey) {
    const source = getSource(expected);
    captures.push({category: source.category_id, turnKey});
    if (options.focusOnCapture) focusOwned();
    const custom = options.capture?.(source, turnKey, stats);
    if (custom) return custom;
    return {ok: true, title: `${source.task_name} - ChatGPT`, path: `/c/${source.conversation_id}`,
      turn_key: turnKey, is_report: true, report_time: RECENT, text: '模拟报告正文',
      links: [], table_count: 0, guard: {copy_intercepted: 1, real_clipboard_calls: 0}};
  }
  function scrollOlder(expected, scrollOptions) {
    const source = getSource(expected);
    scrollCounts.set(source.category_id, (scrollCounts.get(source.category_id) || 0) + 1);
    return options.scroll?.(source, stats, scrollOptions) || {ok: true, moved: false};
  }
  class FakeDate extends Date {
    constructor(...args) { super(...(args.length ? args : [clock])); }
    static now() { return clock; }
  }
  const probeRendering = async () => ({ok: true, visibility: plan.diagnostic ? 'visible' : 'hidden', raf_fired: !!plan.diagnostic});
  const context = vm.createContext({chrome, inspectPage, installClipboardGuard, captureTurn, scrollOlder, probeRendering,
    Date: FakeDate, clearTimeout: () => {},
    setTimeout: (callback, ms) => { if (ms >= 55000) return 2; clock += ms; queueMicrotask(callback); return 1; }});
  const script = workerText.replace(/^import[^\n]+\n/gm, '') + '\n;globalThis.workerTestApi = {run};';
  new vm.Script(script, {filename: 'browser-extension/worker.js'}).runInContext(context);
  return Object.assign(stats, {run: event => context.workerTestApi.run(event)});
}

test('dated non-report prompts and pause notices do not block actual reports', async () => {
  const harness = createHarness({snapshotOptions: {notices: true}});
  await harness.run('alarm');
  assert.equal(harness.storage.result.status, 'complete');
  assert.equal(harness.categories.size, 6);
  assert.equal(harness.captures.length, 6);
  assert.ok(harness.captures.every(capture => capture.turnKey.endsWith('-report')));
  const saves = harness.nativeCalls.filter(call => call.op === 'save_report');
  assert.equal(saves.filter(call => call.report.is_report === false).length, 6);
  assert.equal(saves.filter(call => call.report.is_report === true).length, 6);
});

test('a report failure stays partial while other categories continue; the probe then stops', async () => {
  const harness = createHarness({capture: source => source.category_id === 'ai-products'
    ? {ok: false, error: 'copy_capture_incomplete'} : undefined});
  await harness.run('alarm');
  assert.equal(harness.categories.size, 6);
  assert.equal(harness.categories.get('ai-products').complete, false);
  assert.equal([...harness.categories.values()].filter(coverage => coverage.complete).length, 5);
  assert.equal(harness.storage.result.status, 'partial');
  assert.equal(harness.storage.status.phase, 'failed');
  const previousActionCount = harness.browserActions.length;
  await harness.run('alarm');
  assert.equal(harness.browserActions.length, previousActionCount, 'same failed probe must not reopen browser windows');
});

test('an exact undated platform notice is recorded as skipped while an unknown undated report fails', async () => {
  const harness = createHarness({inspect: source => {
    const result = snapshot(source);
    if (source.category_id === 'cross-border') result.turns.push({
      turn_key: 'platform-confirmation', is_report: false, is_task_notice: true,
      assistant_count: 1, report_time: null, state: 'complete'
    });
    if (source.category_id === 'ai-models') result.turns.push({
      turn_key: 'unknown-undated', is_report: true, assistant_count: 1,
      report_time: null, state: 'complete'
    });
    return result;
  }});
  await harness.run('alarm');
  assert.equal(harness.categories.get('cross-border').complete, true);
  assert.equal(harness.categories.get('ai-models').error, 'undated_report');
  assert.ok(harness.nativeCalls.some(call => call.op === 'save_report' &&
    call.report.turn_key === 'platform-confirmation' && call.report.is_report === false));
  assert.ok(!harness.captures.some(item => item.turnKey === 'platform-confirmation'));
  assert.equal(harness.storage.result.status, 'partial');
});

test('an unchanged history list without an older boundary never becomes complete', async () => {
  const harness = createHarness({snapshotOptions: {boundary: false}});
  await harness.run('alarm');
  assert.equal(harness.categories.size, 6);
  for (const coverage of harness.categories.values()) {
    assert.equal(coverage.complete, false);
    assert.equal(coverage.error, 'history_boundary_not_proven');
  }
  assert.equal(harness.storage.result.status, 'partial');
});

test('an undated older turn beyond the actual dated boundary cannot block the in-range report', async () => {
  const harness = createHarness({
    plan: {completed_categories: sources.slice(1).map(source => source.category_id)},
    inspect: source => {
      const page = snapshot(source);
      page.turns.unshift({turn_key: 'older-undated-report', is_report: true,
        assistant_count: 1, report_time: null, state: 'complete', text_length: 2454});
      return page;
    }
  });
  await harness.run('alarm');
  assert.deepEqual(harness.captures.map(item => item.turnKey), ['ai-models-report']);
  assert.equal(harness.categories.get('ai-models').boundary_time, OLDER);
  assert.equal(harness.categories.get('ai-models').complete, true);
  assert.ok(!harness.nativeCalls.some(call => call.meta?.issue === 'undated_report'));
});

test('an undated turn between the boundary and a recent report remains unresolved', async () => {
  const harness = createHarness({
    plan: {completed_categories: sources.slice(1).map(source => source.category_id)},
    inspect: source => {
      const page = snapshot(source);
      page.turns.splice(1, 0, {turn_key: 'possibly-in-range', is_report: true,
        assistant_count: 1, report_time: null, state: 'complete'});
      return page;
    }
  });
  await harness.run('alarm');
  assert.equal(harness.categories.get('ai-models').error, 'undated_report');
  assert.equal(harness.categories.get('ai-models').complete, false);
});

test('one dated anchor cannot justify skipping a leading undated report', async () => {
  const harness = createHarness({
    plan: {completed_categories: sources.slice(1).map(source => source.category_id)},
    inspect: source => ({...snapshot(source), turns: [
      {turn_key: 'unknown-undated', is_report: true, assistant_count: 1, report_time: null},
      {turn_key: 'old', is_report: true, assistant_count: 1, report_time: OLDER}
    ]})
  });
  await harness.run('alarm');
  assert.equal(harness.categories.get('ai-models').error, 'undated_report');
});

test('a sorted dates summary cannot conceal reversed DOM order or duplicate turn identities', async () => {
  for (const kind of ['reversed', 'duplicate']) {
    const harness = createHarness({
      plan: {completed_categories: sources.slice(1).map(source => source.category_id)},
      inspect: source => {
        const page = snapshot(source);
        if (kind === 'reversed') page.turns.reverse();
        else page.turns[1].turn_key = page.turns[0].turn_key;
        return page;
      }
    });
    await harness.run('alarm');
    assert.equal(harness.captures.length, 0);
    assert.equal(harness.categories.get('ai-models').error,
      kind === 'reversed' ? 'history_order_not_ascending' : 'ambiguous_turn_identity');
  }
});

test('a transient missing report date is rechecked without skipping the report or advancing coverage', async () => {
  let inspections = 0;
  const harness = createHarness({
    plan: {completed_categories: sources.slice(1).map(source => source.category_id)},
    inspect: source => {
      const page = snapshot(source);
      if (++inspections <= 3) {
        page.turns.find(t => t.turn_key === `${source.category_id}-report`).report_time = null;
        page.dates = [OLDER];
      }
      return page;
    }
  });
  await harness.run('alarm');
  assert.ok(inspections >= 4);
  assert.equal(harness.captures.length, 1);
  assert.equal(harness.categories.get('ai-models').complete, true);
  assert.ok(harness.nativeCalls.some(call => call.op === 'observe' &&
    call.meta.issue === 'undated_report'));
});

test('a stuck loader gets one normal scroll reentry and still cannot claim historical coverage', async () => {
  const harness = createHarness({
    plan: {completed_categories: sources.slice(1).map(source => source.category_id)},
    inspect: source => ({...snapshot(source, {boundary: false}), loading: true, scroll: {at_top: true}}),
    scroll: (source, stats, options) => {
      assert.deepEqual(clone(options), {reenterTop: true, notifyScroll: true});
      assert.equal(stats.storage.progress.historyReentered, true,
        'consume the one scroll notification before executing it');
      return {ok: true, reentered: true, moved: false, scroll_event_dispatched: true};
    }
  });
  await harness.run('alarm');
  assert.equal(harness.scrollCounts.get('ai-models'), 1);
  assert.equal(harness.categories.get('ai-models').complete, false);
  assert.equal(harness.categories.get('ai-models').error, 'history_loading_timeout_without_boundary');
  assert.equal(harness.storage.result.status, 'partial');
});

test('a delayed focus notification for an unfocused minimized collector does not abort intake', async () => {
  const harness = createHarness({inspect: (source, stats) => {
    stats.emitFocus();
    return snapshot(source);
  }});
  await harness.run('alarm');
  assert.equal(harness.storage.result.status, 'complete');
  assert.equal(harness.captures.length, 6);
  assert.equal(harness.storage.terminalResult, undefined);
  assert.equal(harness.windows.size, 0);
});

test('a confirmed focus event remains an interruption even if the window is minimized again', async () => {
  let emitted = false;
  const harness = createHarness({inspect: async (source, stats) => {
    if (!emitted) {
      emitted = true;
      const win = stats.windows.get(stats.storage.owned.windowId);
      win.focused = true;
      stats.emitFocus();
      await Promise.resolve();
      win.focused = false;
      stats.focusedAtActionCount = stats.browserActions.length;
    }
    return snapshot(source);
  }});
  await harness.run('alarm');
  assert.equal(harness.storage.result.status, 'partial');
  assert.equal(harness.captures.length, 0);
  assert.equal(harness.browserActions.length, harness.focusedAtActionCount);
  await harness.run('alarm');
  assert.equal(harness.browserActions.length, harness.focusedAtActionCount);
});

test('focus notifications from another window do not interrupt the collector', async () => {
  const harness = createHarness({inspect: (source, stats) => {
    stats.emitFocus(9999);
    return snapshot(source);
  }});
  await harness.run('alarm');
  assert.equal(harness.storage.result.status, 'complete');
  assert.equal(harness.captures.length, 6);
});

test('a pending actual focus query must finish before minimizing the newly created shell', async () => {
  let pending = false;
  const harness = createHarness({
    native: (message, stats) => {
      if (message.op !== 'observe' || message.event !== 'window_created') return;
      const win = stats.windows.get(stats.storage.owned.windowId);
      win.focused = true; win.state = 'normal';
      stats.focusedAtActionCount = stats.browserActions.length;
      pending = true;
      stats.emitFocus();
    },
    windowGet: (id, stats) => {
      if (!pending) return;
      pending = false;
      const observed = clone(stats.windows.get(id));
      return new Promise(resolve => setImmediate(() => resolve(observed)));
    }
  });
  await harness.run('alarm');
  assert.equal(harness.storage.result.status, 'partial');
  assert.equal(harness.browserActions.length, harness.focusedAtActionCount);
  assert.equal(harness.scriptCalls.length, 0);
});

test('focus during ownership persistence prevents initial and later report navigation', async () => {
  for (const sourceIndex of [0, 1]) {
    let triggered = false;
    const harness = createHarness({stored: (value, stats) => {
      if (triggered || value.owned?.url !== urlFor(sources[sourceIndex])) return;
      triggered = true;
      const win = stats.windows.get(value.owned.windowId);
      win.focused = true; win.state = 'normal';
      stats.focusedAtActionCount = stats.browserActions.length;
      stats.emitFocus();
    }});
    await harness.run('alarm');
    assert.equal(triggered, true);
    assert.equal(harness.browserActions.length, harness.focusedAtActionCount);
    assert.equal(harness.storage.blockedRun, RUN_ID);
    assert.equal(harness.tabs.size, 1);
  }
});

test('user focusing the collector stops work and subsequent alarms cannot close, navigate, or reopen', async () => {
  const harness = createHarness({focusOnCapture: true});
  await harness.run('alarm');
  assert.equal(harness.storage.blockedRun, RUN_ID);
  assert.equal(harness.nativeCalls.filter(call => call.op === 'save_report').length, 0);
  assert.deepEqual(harness.browserActions.map(action => action.op), ['create', 'window-update', 'update']);
  assert.equal(harness.browserActions.length, harness.focusedAtActionCount, 'no browser mutation after user focus');
  assert.equal(harness.tabs.size, 1, 'user-adopted window must remain open');
  const previousScriptCount = harness.scriptCalls.length;
  await harness.run('alarm');
  assert.equal(harness.browserActions.length, harness.focusedAtActionCount);
  assert.equal(harness.scriptCalls.length, previousScriptCount);
});

test('recovery after the native host completed cleans up only the matching collector window', async () => {
  const harness = createHarness({plan: {status: 'complete'},
    storage: {owned: {windowId: 20, tabId: 21, url: urlFor(sources[0]), runId: RUN_ID}},
    existingOwned: {}});
  await harness.run('startup');
  assert.equal(harness.storage.status.phase, 'complete');
  assert.equal(harness.storage.owned, undefined);
  assert.equal(harness.tabs.size, 0);
  assert.deepEqual(harness.browserActions.map(action => action.op), ['remove']);
  assert.equal(harness.captures.length, 0);
});

test('recovery after losing the owned tab resets traversal counters while retaining captured identities', async () => {
  const harness = createHarness({
    plan: {completed_categories: sources.slice(1).map(source => source.category_id)},
    storage: {
      owned: {windowId: 20, tabId: 21, url: urlFor(sources[0]), runId: RUN_ID},
      progress: {runId: RUN_ID, index: 0, seen: ['ai-models-report'], pages: 180, stationary: 9}
    },
    inspect: (source, stats) => snapshot(source, {boundary: (stats.scrollCounts.get(source.category_id) || 0) >= 2}),
    scroll: (source, stats) => ({ok: true, moved: stats.scrollCounts.get(source.category_id) > 1})
  });
  await harness.run('startup');
  const coverage = harness.categories.get('ai-models');
  assert.equal(coverage.complete, true);
  assert.equal(coverage.page_count, 2);
  assert.equal(harness.scrollCounts.get('ai-models'), 2);
  assert.equal(harness.captures.length, 0, 'already captured identity must not be copied again');
  assert.equal(harness.storage.result.status, 'complete');
});

test('Chrome returning a normal unfocused shell must be minimized and confirmed before report navigation', async () => {
  const harness = createHarness({createdState: 'normal', createdFocused: false});
  await harness.run('alarm');
  assert.equal(harness.storage.result.status, 'complete');
  const created = harness.browserActions.filter(action => action.op === 'create');
  assert.equal(created.length, 1);
  assert.equal(created[0].url, SHELL_URL);
  const minimizeAt = harness.timeline.findIndex(action => action.op === 'window-update');
  const navigateAt = harness.timeline.findIndex(action => action.op === 'update');
  assert.ok(minimizeAt > 0 && navigateAt > minimizeAt);
  assert.ok(harness.timeline.slice(minimizeAt + 1, navigateAt).some(action =>
    action.op === 'window-get' && action.state === 'minimized' && action.focused === false),
  'must re-read the real window state after requesting minimization');
  assert.ok(harness.nativeCalls.some(call => call.op === 'observe' &&
    call.event === 'window_created' && call.meta.state === 'normal'));
});

test('an initially focused collector cannot be minimized, navigated, or retried', async () => {
  const harness = createHarness({createdState: 'normal', createdFocused: true});
  await harness.run('alarm');
  assert.deepEqual(harness.browserActions.map(action => action.op), ['create']);
  assert.equal(harness.captures.length, 0);
  assert.equal(harness.nativeCalls.filter(call => call.op === 'save_report').length, 0);
  assert.equal(harness.storage.blockedRun, RUN_ID);
  assert.equal(harness.storage.status.phase, 'failed');
  await harness.run('alarm');
  assert.deepEqual(harness.browserActions.map(action => action.op), ['create']);
});

test('a minimize request that did not change actual state cannot authorize report loading', async () => {
  const harness = createHarness({createdState: 'normal', ignoreMinimize: true});
  await harness.run('alarm');
  assert.deepEqual(harness.browserActions.map(action => action.op), ['create', 'window-update']);
  assert.equal(harness.captures.length, 0);
  assert.equal(harness.categories.size, 6, 'terminal recovery records six explicit failures');
  assert.ok([...harness.categories.values()].every(coverage => coverage.complete === false),
    'window diagnostics must never mark coverage complete');
  assert.equal(harness.storage.blockedRun, RUN_ID);
  assert.ok(harness.nativeCalls.some(call => call.op === 'observe'));
  assert.equal(harness.storage.status.phase, 'failed');
});

test('disabled reload recovery closes only its verified minimized shell without opening another page', async () => {
  const owned = {windowId: 11, tabId: 12, url: SHELL_URL, runId: RUN_ID};
  const harness = createHarness({plan: {status: 'disabled'},
    storage: {activeRun: RUN_ID, owned}, existingOwned: {}});
  await harness.run('installed');
  assert.deepEqual(harness.browserActions.map(x => x.op), ['remove']);
  assert.equal(harness.tabs.size, 0);
  assert.equal(harness.storage.owned, undefined);
  assert.equal(harness.captures.length, 0);
});

test('disabled reload recovery keeps ownership evidence and never retries closing a user-adopted window', async () => {
  const owned = {windowId: 11, tabId: 12, url: SHELL_URL, runId: RUN_ID};
  const harness = createHarness({plan: {status: 'disabled'},
    storage: {activeRun: RUN_ID, owned}, existingOwned: {focused: true}});
  await harness.run('installed');
  assert.equal(harness.browserActions.length, 0);
  assert.equal(harness.storage.blockedRun, RUN_ID);
  assert.deepEqual(harness.storage.owned, owned);
  harness.windows.get(11).focused = false;
  await harness.run('alarm');
  assert.equal(harness.browserActions.length, 0);
});

test('disabled cleanup rejects an unrecognized stored page even if window ids match', async () => {
  const owned = {windowId: 11, tabId: 12, url: 'https://example.com/', runId: RUN_ID};
  const harness = createHarness({plan: {status: 'disabled'},
    storage: {activeRun: RUN_ID, owned}, existingOwned: {}});
  await harness.run('installed');
  assert.equal(harness.browserActions.length, 0);
  assert.deepEqual(harness.storage.owned, owned);
});

test('an armed startup-only probe does nothing on installed or timer events', async () => {
  const harness = createHarness({plan: {status: 'awaiting_startup'}});
  await harness.run('installed');
  await harness.run('alarm');
  assert.equal(harness.browserActions.length, 0);
  assert.equal(harness.captures.length, 0);
  assert.equal(harness.categories.size, 0);
  assert.equal(harness.storage.status.phase, 'waiting_startup');
  assert.equal(harness.storage.blockedRun, undefined);
});

const approvedVisible = {kind: 'visible_history_once', category_id: 'ai-models',
  max_visible_seconds: 120, approval_id: 'example-visible-history-consent'};

test('approved visible diagnostic reads models only and never declares six-category completion', async () => {
  const harness = createHarness({plan: {diagnostic: approvedVisible}});
  await harness.run('alarm');
  assert.deepEqual(harness.captures.map(x => x.category), ['ai-models']);
  assert.equal(harness.categories.size, 1);
  assert.equal(harness.storage.visibleResult.complete, true);
  assert.equal(harness.storage.result.status, 'partial');
  assert.equal(harness.storage.visibleApprovalConsumed, approvedVisible.approval_id);
  assert.equal(harness.tabs.size, 0);
  assert.ok(!harness.browserActions.some(x => x.op === 'window-update'));
  const count = harness.browserActions.length;
  await harness.run('startup');
  assert.equal(harness.browserActions.length, count);
});

test('consumed visible approval cannot create another window after lost result bookkeeping', async () => {
  const harness = createHarness({plan: {diagnostic: approvedVisible},
    storage: {visibleApprovalConsumed: approvedVisible.approval_id}});
  await harness.run('startup');
  assert.equal(harness.browserActions.length, 0);
  assert.equal(harness.captures.length, 0);
});

test('native-validated leftover initialization budget can be used once and cannot reset the original grant', async () => {
  const diagnostic = {...approvedVisible, max_visible_seconds: 60,
    resume_from_run_id: 'extension-probe-000000000000000000000002'};
  const harness = createHarness({plan: {diagnostic},
    storage: {visibleApprovalConsumed: approvedVisible.approval_id}});
  await harness.run('alarm');
  assert.equal(harness.captures.length, 1);
  assert.equal(harness.storage.visibleContinuationConsumed, approvedVisible.approval_id);
  assert.equal(harness.tabs.size, 0);
  const second = createHarness({plan: {diagnostic}, storage: {
    visibleApprovalConsumed: approvedVisible.approval_id,
    visibleContinuationConsumed: approvedVisible.approval_id}});
  await second.run('startup');
  assert.equal(second.browserActions.length, 0);
});

test('unapproved visible duration is rejected before any window action', async () => {
  const harness = createHarness({plan: {diagnostic: {...approvedVisible, max_visible_seconds: 121}}});
  await harness.run('alarm');
  assert.equal(harness.browserActions.length, 0);
  assert.equal(harness.storage.status.detail, 'unapproved_visible_diagnostic');
});

test('a host-validated final unused initialization budget is consumed once without resetting prior grants', async () => {
  const diagnostic = {...approvedVisible, max_visible_seconds: 60,
    resume_from_run_id: 'extension-probe-000000000000000000000003'};
  const prior = {visibleApprovalConsumed: approvedVisible.approval_id,
    visibleContinuationConsumed: approvedVisible.approval_id};
  const harness = createHarness({plan: {diagnostic}, storage: prior});
  await harness.run('alarm');
  assert.equal(harness.captures.length, 1);
  assert.equal(harness.storage.visibleFinalInitializationConsumed, approvedVisible.approval_id);
  assert.equal(harness.storage.visibleContinuationConsumed, approvedVisible.approval_id);
  const second = createHarness({plan: {diagnostic}, storage: {...prior,
    visibleFinalInitializationConsumed: approvedVisible.approval_id}});
  await second.run('startup');
  assert.equal(second.browserActions.length, 0);
});

test('slow pre-open native bookkeeping cannot create a visible window after the deadline', async () => {
  const harness = createHarness({plan: {diagnostic: approvedVisible}, preOpenDelay: 101000});
  await harness.run('alarm');
  assert.equal(harness.browserActions.length, 0);
  assert.equal(harness.captures.length, 0);
  assert.equal(harness.storage.visibleResult.error, 'visible_diagnostic_deadline');
  assert.equal(harness.storage.visibleApprovalConsumed, approvedVisible.approval_id);
});

test('visible diagnostic leaves a user-adopted collector untouched and consumes the one-time grant', async () => {
  const harness = createHarness({plan: {diagnostic: approvedVisible}, focusOnCapture: true});
  await harness.run('alarm');
  assert.equal(harness.storage.visibleApprovalConsumed, approvedVisible.approval_id);
  assert.equal(harness.captures.length, 1);
  assert.equal(harness.nativeCalls.filter(x => x.op === 'save_report').length, 0);
  assert.equal(harness.tabs.size, 1);
  assert.equal(harness.browserActions.length, harness.focusedAtActionCount);
  const count = harness.browserActions.length;
  await harness.run('alarm');
  assert.equal(harness.browserActions.length, count);
});

const pendingFailure = () => ({runId: RUN_ID, error: 'collector_window_user_interaction',
  at: '2026-10-18T11:59:00+08:00'});

test('outer failure persists while host is offline and confirms partial before a later alarm can do other work', async () => {
  let online = false;
  const harness = createHarness({createdFocused: true, native: message => {
    if (message.op === 'status' && !online) throw new Error('native_host_disconnected');
  }});
  await harness.run('alarm');
  assert.equal(harness.storage.terminalPending.runId, RUN_ID);
  assert.equal(harness.storage.terminalPending.error, 'collector_creation_took_focus');
  assert.equal(harness.storage.finishedRun, undefined);
  const firstFailure = clone(harness.storage.terminalPending);
  const actions = clone(harness.browserActions);
  await harness.run('alarm');
  assert.deepEqual(harness.storage.terminalPending, firstFailure, 'retry must preserve the initial failure');
  assert.equal(harness.nativeCalls.filter(x => x.op === 'get_plan').length, 1);
  online = true;
  await harness.run('alarm');
  assert.equal(harness.storage.terminalPending, undefined);
  assert.equal(harness.storage.finishedRun, RUN_ID);
  assert.equal(harness.storage.result.status, 'partial');
  assert.equal(harness.storage.terminalResult.error, firstFailure.error);
  assert.equal(harness.categories.size, 6);
  assert.ok([...harness.categories.values()].every(x => x.complete === false && x.page_count === 0));
  assert.deepEqual(harness.browserActions, actions, 'recovery must not act on the adopted window or create another');
  assert.equal(harness.nativeCalls.filter(x => x.op === 'get_plan').length, 1);
  await harness.run('alarm');
  assert.deepEqual(harness.browserActions, actions);
});

test('pending terminal handshake preserves previously completed categories and never opens a page', async () => {
  const completed = sources.slice(0, 2).map(x => x.category_id);
  const harness = createHarness({plan: {completed_categories: completed},
    storage: {activeRun: RUN_ID, terminalPending: pendingFailure()}});
  const before = completed.map(category => clone(harness.categories.get(category)));
  await harness.run('startup');
  assert.equal(harness.storage.result.status, 'partial');
  assert.equal(harness.storage.terminalPending, undefined);
  assert.deepEqual(completed.map(category => harness.categories.get(category)), before);
  const finish = harness.nativeCalls.filter(x => x.op === 'finish_category');
  assert.equal(finish.length, 4);
  assert.ok(finish.every(x => !completed.includes(x.category_id) && x.coverage.complete === false));
  assert.equal(harness.nativeCalls.filter(x => x.op === 'get_plan').length, 0);
  assert.equal(harness.browserActions.length, 0);
});

test('terminal run mismatch retains the failure without writing into a new host run', async () => {
  const pending = pendingFailure();
  const harness = createHarness({storage: {terminalPending: pending}, native: message => {
    if (message.op === 'status') return {ok: true, run_id: 'extension-probe-some-new-request',
      status: 'pending', completed_categories: []};
  }});
  await harness.run('alarm');
  await harness.run('alarm');
  assert.deepEqual(harness.storage.terminalPending, pending);
  assert.equal(harness.storage.finishedRun, undefined);
  assert.equal(harness.storage.status.detail, 'terminal_run_mismatch');
  assert.ok(harness.nativeCalls.every(x => x.op === 'status'), 'only read-only status may reach the different run');
  assert.equal(harness.browserActions.length, 0);
});

test('lost finish response recovers from explicit host partial without repeating terminal writes', async () => {
  let committed = false;
  const harness = createHarness({storage: {terminalPending: pendingFailure()}, native: message => {
    if (message.op === 'finish_run') { committed = true; throw new Error('response_lost_after_commit'); }
    if (message.op === 'status' && committed) return {ok: true, run_id: RUN_ID,
      status: 'partial', completed_categories: []};
  }});
  await harness.run('alarm');
  assert.ok(harness.storage.terminalPending);
  assert.equal(harness.storage.finishedRun, undefined);
  assert.equal(harness.nativeCalls.filter(x => x.op === 'finish_run').length, 1);
  const categoryWrites = harness.nativeCalls.filter(x => x.op === 'finish_category').length;
  await harness.run('startup');
  assert.equal(harness.storage.terminalPending, undefined);
  assert.equal(harness.storage.finishedRun, RUN_ID);
  assert.equal(harness.nativeCalls.filter(x => x.op === 'finish_run').length, 1);
  assert.equal(harness.nativeCalls.filter(x => x.op === 'finish_category').length, categoryWrites);
  assert.equal(harness.browserActions.length, 0);
  assert.equal(harness.nativeCalls.filter(x => x.op === 'get_plan').length, 0);
});

test('finish acknowledgement alone cannot clear pending when follow-up status is not terminal', async () => {
  let confirm = false;
  const harness = createHarness({storage: {terminalPending: pendingFailure()}, native: message => {
    if (message.op === 'status' && !confirm) return {ok: true, run_id: RUN_ID,
      status: 'pending', completed_categories: []};
  }});
  await harness.run('alarm');
  assert.ok(harness.storage.terminalPending);
  assert.equal(harness.storage.finishedRun, undefined);
  assert.equal(harness.storage.status.detail, 'terminal_finish_unconfirmed');
  confirm = true;
  await harness.run('alarm');
  assert.equal(harness.storage.terminalPending, undefined);
  assert.equal(harness.storage.finishedRun, RUN_ID);
  assert.equal(harness.nativeCalls.filter(x => x.op === 'finish_run').length, 1);
  assert.equal(harness.browserActions.length, 0);
});

test('fully completed real host state is preserved even if its final browser bookkeeping failed', async () => {
  const harness = createHarness({plan: {completed_categories: sources.map(x => x.category_id)},
    hostStatus: 'complete', storage: {terminalPending: pendingFailure()}});
  await harness.run('startup');
  assert.equal(harness.storage.result.status, 'complete');
  assert.equal(harness.storage.terminalPending, undefined);
  assert.equal(harness.storage.finishedRun, RUN_ID);
  assert.ok(harness.nativeCalls.every(x => x.op === 'status'));
  assert.equal(harness.browserActions.length, 0);
});

test('a new host failure before activating a run does not attach a terminal failure to the last run', async () => {
  let offline = false;
  const harness = createHarness({native: message => {
    if (message.op === 'get_plan' && offline) throw new Error('native_host_disconnected');
  }});
  await harness.run('alarm');
  assert.equal(harness.storage.activeRun, RUN_ID);
  const count = harness.nativeCalls.filter(x => x.op === 'finish_run').length;
  offline = true;
  await harness.run('alarm');
  assert.equal(harness.storage.terminalPending, undefined);
  assert.equal(harness.storage.blockedRun, undefined);
  assert.equal(harness.nativeCalls.filter(x => x.op === 'finish_run').length, count);
});

test('a newly confirmed completed category during recovery is never overwritten on retry', async () => {
  let conflict = true;
  const category = sources[0].category_id;
  const harness = createHarness({storage: {terminalPending: pendingFailure()}, native: (message, stats) => {
    if (conflict && message.op === 'finish_category' && message.category_id === category) {
      conflict = false;
      stats.categories.set(category, {complete: true, reason: 'boundary_before_start', boundary_time: OLDER});
      return {ok: false, error: {code: 'coverage_conflict'}};
    }
  }});
  await harness.run('alarm');
  assert.ok(harness.storage.terminalPending);
  const completed = clone(harness.categories.get(category));
  await harness.run('alarm');
  assert.deepEqual(harness.categories.get(category), completed);
  assert.equal(harness.nativeCalls.filter(x => x.op === 'finish_category' && x.category_id === category).length, 1);
  assert.equal(harness.storage.terminalPending, undefined);
  assert.equal(harness.storage.result.status, 'partial');
  assert.equal(harness.browserActions.length, 0);
});
