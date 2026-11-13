import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';

const readerSource = await readFile(new URL('../browser-extension/reader.js', import.meta.url), 'utf8');
const earlySource = await readFile(new URL('../browser-extension/early-guard.js', import.meta.url), 'utf8');
const reader = await import(`data:text/javascript;base64,${Buffer.from(readerSource).toString('base64')}`);
const renderSource = await readFile(new URL('../browser-extension/render-probe.js', import.meta.url), 'utf8');
const renderProbe = await import(`data:text/javascript;base64,${Buffer.from(renderSource).toString('base64')}`);
const expected = {title: 'AI模型新发布', conversation_id: '00000000-0000-4000-8000-000000000010'};

function fixture({marked = true, early = true, text = '# 完整报告\n\n|字段|值|\n|---|---|\n|发布|待核验|', reverse = false} = {}) {
  const counters = {nativeWrites: 0, nativeReads: 0, nativeCopies: 0, click: 0};
  class Element {
    constructor(tag = 'div', attrs = {}, innerText = '') {
      this.tagName = tag.toUpperCase(); this.attrs = attrs; this.innerText = innerText;
      this.textContent = innerText; this.children = []; this.parentElement = null;
      this.isConnected = true; this.disabled = false; this.scrollTop = 0;
      this.scrollHeight = 0; this.clientHeight = 0;
      this.style = {overflowY: 'visible', flexDirection: 'column'};
      this.events = [];
      this.rect = {left: 0, top: 0, right: 1000, bottom: 800, width: 1000, height: 800};
    }
    append(child) { this.children.push(child); child.parentElement = this; return child; }
    getAttribute(name) { return this.attrs[name] ?? null; }
    contains(element) { for (let current = element; current; current = current.parentElement) if (current === this) return true; return false; }
    matches(selector) {
      if (selector === '*') return true;
      const match = /^\[([^=\]]+)(?:="([^"]+)")?\]$/.exec(selector);
      if (match) return this.getAttribute(match[1]) !== null && (match[2] === undefined || this.getAttribute(match[1]) === match[2]);
      if (selector === 'time[datetime]') return this.tagName === 'TIME' && this.getAttribute('datetime') !== null;
      if (selector === 'a[href]') return this.tagName === 'A' && this.getAttribute('href') !== null;
      return this.tagName.toLowerCase() === selector;
    }
    querySelectorAll(selector) { return this.children.flatMap(child => [...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)]); }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    closest(selector) { for (let current = this; current; current = current.parentElement) if (current.matches(selector)) return current; return null; }
    addEventListener() {}
    dispatchEvent(event) { this.events.push(event); this.onDispatch?.(event); return true; }
    getBoundingClientRect() { return this.rect; }
  }
  class Document extends Element {
    execCommand(command) { if (/copy|cut/i.test(command)) counters.nativeCopies++; return true; }
  }
  class Clipboard {
    async writeText() { counters.nativeWrites++; }
    async write() { counters.nativeWrites++; }
    async readText() { counters.nativeReads++; return 'private clipboard'; }
    async read() { counters.nativeReads++; return []; }
  }
  const doc = new Document('html');
  doc.hasFocus = () => false;
  doc.title = expected.title; doc.readyState = early ? 'loading' : 'complete'; doc.visibilityState = 'hidden';
  const body = doc.append(new Element('body', {}, '报告正文')); doc.body = body;
  const scroller = body.append(new Element('main'));
  Object.assign(scroller, {scrollHeight: 3000, clientHeight: 600, scrollTop: reverse ? 0 : 2000});
  scroller.style = {overflowY: 'auto', flexDirection: reverse ? 'column-reverse' : 'column'};
  doc.scrollingElement = doc;
  const turn = scroller.append(new Element('article', {'data-content-search-turn-key': 'turn-001', 'data-talvt-turn-state': 'complete'}));
  const time = turn.append(new Element('time', {datetime: '2026-10-18T00:03:00.000Z'})); time.dateTime = time.attrs.datetime;
  const message = turn.append(new Element('div', {'data-chatgpt-selection-message-id': 'message-001'}));
  const markdown = message.append(new Element('div', {'data-markdown-text-style': 'assistant-message'}, text));
  const nested = markdown.append(new Element('div')); Object.assign(nested, {scrollHeight: 900, clientHeight: 50}); nested.style.overflowY = 'auto';
  markdown.append(new Element('table'));
  const link = markdown.append(new Element('a', {href: 'https://example.test/original?a=1&b=2'}, '来源')); link.href = link.attrs.href;
  const copy = turn.append(new Element('button', {'aria-label': '复制'}));
  const clipboard = new Clipboard();
  copy.click = () => { counters.click++; void clipboard.writeText(text); };
  const location = {protocol: 'https:', hostname: 'chatgpt.com', pathname: `/c/${expected.conversation_id}`,
    hash: marked ? '#personal-intel-probe=extension-test_123' : ''};
  location.href = `https://chatgpt.com${location.pathname}${location.hash}`;
  const context = vm.createContext({document: doc, navigator: {clipboard}, Clipboard, Document, location,
    innerWidth: 1000, innerHeight: 800, URL, Event, getComputedStyle: element => element.style,
    setTimeout: callback => setImmediate(callback), addEventListener() {}});
  context.window = context;
  const run = (name, ...args) => {
    context.__testArgs = args;
    return vm.runInContext(`(${(reader[name] || renderProbe[name]).toString()})(...__testArgs)`, context);
  };
  const install = () => { vm.runInContext(earlySource, context); doc.readyState = 'complete'; };
  const addLoader = ({parent = scroller, attrs = {role: 'status'}, text = '正在加载更早的消息…', rect, style} = {}) => {
    const loader = parent.append(new Element('span', attrs, text));
    if (rect) loader.rect = rect;
    if (style) Object.assign(loader.style, style);
    return loader;
  };
  return {context, doc, body, scroller, turn, markdown, copy, counters, clipboard, run, install, addLoader};
}

