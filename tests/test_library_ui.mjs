// Behavioral DOM tests only. This lightweight model does not render CSS and is
// not evidence of visual quality, real browser permissions, or disk persistence.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import {dirname, resolve} from 'node:path';
import test from 'node:test';
import vm from 'node:vm';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const app = readFileSync(resolve(root, 'ui/app.js'), 'utf8');
const template = readFileSync(resolve(root, 'ui/template.html'), 'utf8');
const dataName = name => name.replace(/[A-Z]/g, letter => '-' + letter.toLowerCase());

class Node {
  constructor(tag, document) {
    this.tagName = tag.toLowerCase(); this.ownerDocument = document;
    this.childNodes = []; this.parentNode = null; this.attributes = new Map();
    this.listeners = new Map(); this._text = ''; this.value = ''; this.disabled = false;
    this.hidden = false; this.open = false;
    this.dataset = new Proxy({}, {
      get: (_, key) => this.getAttribute('data-' + dataName(key)) ?? undefined,
      set: (_, key, value) => { this.setAttribute('data-' + dataName(key), value); return true; }
    });
    this.classList = {
      add: (...names) => { this.className = [...new Set([...this.className.split(/\s+/), ...names])].join(' ').trim(); },
      contains: name => this.className.split(/\s+/).includes(name)
    };
  }
  get className() { return this.getAttribute('class') || ''; }
  set className(value) { this.setAttribute('class', value); }
  get children() { return this.childNodes.filter(child => child.tagName !== '#text'); }
  get textContent() { return this._text + this.childNodes.map(child => child.textContent).join(''); }
  set textContent(value) {
    for (const child of this.childNodes) child.parentNode = null;
    this.childNodes = []; this._text = String(value ?? '');
  }
  set innerHTML(_) { throw new Error('UI must not parse source text as HTML'); }
  get isConnected() { return this === this.ownerDocument.body || Boolean(this.parentNode?.isConnected); }
  setAttribute(name, value) {
    this.attributes.set(name, String(value));
    if (name === 'hidden') this.hidden = true;
    if (name === 'disabled') this.disabled = true;
    if (name === 'value') this.value = String(value);
  }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  append(...children) {
    for (let child of children) {
      if (typeof child === 'string') child = this.ownerDocument.createTextNode(child);
      child.remove(); child.parentNode = this; this.childNodes.push(child);
    }
  }
  replaceChildren(...children) { this.textContent = ''; this.append(...children); }
  remove() {
    if (this.parentNode) this.parentNode.childNodes = this.parentNode.childNodes.filter(node => node !== this);
    this.parentNode = null;
  }
  matches(selector) {
    if (selector.startsWith('#')) return this.getAttribute('id') === selector.slice(1);
    if (selector.startsWith('.')) return this.classList.contains(selector.slice(1));
    const attr = selector.match(/^\[([^=\]]+)(?:="([^"]*)")?\]$/);
    if (attr) return attr[2] === undefined ? this.attributes.has(attr[1]) : this.getAttribute(attr[1]) === attr[2];
    return this.tagName === selector.toLowerCase();
  }
  querySelectorAll(selector) {
    const found = [];
    const parts = selector.trim().split(/\s+/);
    const visit = node => {
      for (const child of node.children) {
        if (child.matches(parts.at(-1))) {
          let ancestor = child.parentNode; let index = parts.length - 2;
          while (index >= 0 && ancestor) {
            if (ancestor.matches(parts[index])) index--;
            ancestor = ancestor.parentNode;
          }
          if (index < 0) found.push(child);
        }
        visit(child);
      }
    };
    visit(this); return found;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  closest(selector) { return this.matches(selector) ? this : this.parentNode?.closest(selector) || null; }
  contains(node) { return node === this || this.childNodes.some(child => child.contains(node)); }
  addEventListener(type, listener) {
    const listeners = this.listeners.get(type) || []; listeners.push(listener); this.listeners.set(type, listeners);
  }
  dispatchEvent(event) {
    event.target ??= this; event.currentTarget = this; event.preventDefault ??= () => {};
    const work = (this.listeners.get(event.type) || []).map(listener => listener(event));
    if (typeof this['on' + event.type] === 'function') work.push(this['on' + event.type](event));
    return Promise.all(work);
  }
  click() { return this.disabled ? Promise.resolve() : this.dispatchEvent({type: 'click'}); }
  focus() { if (!this.disabled) this.ownerDocument.activeElement = this; }
  showModal() { this.open = true; this.querySelector('button')?.focus(); }
  close() { this.open = false; return this.dispatchEvent({type: 'close'}); }
  getBoundingClientRect() { return {left: 0, top: 0, right: 800, bottom: 600}; }
}

function documentFromTemplate() {
  const document = {
    createElement: tag => new Node(tag, document),
    createTextNode: text => { const node = new Node('#text', document); node.textContent = text; return node; },
    querySelectorAll: selector => document.root.querySelectorAll(selector),
    getElementById: id => document.root.querySelector('#' + id)
  };
  document.root = new Node('document', document);
  const stack = [document.root];
  const voidTags = new Set(['meta', 'input', 'br', 'link', 'hr']);
  for (const token of template.match(/<!--[\s\S]*?-->|<![^>]*>|<\/[\w-]+\s*>|<[\w-]+[^>]*>|[^<]+/g)) {
    if (token.startsWith('<!')) continue;
    if (token.startsWith('</')) { stack.pop(); continue; }
    if (token.startsWith('<')) {
      const tag = token.match(/^<([\w-]+)/)[1]; const node = document.createElement(tag);
      const attrs = token.slice(tag.length + 1, token.length - 1);
      for (const match of attrs.matchAll(/([\w-]+)(?:="([^"]*)")?/g)) node.setAttribute(match[1], match[2] ?? '');
      stack.at(-1).append(node);
      if (tag === 'body') document.body = node;
      if (!voidTags.has(tag) && !token.endsWith('/>')) stack.push(node);
    } else stack.at(-1).append(document.createTextNode(token));
  }
  document.activeElement = document.body;
  return document;
}

function fixture() {
  const categoryIds = ['ai-models', 'ai-products', 'platform-income', 'user-pains', 'solo-business', 'cross-border'];
  const categories = categoryIds.map(id => ({id, label: id, description: '栏目说明'}));
  const records = [1, 2, 3].map(index => ({
    id: 'intel-20261018-00' + index, category_id: 'ai-products', title: '报告' + index,
    collected_at: '2026-10-18', report_date: null, source_task: 'AI产品机会雷达',
    summary: '简明结论', audience: '目标用户', relevance: '整理者判断：有待验证',
    recommendation: '继续观察', recommendation_reason: '尚无真实反馈', next_action: '核实一条来源',
    status: '待验证', uncertainties: ['尚未核验'], tags: ['工具'], sources: [], related_ids: [],
    original: '正文专用词 Orchid <img id="untrusted" src=x>', original_file: 'data/originals/a.md', original_sha256: 'a'.repeat(64)
  }));
  const weekly_reviews = [1, 2, 3].map(index => ({
    id: 'weekly-20261025-00' + index, title: '复盘' + index, summary: '本周先核验实际使用条件',
    next_action: '用三十分钟核验资格', unknowns: ['用户尚未反馈，执行结果未知。'],
    candidate_record_ids: [records[0].id, records[1].id], selected_record_id: records[1].id,
    scheduled_for: '2026-10-25T10:00:00+08:00', generated_at: '2026-10-25T10:03:00+08:00',
    collected_at: '2026-10-25', coverage_start: '2026-10-18', coverage_end: '2026-10-25',
    original: '# 周复盘\n原文独有词 ReviewOriginal <script>untrusted()</script>',
    original_file: 'data/weekly-reviews/a.md', original_sha256: 'b'.repeat(64)
  }));
  const states = {};
  for (const items of [records, weekly_reviews]) {
    states[items[1].id] = {state: 'archived', restore_to: 'active'};
    states[items[2].id] = {state: 'trashed', restore_to: 'archived'};
  }
  return {categories, records, weekly_reviews, updated_at: '2026-10-25', management: {states}};
}

function load({connected = true, rejectChanges = false, noManager = false, data = fixture(),
  managementReady = Promise.resolve(), withManual = false} = {}) {
  const document = documentFromTemplate(); document.getElementById('library-data').textContent = JSON.stringify(data);
  const states = structuredClone(data.management.states); const calls = [], connectionCalls = [], manualCalls = [];
  let callbacks, manualOptions;
  const directoryHandle = {name: '用户痛点'};
  const connectedStatus = () => { connected = true; callbacks.onStatus({message: '已连接资料文件夹', connected, busy: false}); };
  const controller = {
    ready: managementReady,
    getState: id => states[id] || {state: 'active', restore_to: 'active'},
    getRoot: () => connected ? directoryHandle : null,
    connect: async () => { connectionCalls.push('connect'); connectedStatus(); },
    useDefault: async () => { connectionCalls.push('useDefault'); connectedStatus(); },
    refresh: async () => { callbacks.onChange(); callbacks.onStatus({busy: false, connected}); },
    change: async (id, action) => {
      if (!connected) throw new Error('请先连接资料文件夹，再进行管理操作。');
      if (rejectChanges) throw new Error('磁盘写入失败；尚未确认保存，请刷新后重试。');
      callbacks.onStatus({busy: true, connected});
      await Promise.resolve(); calls.push({id, action});
      const before = controller.getState(id);
      states[id] = action === 'trash' ? {state: 'trashed', restore_to: before.state} :
        {state: action === 'archive' ? 'archived' : action === 'restore' ? before.restore_to : 'active', restore_to: 'active'};
      callbacks.onChange(); callbacks.onStatus({level: 'success', message: '已保存到资料文件夹', connected, busy: false});
      callbacks.onStatus({busy: false, connected});
    }
  };
  const manualController = {
    async submit() { manualCalls.push('submit'); await manualOptions.connect(); },
    async refresh() { manualCalls.push('refresh'); manualOptions.onStatus({status: 'idle', busy: false}); }
  };
  const window = {setTimeout, IntelManualIntake: withManual ? {create: options => {
    manualOptions = options; return manualController;
  }} : undefined, IntelManagement: noManager ? undefined : {create: options => {
    callbacks = options; callbacks.onStatus({connected, busy: false, message: connected ? '已连接资料文件夹' : '当前显示阅读快照，请连接资料文件夹。'});
    return controller;
  }}};
  vm.runInNewContext(app, {document, window, navigator: {}, URL, Blob, setTimeout, Promise}, {filename: 'ui/app.js'});
  const byId = id => document.getElementById(id);
  const nav = view => byId('category-nav').querySelectorAll('button').find(button => button.dataset.view === view && button.dataset.category === '');
  const cards = () => byId('record-list').children;
  const action = (id, name) => document.querySelectorAll('[data-manage-record]').find(button => button.dataset.manageRecord === id && button.dataset.action === name);
  return {document, byId, nav, cards, action, data, calls, states, controller, connectionCalls, manualCalls,
    emitManual: value => manualOptions.onStatus(value)};
}

test('manual intake waits for remembered-folder restoration before it can be clicked', async () => {
  let finishRestore;
  const managementReady = new Promise(resolve => { finishRestore = resolve; });
  const ui = load({withManual: true, managementReady, connected: false});
  assert.equal(ui.byId('manual-intake').disabled, true);
  assert.equal(ui.byId('manual-intake-label').textContent, '恢复连接…');
  await ui.byId('manual-intake').click();
  assert.deepEqual(ui.manualCalls, []);
  assert.deepEqual(ui.connectionCalls, []);
  finishRestore();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(ui.byId('manual-intake').disabled, false);
  assert.equal(ui.byId('manual-intake-label').textContent, '手动收录');
  assert.deepEqual(ui.manualCalls, ['refresh']);
});

test('manual intake uses the default folder while explicit reconnect remains a separate action', async () => {
  const ui = load({withManual: true, connected: false});
  await new Promise(resolve => setImmediate(resolve));
  await ui.byId('manual-intake').click();
  assert.deepEqual(ui.connectionCalls, ['useDefault']);
  assert.equal(ui.manualCalls.filter(call => call === 'submit').length, 1);
  await ui.byId('connect-library').click();
  assert.deepEqual(ui.connectionCalls, ['useDefault', 'connect']);
});

test('manual intake labels distinguish authorization and submission from a saved or running request', async () => {
  const ui = load({withManual: true});
  await new Promise(resolve => setImmediate(resolve));
  for (const [status, label] of [['restoring', '恢复连接…'], ['authorizing', '等待授权…'],
    ['submitting', '提交中…'], ['waiting', '等待收录…'], ['running', '正在收录…']]) {
    ui.emitManual({status, busy: true, message: status});
    assert.equal(ui.byId('manual-intake-label').textContent, label, status);
    assert.equal(ui.byId('manual-intake').disabled, true, status);
    assert.equal(ui.byId('manual-intake-panel').getAttribute('aria-busy'), 'true', status);
    assert.equal(ui.byId('reload-library').hidden, true, status);
  }
  ui.emitManual({status: 'complete', busy: false, canRefresh: true, message: '本次收录已完成'});
  assert.equal(ui.byId('manual-intake-label').textContent, '手动收录');
  assert.equal(ui.byId('manual-intake').disabled, false);
  assert.equal(ui.byId('reload-library').hidden, false);
});

test('main report counts exclude reviews, archived reports and trash', () => {
  const ui = load();
  assert.equal(ui.cards().length, 1);
  assert.equal(ui.cards()[0].dataset.recordId, ui.data.records[0].id);
  assert.match(ui.byId('collection-info').textContent, /总计 3 份报告 · 3 份周复盘/);
  assert.equal(ui.nav('weekly').querySelector('.nav-count').textContent, '1');
  assert.equal(ui.nav('archived').querySelector('.nav-count').textContent, '2');
  assert.equal(ui.nav('trashed').querySelector('.nav-count').textContent, '2');
});

test('archive and trash include reports and reviews; clearing search preserves view', async () => {
  const ui = load(); await ui.nav('archived').click();
  assert.equal(ui.cards().length, 2); assert.match(ui.byId('result-count').textContent, /1 份报告 · 1 份周复盘/);
  ui.byId('search-input').value = '不存在'; await ui.byId('search-input').dispatchEvent({type: 'input'});
  assert.equal(ui.cards().length, 0); await ui.byId('clear-filters').click();
  assert.equal(ui.byId('page-title').textContent, '已归档'); assert.equal(ui.cards().length, 2);
  await ui.nav('trashed').click(); assert.equal(ui.cards().length, 2);
  assert.match(ui.byId('page-description').textContent, /可以恢复，完整原文仍保留/);
});

test('successful archive, trash and restore follow controller state and preserve source data', async () => {
  const ui = load(); const before = JSON.stringify(ui.data);
  const id = ui.data.records[0].id;
  await ui.action(id, 'archive').click(); assert.equal(ui.cards().length, 0);
  await ui.nav('archived').click(); assert.equal(ui.cards().length, 3);
  await ui.action(id, 'trash').click(); assert.equal(ui.cards().length, 2);
  await ui.nav('trashed').click(); assert.equal(ui.action(id, 'restore').textContent, '恢复到已归档');
  await ui.action(id, 'restore').click(); assert.equal(ui.cards().length, 2);
  await ui.nav('archived').click(); await ui.action(id, 'unarchive').click();
  await ui.nav('reports').click(); assert.equal(ui.cards().length, 1);
  assert.equal(JSON.stringify(ui.data), before);
  assert.equal(ui.byId('management-status').textContent, '已保存到资料文件夹');
});

test('disconnected and rejected changes never hide the record or claim success', async () => {
  for (const options of [{connected: false}, {rejectChanges: true}]) {
    const ui = load(options); const id = ui.data.records[0].id;
    await ui.action(id, 'archive').click(); assert.equal(ui.cards().length, 1);
    assert.equal(ui.byId('management-status').getAttribute('role'), 'alert');
    assert.equal(ui.calls.length, 0);
    assert.match(ui.byId('management-status').textContent, /连接|未能|未确认/);
  }
});

test('review details show action, unknown feedback, source navigation and exact original safely', async () => {
  const ui = load(); await ui.nav('weekly').click();
  assert.equal(ui.byId('recommendation-field').hidden, true);
  assert.equal(ui.cards().length, 1); await ui.cards()[0].querySelector('.view-details').click();
  assert.equal(ui.byId('record-dialog').open, true);
  assert.match(ui.byId('detail-content').textContent, /本周唯一行动/);
  assert.match(ui.byId('detail-content').textContent, /执行结果未知/);
  assert.match(ui.byId('detail-content').textContent, /本周选择/);
  await ui.byId('original-tab').click();
  assert.equal(ui.byId('original-text').textContent, ui.data.weekly_reviews[0].original);
  assert.equal(ui.byId('original-text').querySelector('script'), null);
  await ui.byId('summary-tab').click();
  const source = ui.byId('detail-content').querySelector('.review-source-list').querySelector('button');
  await source.click(); assert.equal(ui.byId('detail-title').textContent, '报告2');
  assert.match(ui.byId('detail-placement').textContent, /已归档/);
  await ui.byId('detail-back').click(); assert.equal(ui.byId('detail-title').textContent, '复盘1');
});

test('review management works in the dialog and keeps original tab selected', async () => {
  const ui = load(); await ui.nav('weekly').click(); await ui.cards()[0].querySelector('.view-details').click();
  await ui.byId('original-tab').click();
  const button = ui.byId('detail-management-actions').querySelectorAll('button').find(node => node.dataset.action === 'archive');
  await button.click();
  assert.equal(ui.cards().length, 0);
  assert.equal(ui.byId('original-tab').getAttribute('aria-selected'), 'true');
  assert.match(ui.byId('detail-placement').textContent, /已归档/);
  assert.equal(ui.byId('original-text').textContent, ui.data.weekly_reviews[0].original);
});

test('keyboard detail tabs and close focus remain usable after an archive', async () => {
  const ui = load(); const card = ui.cards()[0];
  await card.querySelector('.view-details').click();
  await ui.byId('original-tab').dispatchEvent({type: 'keydown', key: 'ArrowLeft'});
  assert.equal(ui.document.activeElement, ui.byId('summary-tab'));
  await ui.byId('detail-management-actions').querySelector('button').click();
  await ui.byId('record-dialog').close();
  assert.equal(ui.document.activeElement, ui.nav('reports'));
});

test('review full-text search and empty review state do not invent content', async () => {
  const ui = load(); await ui.nav('weekly').click();
  ui.byId('search-input').value = 'ReviewOriginal'; await ui.byId('search-input').dispatchEvent({type: 'input'});
  assert.equal(ui.cards().length, 1);
  const data = fixture(); data.weekly_reviews = [];
  const empty = load({data}); await empty.nav('weekly').click();
  assert.equal(empty.cards().length, 0); assert.match(empty.byId('empty-description').textContent, /尚未生成/);
});

test('missing persistence controller preserves readable cards and exposes an error', () => {
  const ui = load({noManager: true});
  assert.equal(ui.cards().length, 1); assert.equal(ui.byId('connect-library').disabled, true);
  assert.match(ui.byId('management-status').textContent, /管理功能未能加载/);
});

test('template has one script slot, local resources and no permanent deletion action', () => {
  assert.equal(template.split('{{SCRIPT}}').length - 1, 1);
  assert.equal(/<script[^>]+src=|<link[^>]+href=|<iframe/i.test(template), false);
  assert.equal(/innerHTML\s*=|insertAdjacentHTML|removeEntry\(|localStorage\.setItem/.test(app), false);
  const ui = load();
  assert.equal(ui.document.querySelectorAll('button').some(button => /永久删除|清空回收站/.test(button.textContent)), false);
});
