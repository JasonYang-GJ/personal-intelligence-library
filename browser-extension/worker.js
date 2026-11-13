import {inspectPage, installClipboardGuard, captureTurn, scrollOlder} from './reader.js';
import {probeRendering} from './render-probe.js';

const HOST = 'com.personal_intel.collector';
const ALARM = 'personal-intel-probe';
const SOURCES = new Map([
  ['ai-models', ['AI模型新发布', '00000000-0000-4000-8000-000000000010']],
  ['ai-products', ['AI产品机会雷达', '00000000-0000-4000-8000-000000000008']],
  ['platform-income', ['平台变现机会雷达', '00000000-0000-4000-8000-000000000009']],
  ['user-pains', ['真实用户痛点雷达', '00000000-0000-4000-8000-000000000007']],
  ['solo-business', ['个人创业机会雷达', '00000000-0000-4000-8000-000000000011']],
  ['cross-border', ['跨境电商入场雷达', '00000000-0000-4000-8000-000000000012']]
]);
let running = false;
let owned = null;
let interrupted = false;
let interruptionReason = 'collector_interrupted';
let focusVerification = Promise.resolve();
let currentRunId = null;
let lastWindowSignature = '';
let visibleDiagnostic = false;
let visibleTimer = null;
let visibleDeadline = 0;
let visibleExpired = false;
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
function ensureVisibleTime() {
  if (visibleDiagnostic && (visibleExpired || Date.now() >= visibleDeadline)) throw new Error('visible_diagnostic_deadline');
}