test('unmarked ordinary conversation is untouched and reader refuses it', async () => {
  const f = fixture({marked: false}); f.install();
  assert.equal(f.context.__personalIntelClipboardGuardV1, undefined);
  assert.equal((await f.run('inspectPage', expected)).ok, false);
  assert.equal((await f.run('captureTurn', expected, 'turn-001')).ok, false);
  assert.equal(f.counters.click, 0);
  await f.clipboard.writeText('user action');
  assert.equal(f.counters.nativeWrites, 1);
});

test('late injection cannot claim document_start protection', async () => {
  const f = fixture({early: false}); f.install();
  assert.equal((await f.run('installClipboardGuard', expected)).ok, false);
  assert.equal((await f.run('captureTurn', expected, 'turn-001')).error, 'early_clipboard_guard_unavailable');
  assert.equal(f.counters.click, 0);
});

test('serialized exports capture exact markdown using early guard without native clipboard calls', async () => {
  const f = fixture(); f.install();
  assert.equal((await f.run('installClipboardGuard', expected)).ok, true);
  const result = await f.run('captureTurn', expected, 'turn-001');
  assert.equal(result.ok, true); assert.equal(result.text, f.markdown.innerText);
  assert.equal(result.message_id, 'message-001'); assert.equal(result.table_count, 1);
  assert.equal(result.links[0].href, 'https://example.test/original?a=1&b=2');
  assert.equal(result.guard.copy_intercepted, 1); assert.equal(result.is_report, true);
  assert.deepEqual(f.counters, {nativeWrites: 0, nativeReads: 0, nativeCopies: 0, click: 1});
});

test('legacy copy and clipboard reads are blocked and invalidate a capture', async () => {
  const f = fixture(); f.install();
  assert.equal(f.doc.execCommand('copy'), false);
  await assert.rejects(f.clipboard.readText(), /clipboard_read_blocked/);
  f.copy.click = () => { f.doc.execCommand('copy'); };
  const result = await f.run('captureTurn', expected, 'turn-001');
  assert.equal(result.error, 'copy_capture_incomplete'); assert.equal(result.guard.legacy_copy_calls, 1);
  assert.equal(f.counters.nativeCopies, 0); assert.equal(f.counters.nativeReads, 0);
});

test('two writes do not silently become one report', async () => {
  const f = fixture(); f.install();
  f.copy.click = () => { void f.clipboard.writeText('first'); void f.clipboard.writeText('second'); };
  const result = await f.run('captureTurn', expected, 'turn-001');
  assert.equal(result.error, 'copy_capture_incomplete'); assert.equal(result.guard.copy_intercepted, 2);
  assert.equal(f.counters.nativeWrites, 0);
});

test('pause-only platform notice is not counted as a report', async () => {
  const f = fixture({text: "I've paused this task because there haven't been any updates in a while. You can resume it in the Scheduled tab or by asking ChatGPT."});
  f.install();
  const inspect = await f.run('inspectPage', expected);
  assert.equal(inspect.turns[0].is_report, false);
  const capture = await f.run('captureTurn', expected, 'turn-001');
  assert.equal(capture.ok, true); assert.equal(capture.is_report, false); assert.equal(capture.is_pause_notice, true);
});

test('ambiguous or timestamp-free report fails before clicking', async () => {
  const f = fixture(); f.install();
  f.turn.querySelector('time[datetime]').dateTime = '';
  assert.equal((await f.run('captureTurn', expected, 'turn-001')).error, 'report_time_missing_or_invalid');
  assert.equal(f.counters.click, 0);
});

test('scroll uses conversation ancestor, not nested report scroller, and never infers start', async () => {
  const f = fixture(); f.install();
  const initial = await f.run('inspectPage', expected);
  assert.equal(initial.scroll.node, 'main'); assert.equal(initial.atStartEvidence.complete, false);
  const result = await f.run('scrollOlder', expected);
  assert.equal(result.ok, true); assert.equal(f.scroller.scrollTop, 1520);
  f.scroller.scrollTop = 0;
  const stationary = await f.run('scrollOlder', expected);
  assert.equal(stationary.moved, false); assert.equal(stationary.atStartEvidence.complete, false);
  assert.equal(f.counters.click, 0);
});

test('reverse scroll containers move toward negative oldest boundary', async () => {
  const f = fixture({reverse: true}); f.install();
  const result = await f.run('scrollOlder', expected);
  assert.equal(result.reverse, true); assert.equal(result.target_top, -480); assert.equal(f.scroller.scrollTop, -480);
});

test('wrong title or different source path blocks capture and scrolling', async () => {
  const f = fixture(); f.install();
  f.doc.title = '私人聊天';
  assert.equal((await f.run('captureTurn', expected, 'turn-001')).ok, false);
  assert.equal((await f.run('scrollOlder', expected)).ok, false);
  assert.equal(f.counters.click, 0); assert.equal(f.scroller.scrollTop, 2000);
});

test('quoted loading text inside a report is never an active UI signal', async () => {
  const f = fixture({text: '这份报告解释“正在加载更早的消息…”的含义。'}); f.install();
  f.body.innerText += '\n正在加载更早的消息…';
  f.addLoader({parent: f.markdown, attrs: {role: 'status', 'aria-busy': 'true'}});
  for (const method of ['inspectPage', 'scrollOlder']) {
    const result = await f.run(method, expected);
    assert.equal(result.loading, false); assert.equal(result.history_loader_present, false);
    assert.equal(result.loading_evidence.signals.length, 0);
  }
});

test('offscreen persistent history sentinel is present but does not mean loading', async () => {
  const f = fixture(); f.install();
  f.addLoader({rect: {left: 0, right: 300, top: -70, bottom: -20, width: 300, height: 50}});
  for (const method of ['inspectPage', 'scrollOlder']) {
    const result = await f.run(method, expected);
    assert.equal(result.loading, false); assert.equal(result.history_loader_present, true);
    assert.equal(result.loading_evidence.visible_history_text_nodes, 0);
    assert.equal(result.atStartEvidence.complete, false);
  }
});