async function host(message) {
  const response = await chrome.runtime.sendNativeMessage(HOST, message);
  if (!response?.ok) throw new Error(response?.error?.code || 'native_host_error');
  return response;
}
async function observe(event, meta) {
  if (!currentRunId) return;
  try { await host({op: 'observe', run_id: currentRunId, event, meta}); }
  catch { /* Diagnostics cannot authorize work or declare coverage complete. */ }
}
async function status(phase, detail = '') {
  await chrome.storage.local.set({status: {phase, detail, at: new Date().toISOString()}});
  await chrome.action.setBadgeText({text: phase === 'failed' ? '!' : phase === 'complete' ? '✓' : phase === 'reading' ? '读' : ''});
  await observe(phase === 'failed' ? 'failure' : 'phase', {phase, detail});
}
async function logEvent(event) {
  const saved = await chrome.storage.local.get('events');
  const events = [...(saved.events || []), {event, at: new Date().toISOString()}].slice(-60);
  await chrome.storage.local.set({events});
}
async function ensureAlarm() {
  if (!await chrome.alarms.get(ALARM)) {
    await chrome.alarms.create(ALARM, {periodInMinutes: 1, delayInMinutes: 1, persistAcrossSessions: true});
  }
}
function validateTerminalState(state, runId) {
  if (state.run_id !== runId) throw new Error('terminal_run_mismatch');
  if (!['pending', 'partial', 'complete'].includes(state.status) ||
      !Array.isArray(state.completed_categories) ||
      new Set(state.completed_categories).size !== state.completed_categories.length ||
      state.completed_categories.some(category => !SOURCES.has(category)) ||
      (state.status === 'complete' && state.completed_categories.length !== SOURCES.size) ||
      (state.status === 'partial' && state.completed_categories.length === SOURCES.size)) {
    throw new Error('terminal_status_invalid');
  }
}
async function flushTerminalPending() {
  const saved = await chrome.storage.local.get('terminalPending');
  const pending = saved.terminalPending;
  if (!pending) return null;
  if (!/^extension-probe-[A-Za-z0-9_-]{1,100}$/.test(pending.runId || '') ||
      typeof pending.error !== 'string' || !pending.error || pending.error.length > 180) {
    throw new Error('terminal_pending_invalid');
  }
  // status is read-only and cannot create a new host run. A changed config
  // must not redirect an old failure into a newly scheduled batch.
  let state = await host({op: 'status'});
  validateTerminalState(state, pending.runId);
  if (state.status === 'pending') {
    const completed = new Set(state.completed_categories);
    for (const category of SOURCES.keys()) {
      if (completed.has(category)) continue;
      await host({op: 'finish_category', run_id: pending.runId, category_id: category,
        coverage: {complete: false, reason: 'read_failed', error: pending.error, dates: [], page_count: 0}});
    }
    const finished = await host({op: 'finish_run', run_id: pending.runId});
    validateTerminalState(finished, pending.runId);
    if (!['partial', 'complete'].includes(finished.status)) throw new Error('terminal_finish_unconfirmed');
    state = await host({op: 'status'});
    validateTerminalState(state, pending.runId);
  }
  if (!['partial', 'complete'].includes(state.status)) throw new Error('terminal_finish_unconfirmed');
  // If the previous reply or storage write was lost, the already-final host
  // state is sufficient. Do not rewrite categories or finish timestamps again.
  await chrome.storage.local.set({finishedRun: pending.runId, result: state,
    terminalResult: {...pending, status: state.status, completed_categories: state.completed_categories,
      confirmedAt: new Date().toISOString()}});
  await chrome.storage.local.remove('terminalPending');
  return state;
}
function validatePlan(plan) {
  if (!/^[A-Za-z0-9_-]{1,100}$/.test(plan.run_id || '')) throw new Error('invalid_run_id');
  if (plan.sources?.length !== 6 || new Set(plan.sources.map(s => s.category_id)).size !== 6) throw new Error('source_count');
  for (const source of plan.sources) {
    const fixed = SOURCES.get(source.category_id);
    if (!fixed || source.task_name !== fixed[0] || source.conversation_id !== fixed[1]) throw new Error('source_mismatch');
  }
  if (!Number.isFinite(Date.parse(plan.scan_start)) || !Number.isFinite(Date.parse(plan.scan_end))) throw new Error('invalid_scan_dates');
  if (plan.diagnostic != null) {
    const d = plan.diagnostic;
    const initial = Object.keys(d).length === 4 && d.max_visible_seconds === 120;
    const initializationContinuation = Object.keys(d).length === 5 && d.max_visible_seconds === 60 &&
      ['extension-probe-000000000000000000000002', 'extension-probe-000000000000000000000003']
        .includes(d.resume_from_run_id);
    if (!(initial || initializationContinuation) || d.kind !== 'visible_history_once' ||
        d.category_id !== 'ai-models' ||
        d.approval_id !== 'example-visible-history-consent') throw new Error('unapproved_visible_diagnostic');
  }
}
const urlFor = (source, runId) => `https://chatgpt.com/c/${source.conversation_id}#personal-intel-probe=${runId}`;
async function settleFocusVerification() {
  let pending;
  do {
    pending = focusVerification;
    await pending;
  } while (pending !== focusVerification);
}
async function checkOwned(expectedUrl = owned?.url, allowInitialNormal = false) {
  await settleFocusVerification();
  if (interrupted || !owned) throw new Error(interruptionReason);
  const win = await chrome.windows.get(owned.windowId, {populate: true});
  const tab = await chrome.tabs.get(owned.tabId);
  const signature = JSON.stringify([win.id, win.state, win.focused, win.tabs?.length]);
  if (signature !== lastWindowSignature) {
    await observe('window_checked', {id: win.id, state: win.state, focused: win.focused, tabCount: win.tabs?.length || 0});
    lastWindowSignature = signature;
  }
  const allowedState = win.state === (visibleDiagnostic ? 'normal' : 'minimized') ||
    (allowInitialNormal && win.state === 'normal');
  if (win.focused || !allowedState || win.tabs?.length !== 1 ||
      tab.windowId !== owned.windowId || !win.tabs.some(t => t.id === owned.tabId)) {
    interrupted = true;
    interruptionReason = 'collector_window_user_interaction';
    throw new Error('collector_window_user_interaction');
  }
  if (tab.url !== expectedUrl && tab.pendingUrl !== expectedUrl) throw new Error('collector_url_changed');
  // A focus query can finish while the window/tab checks or diagnostics await.
  // Confirm it before allowing the caller to read, navigate, or close the tab.
  await settleFocusVerification();
  if (interrupted) throw new Error(interruptionReason);
  return tab;
}
async function execute(func, args) {
  ensureVisibleTime();
  await checkOwned();
  const result = await chrome.scripting.executeScript({target: {tabId: owned.tabId}, world: 'MAIN', func, args});
  await checkOwned();
  if (result.length !== 1 || !result[0].result?.ok) throw new Error(result[0]?.result?.error || 'reader_failed');
  return result[0].result;
}
async function closeOwned() {
  if (!owned) return;
  try {
    await checkOwned();
    await chrome.tabs.remove(owned.tabId);
  } catch { /* Never close a window/tab the user has opened or repurposed. */ }
  owned = null;
  await chrome.storage.local.remove('owned');
}
async function openOwned(source, runId) {
  const url = urlFor(source, runId);
  const shellUrl = chrome.runtime.getURL('collector.html') +
    (visibleDiagnostic ? `#personal-intel-probe=${runId}` : '');
  ensureVisibleTime();
  const win = await chrome.windows.create({url: shellUrl, state: visibleDiagnostic ? 'normal' : 'minimized', focused: false, type: 'normal'});
  if (!win?.id || win.tabs?.length !== 1) throw new Error('collector_window_creation_failed');
  owned = {windowId: win.id, tabId: win.tabs[0].id, url: shellUrl, runId};
  if (visibleDiagnostic && (visibleExpired || Date.now() >= visibleDeadline)) {
    await closeOwned();
    throw new Error('visible_diagnostic_deadline');
  }
  await chrome.storage.local.set({owned});
  await observe('window_created', {id: win.id, state: win.state, focused: win.focused, tabCount: win.tabs.length});
  if (win.focused || interrupted) {
    interrupted = true;
    throw new Error('collector_creation_took_focus');
  }
  // Chrome may apply the initial state asynchronously. Finish minimizing our
  // own blank shell before loading any report, without requesting focus.
  await checkOwned(shellUrl, true);
  if (!visibleDiagnostic) await chrome.windows.update(win.id, {state: 'minimized'});
  await delay(250);
  await checkOwned();
  ensureVisibleTime();
  owned.url = url;
  await chrome.storage.local.set({owned});
  await checkOwned(shellUrl);
  await chrome.tabs.update(owned.tabId, {url});
  await checkOwned();
}

async function runVisibleDiagnostic(plan, saved) {
  // The grant is consumed before any window is created. Reloads and later
  // alarms can clean up but cannot silently repeat the visible experiment.
  const continuation = !!plan.diagnostic.resume_from_run_id;
  const finalInitialization = plan.diagnostic.resume_from_run_id === 'extension-probe-000000000000000000000003';
  const consumed = finalInitialization ? saved.visibleFinalInitializationConsumed === plan.diagnostic.approval_id :
    continuation ? saved.visibleContinuationConsumed === plan.diagnostic.approval_id :
    saved.visibleApprovalConsumed === plan.diagnostic.approval_id;
  if (consumed) {
    if (saved.owned?.runId === plan.run_id) {
      visibleDiagnostic = true;
      owned = saved.owned;
      await closeOwned();
      visibleDiagnostic = false;
    }
    return;
  }
  if (saved.owned) {
    owned = saved.owned;
    await checkOwned();
    await closeOwned();
  }
  await chrome.storage.local.set({visibleApprovalConsumed: plan.diagnostic.approval_id,
    ...(finalInitialization ? {visibleFinalInitializationConsumed: plan.diagnostic.approval_id} :
      continuation ? {visibleContinuationConsumed: plan.diagnostic.approval_id} : {}),
    activeRun: plan.run_id, visibleStartedAt: new Date().toISOString()});
  visibleDiagnostic = true;
  visibleExpired = false;
  const source = plan.sources.find(s => s.category_id === 'ai-models');
  const deadline = Date.now() + (continuation ? 45000 : 85000);
  visibleDeadline = deadline;
  visibleTimer = setTimeout(() => { visibleExpired = true; void closeOwned(); }, continuation ? 55000 : 100000);
  let coverage;
  try {
    await status('reading', '一次可见对照：仅 AI模型新发布');
    coverage = await scanSource(plan, source,
      {runId: plan.run_id, index: 0, seen: [], pages: 0, stationary: 0}, deadline);
    if (!coverage) coverage = {complete: false, reason: 'read_failed', error: 'visible_diagnostic_deadline'};
  } catch (error) {
    coverage = {complete: false, reason: 'read_failed', error: String(error.message).slice(0, 180)};
  } finally {
    // Finish visible work before report bookkeeping. The independent Windows
    // watchdog supplies a second deadline if the browser worker stalls.
    await closeOwned();
    clearTimeout(visibleTimer);
    visibleTimer = null;
    visibleExpired = true;
    visibleDiagnostic = false;
  }
  await host({op: 'finish_category', run_id: plan.run_id, category_id: 'ai-models', coverage});
  const result = await host({op: 'finish_run', run_id: plan.run_id});
  await chrome.storage.local.set({finishedRun: plan.run_id, result, visibleResult: coverage});
  await status('diagnostic_finished', coverage.complete ? '可见对照取得历史边界，尚未证明后台读取' : '可见对照未取得历史边界');
}
async function ready(source) {
  const expected = {title: source.task_name, conversation_id: source.conversation_id};
  const readyUntil = visibleDiagnostic ? Math.min(Date.now() + 20000, visibleDeadline) : Date.now() + 45000;
  let lastError = 'page_not_ready';
  for (let i = 0; i < 45 && Date.now() < readyUntil; i++) {
    await checkOwned();
    try {
      const snapshot = await execute(inspectPage, [expected]);
      if (i === 0 || i % 10 === 0) {
        await observe('reader_snapshot', {category: source.category_id, width: snapshot.viewport?.width || 0,
          height: snapshot.viewport?.height || 0, turnCount: snapshot.turns?.length || 0,
          loading: !!snapshot.loading, completeCount: snapshot.turns?.filter(t => t.state === 'complete').length || 0,
          dates: (snapshot.dates || []).slice(-20)});
      }
      if (snapshot.turns?.length) {
        await observe('reader_snapshot', {category: source.category_id, width: snapshot.viewport?.width || 0,
          height: snapshot.viewport?.height || 0, turnCount: snapshot.turns.length, dates: snapshot.dates || []});
        if (!(snapshot.viewport?.width > 0 && snapshot.viewport?.height > 0)) throw new Error('zero_viewport');
        await execute(installClipboardGuard, [expected]);
        if (visibleDiagnostic) {
          if (snapshot.viewport?.visibility !== 'visible') {
            lastError = 'visible_condition_not_established';
            await delay(500);
            continue;
          }
          const rendering = await execute(probeRendering, [expected]);
          await observe('reader_snapshot', {category: source.category_id, rendering});
          if (!rendering.raf_fired || rendering.visibility !== 'visible') {
            lastError = 'visible_rendering_not_established';
            await delay(500);
            continue;
          }
        }
        return snapshot;
      }
      lastError = snapshot.login_required ? 'login_required' : 'page_loading';
    } catch (error) {
      lastError = String(error.message).slice(0, 160);
      if (interrupted || /clipboard|collector_|login_required/.test(lastError)) throw error;
    }
    await delay(1000);
  }
  throw new Error(lastError);
}