test('hidden and scroll-clipped loaders do not assert loading', async () => {
  for (const scenario of ['display-none', 'aria-hidden', 'clipped']) {
    const f = fixture(); f.install();
    const loader = f.addLoader();
    if (scenario === 'display-none') loader.style.display = 'none';
    if (scenario === 'aria-hidden') loader.attrs['aria-hidden'] = 'true';
    if (scenario === 'clipped') {
      f.scroller.rect = {left: 0, right: 1000, top: 100, bottom: 700, width: 1000, height: 600};
      loader.rect = {left: 0, right: 300, top: 20, bottom: 60, width: 300, height: 40};
    }
    const result = await f.run('inspectPage', expected);
    assert.equal(result.loading, false, scenario); assert.equal(result.history_loader_present, true);
  }
});

test('visible text without an active UI status remains a diagnostic only', async () => {
  const f = fixture(); f.install(); f.addLoader({attrs: {}});
  const result = await f.run('inspectPage', expected);
  assert.equal(result.loading, false); assert.equal(result.history_loader_present, true);
  assert.equal(result.loading_evidence.visible_history_text_nodes, 1);
});

test('visible semantic history status and active conversation aria-busy are detected', async () => {
  const f = fixture(); f.install(); f.addLoader({attrs: {'aria-live': 'polite'}});
  for (const method of ['inspectPage', 'scrollOlder']) {
    const result = await f.run(method, expected);
    assert.equal(result.loading, true);
    assert.ok(result.loading_evidence.signals.includes('visible_history_live_region'));
  }
  const busy = fixture(); busy.install(); busy.scroller.attrs['aria-busy'] = 'true';
  const result = await busy.run('inspectPage', expected);
  assert.equal(result.loading, true); assert.equal(result.history_loader_present, false);
  assert.equal(result.loading_evidence.visible_busy_regions, 1);
});

test('exact completed monitoring confirmation preserves its full text, identity, and null date', async () => {
  const text = "  This scheduled task hasn't produced any updates in a while. Do you want me to keep monitoring for you?\n";
  const f = fixture({text}); f.install();
  f.turn.querySelector('time[datetime]').dateTime = '';
  const inspected = (await f.run('inspectPage', expected)).turns[0];
  assert.equal(inspected.is_task_notice, true); assert.equal(inspected.is_report, false);
  assert.equal(inspected.notice_kind, 'monitoring_confirmation'); assert.equal(inspected.notice_text, text);
  assert.equal(inspected.report_time, null); assert.equal(inspected.turn_key, 'turn-001');
  const captured = await f.run('captureTurn', expected, 'turn-001');
  assert.equal(captured.ok, true); assert.equal(captured.is_task_notice, true); assert.equal(captured.is_report, false);
  assert.equal(captured.notice_kind, 'monitoring_confirmation'); assert.equal(captured.notice_text, text);
  assert.equal(captured.text, text); assert.equal(captured.report_time, null);
  assert.equal(captured.turn_key, 'turn-001'); assert.equal(captured.message_id, 'message-001');
  assert.equal(f.counters.nativeWrites, 0);
});

test('monitoring template with additional report content cannot bypass missing-date failure', async () => {
  const text = "This scheduled task hasn't produced any updates in a while. Do you want me to keep monitoring for you?\n\n本轮发现：新的跨境机会与收费条件。";
  const f = fixture({text}); f.install();
  f.turn.querySelector('time[datetime]').dateTime = '';
  const inspected = (await f.run('inspectPage', expected)).turns[0];
  assert.equal(inspected.is_task_notice, false); assert.equal(inspected.is_report, true);
  assert.equal(inspected.notice_kind, null); assert.equal(inspected.notice_text, null);
  const captured = await f.run('captureTurn', expected, 'turn-001');
  assert.equal(captured.error, 'report_time_missing_or_invalid'); assert.equal(f.counters.click, 0);
});