async function scanSource(plan, source, progress, deadline) {
  ensureVisibleTime();
  const expected = {title: source.task_name, conversation_id: source.conversation_id};
  const start = Date.parse(source.scan_start || plan.scan_start);
  const end = Date.parse(plan.scan_end);
  if (!owned) {
    progress = {...progress, pages: 0, stationary: 0};
    await chrome.storage.local.set({progress});
    await openOwned(source, plan.run_id);
  }
  else if (owned.url !== urlFor(source, plan.run_id)) {
    await checkOwned();
    const previousUrl = owned.url;
    owned.url = urlFor(source, plan.run_id);
    await chrome.storage.local.set({owned});
    await checkOwned(previousUrl);
    await chrome.tabs.update(owned.tabId, {url: owned.url});
  }
  await ready(source);
  const seen = new Set(progress.seen || []);
  let pages = progress.pages || 0;
  let stationary = progress.stationary || 0;
  let busyCount = 0;
  let historyReentered = progress.historyReentered === true;
  const undatedAttempts = new Map();
  while (Date.now() < deadline) {
    const snapshot = await execute(inspectPage, [expected]);
    if (pages % 5 === 0 || (snapshot.loading && busyCount % 10 === 0)) {
      await observe('reader_snapshot', {category: source.category_id, pageCount: pages,
        turnCount: snapshot.turns?.length || 0, loading: !!snapshot.loading,
        loaderPresent: !!snapshot.history_loader_present,
        loaderVisible: snapshot.loading_evidence?.visible_history_text_nodes || 0,
        busyRegions: snapshot.loading_evidence?.visible_busy_regions || 0,
        signals: snapshot.loading_evidence?.signals || [],
        top: snapshot.scroll?.top ?? null, height: snapshot.scroll?.height ?? null,
        clientHeight: snapshot.scroll?.clientHeight ?? null,
        reverse: snapshot.scroll?.reverse ?? null, atTop: snapshot.scroll?.at_top ?? null,
        dates: (snapshot.dates || []).slice(-20)});
    }
    const turns = snapshot.turns || [];
    if (turns.some(t => typeof t.turn_key !== 'string' || !t.turn_key) ||
        new Set(turns.map(t => t.turn_key)).size !== turns.length) throw new Error('ambiguous_turn_identity');
    const datedTurns = turns.filter(t => t.report_time != null);
    if (datedTurns.some(t => !Number.isFinite(Date.parse(t.report_time)))) throw new Error('invalid_report_time');
    const orderedTimes = datedTurns.map(t => Date.parse(t.report_time));
    if (orderedTimes.some((value, index) => index > 0 && value < orderedTimes[index - 1])) {
      throw new Error('history_order_not_ascending');
    }
    // Actual DOM chronology is oldest to newest (008b evidence). Check it
    // before reversing; never use inspectPage's separately sorted date list
    // to infer order or let a test fixture authorize a different layout.
    const canStopAtBoundary = new Set(orderedTimes).size >= 2;
    const times = datedTurns.map(t => t.report_time);
    let traversedBoundary = null;
    let awaitingDate = false;
    for (const turn of [...turns].reverse()) {
      if (canStopAtBoundary && turn.report_time && Date.parse(turn.report_time) < start) {
        traversedBoundary = turn.report_time;
        break;
      }
      if (seen.has(turn.turn_key)) continue;
      if (turn.assistant_count > 1 || typeof turn.is_report !== 'boolean') throw new Error('ambiguous_report_structure');
      if (!turn.is_report) {
        if (turn.is_pause_notice || turn.is_task_notice) {
          await host({op: 'save_report', run_id: plan.run_id, category_id: source.category_id,
            report: {title: source.task_name, path: `/c/${source.conversation_id}`, turn_key: turn.turn_key, is_report: false}});
        }
        seen.add(turn.turn_key);
        continue;
      }
      if (!turn.report_time) {
        const attempt = (undatedAttempts.get(turn.turn_key) || 0) + 1;
        undatedAttempts.set(turn.turn_key, attempt);
        if (attempt === 1 || attempt === 10) await observe('reader_snapshot', {
          category: source.category_id, issue: 'undated_report', attempt,
          turnCount: snapshot.turns.length, dates: times.slice(-25),
          undated: {key: turn.turn_key, state: turn.state, assistantCount: turn.assistant_count,
            charCount: turn.text_length, hasCopy: turn.has_copy},
          chronology: snapshot.turns.slice(0, 20).map(t => ({key: t.turn_key,
            time: t.report_time, state: t.state, report: t.is_report}))});
        if (attempt >= 10) throw new Error('undated_report');
        awaitingDate = true;
        break;
      }
      const time = Date.parse(turn.report_time);
      if (!Number.isFinite(time)) throw new Error('invalid_report_time');
      if (time < start || time > end) continue;
      if (!turn.has_copy) throw new Error('report_copy_missing');
      const capture = await execute(captureTurn, [expected, turn.turn_key]);
      const report = capture.report || capture;
      report.title = source.task_name; // The reader already checked the exact title or ChatGPT suffix.
      if (Date.parse(report.report_time) !== time) throw new Error('report_time_changed');
      await host({op: 'save_report', run_id: plan.run_id, category_id: source.category_id, report});
      seen.add(turn.turn_key);
      await chrome.storage.local.set({progress: {...progress, seen: [...seen], pages, stationary}});
    }
    // DOM updates can insert a turn before its date. Wait briefly without
    // skipping it or advancing coverage; persistent ambiguity still fails.
    if (awaitingDate) { await delay(1000); continue; }
    const boundary = traversedBoundary || times.filter(t => Date.parse(t) < start).sort((a,b) => Date.parse(a)-Date.parse(b))[0];
    if (boundary) return {complete: true, reason: 'boundary_before_start', boundary_time: boundary, dates: times, page_count: pages};
    if (snapshot.atStartEvidence?.complete === true) {
      return {complete: true, reason: 'conversation_start', reached_start: true, dates: times, page_count: pages};
    }
    if (snapshot.loading && snapshot.scroll?.at_top) {
      if (busyCount === 0) {
        const rendering = await execute(probeRendering, [expected]);
        await observe('reader_snapshot', {category: source.category_id, rendering});
      }
      if (busyCount === 3 && !historyReentered) {
        historyReentered = true;
        await chrome.storage.local.set({progress: {...progress, seen: [...seen], pages, stationary, historyReentered}});
        const movement = await execute(scrollOlder, [expected, {reenterTop: true,
          ...(!visibleDiagnostic ? {notifyScroll: true} : {})}]);
        await observe('reader_snapshot', {category: source.category_id,
          reentered: movement.reentered === true,
          scrollEventDispatched: movement.scroll_event_dispatched === true,
          top: movement.after?.top ?? null,
          turnCount: movement.after?.turn_count ?? null, dates: movement.after?.dates || []});
      }
      if (++busyCount >= 40) throw new Error('history_loading_timeout_without_boundary');
      await delay(1500);
      continue;
    }
    busyCount = 0;
    if (++pages > 180) throw new Error('history_page_limit');
    // All currently rendered, in-range turns have already been captured above.
    // Jump only inside that same owned scroll container; older turns must still
    // actually load and supply a dated boundary before coverage can pass.
    const moved = await execute(scrollOlder, [expected, {jumpToTop: true}]);
    const beforeKeys = (snapshot.turns || []).map(t => t.turn_key).join('|');
    await delay(1600);
    const after = await execute(inspectPage, [expected]);
    const afterKeys = (after.turns || []).map(t => t.turn_key).join('|');
    stationary = !moved.moved && beforeKeys === afterKeys && !after.loading ? stationary + 1 : 0;
    if (stationary >= 10) throw new Error('history_boundary_not_proven');
    await chrome.storage.local.set({progress: {...progress, seen: [...seen], pages, stationary, historyReentered}});
  }
  return null;
}

async function run(event) {
  if (running) return;
  running = true;
  interrupted = false;
  interruptionReason = 'collector_interrupted';
  currentRunId = null;
  try {
    await logEvent(event);
    await ensureAlarm();
    const terminal = await flushTerminalPending();
    if (terminal) {
      const saved = await chrome.storage.local.get('owned');
      if (saved.owned?.runId === terminal.run_id) { owned = saved.owned; await closeOwned(); }
      await status(terminal.status === 'complete' ? 'complete' : 'failed',
        terminal.status === 'complete' ? '已核对上次采集完成状态' : '上次采集异常已记录，等待后续补收');
      return;
    }
    const plan = await host({op: 'get_plan', event});
    if (plan.status === 'disabled') {
      const saved = await chrome.storage.local.get(['owned', 'activeRun', 'blockedRun']);
      const candidate = saved.owned;
      const validRun = candidate && /^extension-probe-[A-Za-z0-9_-]{1,100}$/.test(candidate.runId || '');
      const validUrl = validRun && (candidate.url === chrome.runtime.getURL('collector.html') ||
        [...SOURCES.values()].some(([, id]) => candidate.url ===
          `https://chatgpt.com/c/${id}#personal-intel-probe=${candidate.runId}`));
      if (validUrl && saved.activeRun === candidate.runId && saved.blockedRun !== candidate.runId) {
        owned = candidate;
        try {
          // A reload can leave the old worker's minimized shell behind.
          // Re-prove its single-tab identity; never close an adopted window.
          await checkOwned();
          await chrome.tabs.remove(owned.tabId);
          await chrome.storage.local.remove('owned');
        } catch {
          await chrome.storage.local.set({blockedRun: candidate.runId});
        } finally { owned = null; }
      }
      await status('disabled', '等待开启验证');
      return;
    }
    if (plan.status === 'awaiting_startup') {
      await status('waiting_startup', '等待浏览器真实启动后进行一次后台验证');
      return;
    }
    if (plan.status === 'complete') {
      const saved = await chrome.storage.local.get('owned');
      if (saved.owned?.runId === plan.run_id) { owned = saved.owned; await closeOwned(); }
      await status('complete', '本次验证读取已结束，生产任务尚未切换');
      return;
    }
    validatePlan(plan);
    currentRunId = plan.run_id;
    const saved = await chrome.storage.local.get(['progress', 'owned', 'finishedRun', 'blockedRun',
      'visibleApprovalConsumed', 'visibleContinuationConsumed', 'visibleFinalInitializationConsumed']);
    if (saved.finishedRun === plan.run_id || saved.blockedRun === plan.run_id) return;
    if (plan.diagnostic) {
      await runVisibleDiagnostic(plan, saved);
      return;
    }
    await chrome.storage.local.set({activeRun: plan.run_id});
    let progress = saved.progress?.runId === plan.run_id ? saved.progress : {runId: plan.run_id, index: 0, seen: [], pages: 0, stationary: 0};
    if (saved.owned) {
      owned = saved.owned;
      try {
        await checkOwned();
        if (owned.runId !== plan.run_id) await closeOwned();
      } catch {
        if (interrupted) throw new Error('collector_window_user_interaction');
        owned = null;
      }
    }
    const deadline = Date.now() + 210000;
    for (let i = progress.index; i < plan.sources.length; i++) {
      const source = plan.sources[i];
      if ((plan.completed_categories || []).includes(source.category_id)) {
        progress = {runId: plan.run_id, index: i + 1, seen: [], pages: 0, stationary: 0};
        await chrome.storage.local.set({progress});
        continue;
      }
      await status('reading', source.task_name);
      let coverage;
      try {
        coverage = await scanSource(plan, source, progress, deadline);
      } catch (error) {
        coverage = {complete: false, reason: 'read_failed', error: String(error.message).slice(0, 180)};
        if (interrupted) throw error;
        await closeOwned();
      }
      if (!coverage) { await status('waiting', '已保存进度，下一次后台唤醒继续'); return; }
      await host({op: 'finish_category', run_id: plan.run_id, category_id: source.category_id, coverage});
      progress = {runId: plan.run_id, index: i + 1, seen: [], pages: 0, stationary: 0};
      await chrome.storage.local.set({progress});
      if (Date.now() >= deadline) return;
    }
    const result = await host({op: 'finish_run', run_id: plan.run_id});
    await chrome.storage.local.set({finishedRun: plan.run_id, result});
    await closeOwned();
    await status(result.status === 'complete' ? 'complete' : 'failed', result.status === 'complete' ? '六类读取完成，等待隔离与启动验收' : '部分读取未通过，详见本机验证记录');
  } catch (error) {
    const detail = String(error.message || error).slice(0, 180) || 'collector_failed';
    const saved = await chrome.storage.local.get(['activeRun', 'terminalPending']);
    // Only a run activated by this invocation can acquire a new terminal
    // failure. A host error before get_plan cannot blame an older activeRun.
    if (currentRunId && saved.activeRun === currentRunId) {
      await chrome.storage.local.set({blockedRun: currentRunId,
        ...(!saved.terminalPending ? {terminalPending: {runId: currentRunId,
          error: detail, at: new Date().toISOString()}} : {})});
    }
    await status('failed', detail);
    if (currentRunId && saved.activeRun === currentRunId) {
      try { await flushTerminalPending(); }
      catch (terminalError) {
        await status('failed', `采集已停止，失败状态待确认：${String(terminalError.message).slice(0, 120)}`);
      }
    }
    await closeOwned();
  } finally { running = false; }
}