test('incomplete or different undated assistant message never becomes a monitoring confirmation', async () => {
  const exact = fixture({text: "This scheduled task hasn't produced any updates in a while. Do you want me to keep monitoring for you?"}); exact.install();
  exact.turn.attrs['data-talvt-turn-state'] = 'streaming';
  const inspected = (await exact.run('inspectPage', expected)).turns[0];
  assert.equal(inspected.is_task_notice, false); assert.equal(inspected.is_report, true);
  const unknown = fixture({text: '暂时没有找到新机会。'}); unknown.install();
  unknown.turn.querySelector('time[datetime]').dateTime = '';
  assert.equal((await unknown.run('inspectPage', expected)).turns[0].is_task_notice, false);
  assert.equal((await unknown.run('captureTurn', expected, 'turn-001')).error, 'report_time_missing_or_invalid');
  assert.equal(unknown.counters.click, 0);
});

// Synthetic administrative receipt fixtures; no real task content.
// Test-only values intentionally match the sanitized reader contract.
const configurationReceiptSource = {
  title: 'AI产品机会雷达', conversation_id: '00000000-0000-4000-8000-000000000008',
};
const configurationReceipts = [
  {
    message_id: '00000000-0000-4000-8000-000000000006',
    text: '合成配置回执 1：仅供测试，不代表任何真实任务。',
  },
  {
    message_id: '00000000-0000-4000-8000-000000000001',
    text: '合成配置回执 2：仅供测试，不代表任何真实任务。',
  },
];

function configurationReceiptFixture(receipt, {source = configurationReceiptSource,
  messageId = receipt.message_id, text = receipt.text, state = 'complete'} = {}) {
  const f = fixture({text});
  f.doc.title = source.title;
  f.context.location.pathname = `/c/${source.conversation_id}`;
  f.context.location.href = `https://chatgpt.com${f.context.location.pathname}${f.context.location.hash}`;
  f.markdown.parentElement.attrs['data-chatgpt-selection-message-id'] = messageId;
  if (state === null) delete f.turn.attrs['data-talvt-turn-state'];
  else f.turn.attrs['data-talvt-turn-state'] = state;
  f.turn.querySelector('time[datetime]').dateTime = '';
  f.install();
  return f;
}

test('both exact completed configuration receipts are excluded even without dates', async () => {
  for (const receipt of configurationReceipts) {
    const f = configurationReceiptFixture(receipt);
    const result = await f.run('inspectPage', configurationReceiptSource);
    assert.equal(result.ok, true);
    const inspected = result.turns[0];
    assert.equal(inspected.is_task_notice, true, receipt.message_id);
    assert.equal(inspected.is_report, false);
    assert.equal(inspected.notice_kind, 'automation_configuration_receipt');
    assert.equal(inspected.notice_text, receipt.text);
    assert.equal(inspected.report_time, null);
    assert.equal(inspected.message_id, receipt.message_id);
    assert.equal(inspected.state, 'complete');
    assert.deepEqual(f.counters, {nativeWrites: 0, nativeReads: 0, nativeCopies: 0, click: 0});
  }
});

test('configuration receipt text with an unknown or swapped message ID remains a report', async () => {
  for (const receipt of configurationReceipts) {
    const otherReceipt = configurationReceipts.find(candidate => candidate !== receipt);
    for (const messageId of ['message-001', otherReceipt.message_id]) {
      const f = configurationReceiptFixture(receipt, {messageId});
      const inspected = (await f.run('inspectPage', configurationReceiptSource)).turns[0];
      assert.equal(inspected.is_task_notice, false, messageId);
      assert.equal(inspected.is_report, true);
      assert.equal(inspected.notice_kind, null); assert.equal(inspected.notice_text, null);
      assert.equal((await f.run('captureTurn', configurationReceiptSource, 'turn-001')).error, 'report_time_missing_or_invalid');
      assert.equal(f.counters.click, 0);
    }
  }
});

test('configuration receipt text and message ID in a different allowed conversation remain a report', async () => {
  for (const receipt of configurationReceipts) {
    const f = configurationReceiptFixture(receipt, {source: expected});
    const result = await f.run('inspectPage', expected);
    assert.equal(result.ok, true);
    assert.equal(result.turns[0].is_task_notice, false);
    assert.equal(result.turns[0].is_report, true);
    assert.equal(result.turns[0].notice_kind, null); assert.equal(result.turns[0].notice_text, null);
    assert.equal((await f.run('captureTurn', expected, 'turn-001')).error, 'report_time_missing_or_invalid');
    assert.equal(f.counters.click, 0);
  }
});

test('configuration receipts require exact full rendered text including whitespace', async () => {
  for (const receipt of configurationReceipts) {
    for (const text of [receipt.text + '\n本轮发现：新产品机会。', receipt.text + ' ', receipt.text + '\n', '\n' + receipt.text]) {
      const f = configurationReceiptFixture(receipt, {text});
      const inspected = (await f.run('inspectPage', configurationReceiptSource)).turns[0];
      assert.equal(inspected.is_task_notice, false);
      assert.equal(inspected.is_report, true);
      assert.equal(inspected.notice_kind, null); assert.equal(inspected.notice_text, null);
      assert.equal((await f.run('captureTurn', configurationReceiptSource, 'turn-001')).error, 'report_time_missing_or_invalid');
      assert.equal(f.counters.click, 0);
    }
  }
});

test('exact configuration receipts with incomplete or missing state remain reports', async () => {
  for (const receipt of configurationReceipts) {
    for (const state of ['streaming', null]) {
      const f = configurationReceiptFixture(receipt, {state});
      const inspected = (await f.run('inspectPage', configurationReceiptSource)).turns[0];
      assert.equal(inspected.is_task_notice, false);
      assert.equal(inspected.is_report, true);
      assert.equal(inspected.notice_kind, null); assert.equal(inspected.notice_text, null);
      assert.equal((await f.run('captureTurn', configurationReceiptSource, 'turn-001')).error, 'report_or_copy_not_unique_and_complete');
      assert.equal(f.counters.click, 0);
    }
  }
});

test('explicit jumpToTop targets oldest boundary in normal and reverse containers', async () => {
  for (const reverse of [false, true]) {
    const f = fixture({reverse}); f.install();
    const result = await f.run('scrollOlder', expected, {jumpToTop: true});
    const minimum = reverse ? -(3000 - 600) : 0;
    assert.equal(result.ok, true); assert.equal(result.jump_to_top, true);
    assert.equal(result.target_top, minimum); assert.equal(f.scroller.scrollTop, minimum);
    assert.equal(result.at_top, true); assert.equal(result.atStartEvidence.complete, false);
    assert.equal(f.counters.click, 0);
  }
});

test('render probe records a fired animation frame without UI input and reveals no report text', async () => {
  const f = fixture(); f.install();
  let now = 0, requested = 0, cancelled = 0, cleared = 0;
  Object.assign(f.context, {performance: {now: () => now},
    requestAnimationFrame(callback) { requested++; setImmediate(() => { now = 16.7; callback(now); }); return 7; },
    cancelAnimationFrame(id) { assert.equal(id, 7); cancelled++; },
    setTimeout(callback, delay) { assert.equal(delay, 1500); return 8; },
    clearTimeout(id) { assert.equal(id, 8); cleared++; }});
  const beforeScroll = f.scroller.scrollTop;
  const result = await f.run('probeRendering', expected);
  assert.equal(result.ok, true); assert.equal(result.raf_fired, true); assert.equal(result.delay_ms, 16.7);
  assert.equal(result.visibility, 'hidden'); assert.equal(result.viewport.width, 1000);
  assert.equal(requested, 1); assert.equal(cancelled, 1); assert.equal(cleared, 1);
  assert.equal(f.scroller.scrollTop, beforeScroll); assert.equal(f.counters.click, 0);
  assert.equal(JSON.stringify(result).includes(f.markdown.innerText), false);
});