chrome.windows.onFocusChanged.addListener(id => {
  const candidate = owned;
  if (!candidate || id !== candidate.windowId) return;
  // Notifications may arrive after Chrome has already minimized the window.
  // Query immediately, but never treat the notification itself as user action.
  // Capture the owner object so a late reply cannot stop a replacement window.
  const verification = chrome.windows.get(id, {populate: true}).then(async win => {
    if (owned !== candidate) return;
    if (win.focused) {
      interrupted = true;
      interruptionReason = 'collector_window_user_interaction';
    }
    await observe('window_checked', {id: win.id, state: win.state, focused: win.focused,
      tabCount: win.tabs?.length || 0, trigger: 'focus_notification', confirmed: win.focused});
  }).catch(() => {
    if (owned !== candidate) return;
    interrupted = true;
    interruptionReason = 'collector_window_unavailable';
  });
  focusVerification = Promise.all([focusVerification, verification]).then(() => {});
});
chrome.runtime.onInstalled.addListener(() => { void run('installed'); });
chrome.runtime.onStartup.addListener(() => { void run('startup'); });
chrome.alarms.onAlarm.addListener(alarm => { if (alarm.name === ALARM) void run('alarm'); });
chrome.runtime.onMessage.addListener((message, sender, respond) => {
  if (sender.id !== chrome.runtime.id || message?.op !== 'status') return;
  chrome.storage.local.get(['status', 'events', 'result']).then(respond);
  return true;
});