test('render probe times out, cancels its one frame, and diagnoses a clickable loader ancestor', async () => {
  const f = fixture(); f.install();
  let now = 0, requested = 0, cancelled = 0;
  Object.assign(f.context, {performance: {now: () => now},
    requestAnimationFrame() { requested++; return 9; },
    cancelAnimationFrame(id) { assert.equal(id, 9); cancelled++; },
    setTimeout(callback, delay) { setImmediate(() => { now = delay; callback(); }); return 10; },
    clearTimeout(id) { assert.equal(id, 10); }});
  const button = f.addLoader({text: '', attrs: {role: 'button'}});
  f.addLoader({parent: button});
  f.addLoader({parent: f.markdown});
  const result = await f.run('probeRendering', expected);
  assert.equal(result.raf_fired, false); assert.equal(result.delay_ms, 1500);
  assert.equal(result.history_loader_count, 1); assert.equal(result.loader_has_clickable_button_ancestor, true);
  assert.equal(result.clickable_loader_button_count, 1); assert.equal(requested, 1); assert.equal(cancelled, 1);
  assert.equal(f.counters.click, 0); assert.equal(f.scroller.scrollTop, 2000);
});

test('render probe refuses an unowned page before requesting any frame', async () => {
  const f = fixture({marked: false});
  let requested = 0; f.context.requestAnimationFrame = () => { requested++; };
  const result = await f.run('probeRendering', expected);
  assert.equal(result.error, 'unexpected_conversation_or_unowned_page'); assert.equal(requested, 0);
});

test('reenterTop performs one viewport excursion and returns to the oldest normal and reverse boundary', async () => {
  for (const reverse of [false, true]) {
    const f = fixture({reverse}); f.install();
    const minimum = reverse ? -2400 : 0;
    let position = minimum;
    const positions = [], waits = [];
    Object.defineProperty(f.scroller, 'scrollTop', {get: () => position, set: value => { position = value; positions.push(value); }});
    f.context.setTimeout = (callback, delay) => { waits.push(delay); return setImmediate(callback); };
    const result = await f.run('scrollOlder', expected, {reenterTop: true});
    assert.equal(result.ok, true); assert.equal(result.reentered, true);
    assert.deepEqual(positions, [reverse ? -1800 : 600, minimum]); assert.deepEqual(waits, [650, 650]);
    assert.equal(result.before.top, minimum); assert.equal(result.after.top, minimum);
    assert.equal(result.excursion_top, reverse ? -1800 : 600);
    assert.equal(result.atStartEvidence.complete, false); assert.equal(f.counters.click, 0);
  }
});

test('reenterTop refuses containers that are not already at the history top', async () => {
  for (const reverse of [false, true]) {
    const f = fixture({reverse}); f.install();
    const before = f.scroller.scrollTop;
    const result = await f.run('scrollOlder', expected, {reenterTop: true});
    assert.equal(result.error, 'reenter_requires_history_top'); assert.equal(result.reentered, false);
    assert.equal(f.scroller.scrollTop, before);
  }
});

test('reenterTop never returns after a mid-wait title, path, hash, or container change', async () => {
  for (const change of ['title', 'path', 'hash', 'disconnected']) {
    const f = fixture(); f.install(); f.scroller.scrollTop = 0;
    let waits = 0;
    f.context.setTimeout = callback => setImmediate(() => {
      waits++;
      if (change === 'title') f.doc.title = '用户打开的其他页面';
      if (change === 'path') f.context.location.pathname = '/';
      if (change === 'hash') f.context.location.hash = '#user-changed';
      if (change === 'disconnected') f.scroller.isConnected = false;
      callback();
    });
    const result = await f.run('scrollOlder', expected, {reenterTop: true});
    assert.equal(result.error, 'page_or_scroll_container_changed', change);
    assert.equal(result.reentered, false); assert.equal(waits, 1);
    assert.equal(f.scroller.scrollTop, 600, `${change}: must not move back after ownership changed`);
  }
});

test('reenterTop preserves an intervening scroll or foregrounded collector instead of restoring it', async () => {
  for (const change of ['scroll', 'visibility']) {
    const f = fixture({reverse: true}); f.install(); f.scroller.scrollTop = -2400;
    f.context.setTimeout = callback => setImmediate(() => {
      if (change === 'scroll') f.scroller.scrollTop = -900;
      if (change === 'visibility') f.doc.visibilityState = 'visible';
      callback();
    });
    const result = await f.run('scrollOlder', expected, {reenterTop: true});
    assert.equal(result.ok, false); assert.equal(result.reentered, false);
    assert.equal(f.scroller.scrollTop, change === 'scroll' ? -900 : -1800);
  }
});

test('hidden reentry dispatches exactly one untrusted non-bubbling scroll only after real two-way movement', async () => {
  for (const reverse of [false, true]) {
    const f = fixture({reverse}); f.install(); f.scroller.scrollTop = reverse ? -2400 : 0;
    const result = await f.run('scrollOlder', expected, {reenterTop: true, notifyScroll: true});
    assert.equal(result.ok, true); assert.equal(result.reentered, true); assert.equal(result.scroll_event_dispatched, true);
    assert.equal(f.scroller.events.length, 1); assert.equal(f.doc.events.length, 0); assert.equal(f.body.events.length, 0);
    const event = f.scroller.events[0];
    assert.equal(event.type, 'scroll'); assert.equal(event.isTrusted, false);
    assert.equal(event.bubbles, false); assert.equal(event.cancelable, false); assert.equal(event.composed, false);
    assert.equal(result.atStartEvidence.complete, false); assert.equal(f.counters.click, 0);
    assert.equal(f.counters.nativeReads, 0); assert.equal(f.counters.nativeWrites, 0);
  }
});

test('scroll notification refuses visible, focused, document-root, or non-reentry experiments before moving', async () => {
  for (const scenario of ['visible', 'focused', 'document-root', 'no-reentry']) {
    const f = fixture(); f.install(); f.scroller.scrollTop = 0;
    if (scenario === 'visible') f.doc.visibilityState = 'visible';
    if (scenario === 'focused') f.doc.hasFocus = () => true;
    if (scenario === 'document-root') f.doc.scrollingElement = f.scroller;
    const result = await f.run('scrollOlder', expected, {reenterTop: scenario !== 'no-reentry', notifyScroll: true});
    assert.equal(result.ok, false, scenario); assert.equal(result.scroll_event_dispatched, false);
    assert.equal(f.scroller.scrollTop, 0); assert.equal(f.scroller.events.length, 0);
  }
});

test('no actual excursion or a failed return cannot emit a scroll notification', async () => {
  for (const ignoredMovement of ['excursion', 'return']) {
    const f = fixture(); f.install();
    let position = 0;
    Object.defineProperty(f.scroller, 'scrollTop', {get: () => position, set: value => {
      if (ignoredMovement === 'excursion' && value > 0) return;
      if (ignoredMovement === 'return' && value === 0) return;
      position = value;
    }});
    const result = await f.run('scrollOlder', expected, {reenterTop: true, notifyScroll: true});
    assert.equal(result.ok, false); assert.equal(f.scroller.events.length, 0);
  }
});

test('identity, visibility, focus, or scroll changes before notification suppress dispatch', async () => {
  for (const change of ['hash', 'visibility', 'focus', 'position']) {
    const f = fixture(); f.install(); f.scroller.scrollTop = 0;
    let waits = 0;
    f.context.setTimeout = callback => setImmediate(() => {
      if (++waits === 2) {
        if (change === 'hash') f.context.location.hash = '#changed';
        if (change === 'visibility') f.doc.visibilityState = 'visible';
        if (change === 'focus') f.doc.hasFocus = () => true;
        if (change === 'position') f.scroller.scrollTop = 120;
      }
      callback();
    });
    const result = await f.run('scrollOlder', expected, {reenterTop: true, notifyScroll: true});
    assert.equal(result.ok, false, change); assert.equal(f.scroller.events.length, 0);
  }
});

test('state changes caused during scroll dispatch are reported without another event or rollback', async () => {
  const f = fixture(); f.install(); f.scroller.scrollTop = 0;
  f.scroller.onDispatch = () => { f.doc.title = '用户选择的新页面'; f.scroller.scrollTop = 77; };
  const result = await f.run('scrollOlder', expected, {reenterTop: true, notifyScroll: true});
  assert.equal(result.error, 'scroll_notify_state_changed_after_dispatch');
  assert.equal(result.scroll_event_dispatched, true); assert.equal(f.scroller.events.length, 1);
  assert.equal(f.scroller.scrollTop, 77);
});
