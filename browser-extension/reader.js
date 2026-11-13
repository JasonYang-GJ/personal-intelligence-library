/* Each export is serialized by chrome.scripting.executeScript({world:'MAIN'}).
 * Keep every function self-contained: no imported helpers or module closures.
 * They may only touch a collector-owned tab marked by the exact URL hash.
 */

export async function installClipboardGuard(expected) {
  const title = document.title;
  const path = location.pathname;
  const allowedTitles = expected && [expected.title, `${expected.title} - ChatGPT`,
    `${expected.title} | ChatGPT`, `${expected.title} — ChatGPT`];
  if (!expected || !/^[0-9a-f-]{36}$/i.test(expected.conversation_id || '') ||
      location.protocol !== 'https:' || location.hostname !== 'chatgpt.com' ||
      path !== `/c/${expected.conversation_id}` || !allowedTitles.includes(title) ||
      !/^#personal-intel-probe=[A-Za-z0-9_-]{1,100}$/.test(location.hash)) {
    return {ok: false, error: 'unexpected_conversation_or_unowned_page', title, path};
  }
  // Installation must have happened at document_start. Installing after the
  // site loaded would miss any clipboard methods it had already retained.
  const guard = window.__personalIntelClipboardGuardV1;
  if (!guard || typeof guard.check !== 'function') return {ok: false, error: 'early_clipboard_guard_missing'};
  const status = guard.check();
  return {...status, error: status.ok ? null : 'early_clipboard_guard_unavailable'};
}

export async function inspectPage(expected) {
  const title = document.title;
  const path = location.pathname;
  const hasLogin = Array.from(document.querySelectorAll('button')).some(button =>
    /^(登录|Log in|Sign in)$/.test((button.innerText || '').trim()));
  const titleMatches = expected && [expected.title, `${expected.title} - ChatGPT`,
    `${expected.title} | ChatGPT`, `${expected.title} — ChatGPT`].includes(title);
  const viewport = {width: window.innerWidth, height: window.innerHeight,
    visibility: document.visibilityState, ready_state: document.readyState};
  if (!expected || !/^[0-9a-f-]{36}$/i.test(expected.conversation_id || '') ||
      location.protocol !== 'https:' || location.hostname !== 'chatgpt.com' ||
      path !== `/c/${expected.conversation_id}` || !titleMatches ||
      !/^#personal-intel-probe=[A-Za-z0-9_-]{1,100}$/.test(location.hash)) {
    return {ok: false, error: hasLogin ? 'login_required' : 'unexpected_conversation_or_unowned_page',
      title, path, viewport, has_login: hasLogin};
  }
  // Exact administrative receipts created while applying the user's two-week
  // trial. Identity AND full DOM text must match; never infer from a keyword,
  // short length, missing date, or the unstable fallback turn index.
  // These synthetic receipt examples must be replaced or removed for a personal deployment.
  const adminReceipts = expected.conversation_id === '00000000-0000-4000-8000-000000000008' ? {
    '00000000-0000-4000-8000-000000000006': '合成配置回执 1：仅供测试，不代表任何真实任务。',
    '00000000-0000-4000-8000-000000000001': '合成配置回执 2：仅供测试，不代表任何真实任务。'
  } : {};
  const nodes = Array.from(document.querySelectorAll('[data-content-search-turn-key]'));
  const turns = nodes.map(turn => {
    const bodies = Array.from(turn.querySelectorAll('[data-markdown-text-style="assistant-message"]'));
    const body = bodies.length === 1 ? bodies[0] : null;
    const bodyText = body?.innerText || '';
    const copies = Array.from(turn.querySelectorAll('button')).filter(button =>
      ['复制', 'Copy'].includes(button.getAttribute('aria-label')));
    const time = turn.querySelector('time[datetime]')?.dateTime || null;
    const state = turn.getAttribute('data-talvt-turn-state') ||
      turn.querySelector('[data-talvt-turn-state]')?.getAttribute('data-talvt-turn-state') || null;
    const pauseNotice = bodyText.length < 500 &&
      (/^I've paused this task because there haven't been any updates in a while\. You can resume it in the Scheduled tab or by asking ChatGPT\.$/.test(bodyText.trim()) ||
       /^(由于|因为).{0,80}(没有更新|没有新内容).{0,50}(已暂停|暂停了).{0,100}(恢复|重新开启)/.test(bodyText.trim()));
    // Confirmed platform-only notice. Never infer this from length, a missing
    // timestamp, or a matching prefix; additional prose remains report content.
    const messageId = body?.closest('[data-chatgpt-selection-message-id]')?.getAttribute('data-chatgpt-selection-message-id') || null;
    const adminReceipt = !!body && state === 'complete' &&
      Object.hasOwn(adminReceipts, messageId) && bodyText === adminReceipts[messageId];
    const monitoringNotice = !!body && state === 'complete' && bodyText.trim() ===
      "This scheduled task hasn't produced any updates in a while. Do you want me to keep monitoring for you?";
    const taskNotice = monitoringNotice || adminReceipt;
    return {turn_key: turn.getAttribute('data-content-search-turn-key'),
      report_time: time,
      message_id: messageId,
      state,
      assistant_count: bodies.length, text_length: bodyText.length,
      has_copy: copies.length === 1, copy_count: copies.length,
      is_report: !!body && !pauseNotice && !taskNotice, is_pause_notice: pauseNotice,
      is_task_notice: taskNotice, notice_kind: adminReceipt ? 'automation_configuration_receipt' : monitoringNotice ? 'monitoring_confirmation' : null,
      notice_text: taskNotice ? bodyText : null,
      body_text: bodyText.slice(0, 160), time_valid: time === null || Number.isFinite(Date.parse(time))};
  });
  // The nearest scrollable common ancestor avoids the sidebar and nested
  // markdown/code/table scrollers. A document scroll root is a final fallback.
  let scroller = null;
  if (nodes.length) {
    for (let node = nodes[0].parentElement; node; node = node.parentElement) {
      if (!nodes.every(turn => node.contains(turn))) continue;
      const style = getComputedStyle(node);
      if (/(auto|scroll|overlay)/.test(style.overflowY) &&
          node.clientHeight > 0 && node.scrollHeight > node.clientHeight + 2) { scroller = node; break; }
    }
  }
  if (!scroller) {
    const root = document.scrollingElement;
    if (root && root.clientHeight > 0 && root.scrollHeight > root.clientHeight + 2) scroller = root;
  }
  const style = scroller ? getComputedStyle(scroller) : null;
  const reverse = style?.flexDirection === 'column-reverse';
  const scroll = scroller ? {top: scroller.scrollTop, height: scroller.scrollHeight,
    clientHeight: scroller.clientHeight, reverse,
    at_top: reverse ? scroller.scrollTop <= -(scroller.scrollHeight - scroller.clientHeight) + 3 : scroller.scrollTop <= 3,
    node: scroller.tagName.toLowerCase(), role: scroller.getAttribute('role'),
    is_document: scroller === document.scrollingElement} : null;
  // A permanent, offscreen history sentinel is not an active request. Report
  // prose is untrusted content and must never become a UI loading signal.
  const loadingScope = scroller || nodes[0]?.closest('main') || document.body;
  const loaderText = /^(?:正在加载更早的消息|Loading (?:earlier|older) messages)(?:\s*(?:…|\.{1,3}))?$/i;
  const outsideReport = element => !element.closest('[data-content-search-turn-key]') &&
    !element.closest('[data-markdown-text-style]') && !element.closest('[data-message-author-role]');
  const isVisible = element => {
    if (!element.isConnected || typeof element.getBoundingClientRect !== 'function') return false;
    const rect = element.getBoundingClientRect();
    let left = Math.max(0, rect.left), top = Math.max(0, rect.top);
    let right = Math.min(window.innerWidth, rect.right), bottom = Math.min(window.innerHeight, rect.bottom);
    if (!(rect.width > 0 && rect.height > 0 && right > left && bottom > top)) return false;
    for (let node = element; node; node = node.parentElement) {
      const css = getComputedStyle(node);
      if (css.display === 'none' || css.visibility === 'hidden' || css.visibility === 'collapse' ||
          css.contentVisibility === 'hidden' || css.opacity === '0' || node.hidden ||
          node.getAttribute('aria-hidden') === 'true') return false;
      if (node !== element && /(auto|scroll|hidden|clip)/.test(`${css.overflowX} ${css.overflowY}`)) {
        const clip = node.getBoundingClientRect();
        left = Math.max(left, clip.left); top = Math.max(top, clip.top);
        right = Math.min(right, clip.right); bottom = Math.min(bottom, clip.bottom);
        if (!(right > left && bottom > top)) return false;
      }
    }
    return true;
  };
  const uiNodes = loadingScope ? [loadingScope, ...loadingScope.querySelectorAll('*')].filter(outsideReport) : [];
  const textOf = element => (element.textContent || element.getAttribute('aria-label') || '').trim();
  const loaders = uiNodes.filter(element => loaderText.test(textOf(element)) &&
    !Array.from(element.children).some(child => loaderText.test(textOf(child))));
  const loadingSignals = new Set();
  let visibleLoaders = 0;
  for (const loader of loaders) {
    if (!isVisible(loader)) continue;
    visibleLoaders += 1;
    for (let node = loader; node && node !== loadingScope?.parentElement; node = node.parentElement) {
      if (node.getAttribute('aria-busy') === 'true') loadingSignals.add('visible_history_aria_busy');
      if (['status', 'progressbar'].includes(node.getAttribute('role'))) loadingSignals.add('visible_history_status');
      if (['polite', 'assertive'].includes(node.getAttribute('aria-live'))) loadingSignals.add('visible_history_live_region');
    }
  }
  const visibleBusyRegions = uiNodes.filter(element => element.getAttribute('aria-busy') === 'true' &&
    (element === loadingScope || nodes.some(turn => element.contains(turn)) || loaders.some(loader => element.contains(loader))) &&
    isVisible(element)).length;
  if (visibleBusyRegions) loadingSignals.add('visible_conversation_aria_busy');
  const loading = loadingSignals.size > 0;
  const historyLoaderPresent = loaders.length > 0;
  const loadingEvidence = {history_text_nodes: loaders.length, visible_history_text_nodes: visibleLoaders,
    visible_busy_regions: visibleBusyRegions, signals: Array.from(loadingSignals)};
  const dates = Array.from(new Set(turns.map(turn => turn.report_time).filter(Boolean))).sort();
  return {ok: true, title, path, viewport, has_login: hasLogin, turns, dates, loading, scroll,
    history_loader_present: historyLoaderPresent, loading_evidence: loadingEvidence,
    guard: window.__personalIntelClipboardGuardV1?.check?.() || {ok: false, installed_early: false},
    // Neither scrollTop=0 nor an unchanged list proves the beginning. No
    // verified start marker exists yet for this ChatGPT DOM, so fail closed.
    atStartEvidence: {complete: false, reason: 'no_verified_conversation_start_marker',
      at_top: scroll?.at_top ?? null, first_turn_key: turns[0]?.turn_key || null,
      first_turn_time: turns[0]?.report_time || null, loading},
    error: viewport.width <= 0 || viewport.height <= 0 ? 'zero_viewport' : null};
}

export async function captureTurn(expected, turnKey) {
  const title = document.title;
  const path = location.pathname;
  const titleMatches = expected && [expected.title, `${expected.title} - ChatGPT`,
    `${expected.title} | ChatGPT`, `${expected.title} — ChatGPT`].includes(title);
  if (!expected || !/^[0-9a-f-]{36}$/i.test(expected.conversation_id || '') ||
      location.protocol !== 'https:' || location.hostname !== 'chatgpt.com' ||
      path !== `/c/${expected.conversation_id}` || !titleMatches ||
      !/^#personal-intel-probe=[A-Za-z0-9_-]{1,100}$/.test(location.hash)) {
    return {ok: false, error: 'unexpected_conversation_or_unowned_page', title, path};
  }
  const guard = window.__personalIntelClipboardGuardV1;
  if (!guard?.check?.().ok) return {ok: false, error: 'early_clipboard_guard_unavailable'};
  const matching = Array.from(document.querySelectorAll('[data-content-search-turn-key]')).filter(
    turn => turn.getAttribute('data-content-search-turn-key') === turnKey);
  if (matching.length !== 1) return {ok: false, error: 'turn_not_unique_or_not_rendered'};
  const turn = matching[0];
  const bodies = Array.from(turn.querySelectorAll('[data-markdown-text-style="assistant-message"]'));
  const copies = Array.from(turn.querySelectorAll('button')).filter(button =>
    ['复制', 'Copy'].includes(button.getAttribute('aria-label')));
  const state = turn.getAttribute('data-talvt-turn-state') ||
    turn.querySelector('[data-talvt-turn-state]')?.getAttribute('data-talvt-turn-state');
  if (bodies.length !== 1 || copies.length !== 1 || state !== 'complete' || copies[0].disabled) {
    return {ok: false, error: 'report_or_copy_not_unique_and_complete'};
  }
  const body = bodies[0];
  const renderedText = body.innerText || '';
  const reportTime = turn.querySelector('time[datetime]')?.dateTime || null;
  const isTaskNotice = renderedText.trim() ===
    "This scheduled task hasn't produced any updates in a while. Do you want me to keep monitoring for you?";
  if ((!reportTime && !isTaskNotice) || (reportTime && !Number.isFinite(Date.parse(reportTime)))) {
    return {ok: false, error: 'report_time_missing_or_invalid'};
  }
  let began = false;
  try {
    guard.begin(turnKey);
    began = true;
    copies[0].click();
    let snapshot;
    for (let attempt = 0; attempt < 40; attempt += 1) {
      await new Promise(resolve => setTimeout(resolve, 100));
      snapshot = guard.snapshot();
      if (snapshot.errors.length || snapshot.legacy_copy_calls || snapshot.clipboard_read_calls) break;
      if (snapshot.writes.length && snapshot.pending === 0) {
        await new Promise(resolve => setTimeout(resolve, 150));
        snapshot = guard.snapshot();
        break;
      }
    }
    if (!guard.check().ok || snapshot.active !== turnKey || snapshot.writes.length !== 1 ||
        snapshot.pending || snapshot.copy_intercepted !== 1 || snapshot.legacy_copy_calls ||
        snapshot.clipboard_read_calls || snapshot.real_clipboard_calls || snapshot.errors.length ||
        !snapshot.writes[0]) return {ok: false, error: 'copy_capture_incomplete',
          guard: {copy_intercepted: snapshot.copy_intercepted, real_clipboard_calls: snapshot.real_clipboard_calls,
            legacy_copy_calls: snapshot.legacy_copy_calls, clipboard_read_calls: snapshot.clipboard_read_calls}};
    if (location.pathname !== path || document.title !== title ||
        turn.getAttribute('data-content-search-turn-key') !== turnKey || !turn.isConnected) {
      return {ok: false, error: 'page_or_turn_changed_during_copy'};
    }
    const isPause = renderedText.length < 500 &&
      (/^I've paused this task because there haven't been any updates in a while\. You can resume it in the Scheduled tab or by asking ChatGPT\.$/.test(renderedText.trim()) ||
       /^(由于|因为).{0,80}(没有更新|没有新内容).{0,50}(已暂停|暂停了).{0,100}(恢复|重新开启)/.test(renderedText.trim()));
    return {ok: true, title, path, turn_key: turnKey, report_time: reportTime,
      message_id: body.closest('[data-chatgpt-selection-message-id]')?.getAttribute('data-chatgpt-selection-message-id') || null,
      text: snapshot.writes[0], rendered_text: renderedText, is_report: !isPause && !isTaskNotice,
      is_pause_notice: isPause,
      is_task_notice: isTaskNotice, notice_kind: isTaskNotice ? 'monitoring_confirmation' : null,
      notice_text: isTaskNotice ? renderedText : null,
      links: Array.from(body.querySelectorAll('a[href]')).filter(link => /^https?:$/.test(new URL(link.href, location.href).protocol))
        .map(link => ({text: link.innerText || link.textContent || '', href: link.href})),
      table_count: body.querySelectorAll('table').length,
      history_dates: Array.from(document.querySelectorAll('time[datetime]')).map(time => time.dateTime).filter(Boolean),
      guard: {copy_intercepted: 1, real_clipboard_calls: 0, legacy_copy_calls: 0, clipboard_read_calls: 0,
        installed_early: true, scope: 'guard_calls_only_requires_os_monitor_for_acceptance'}};
  } catch (error) { return {ok: false, error: String(error?.message || error).slice(0, 160)}; }
  finally { if (began) guard.end(); }
}

export async function scrollOlder(expected, options = {}) {
  const title = document.title;
  const path = location.pathname;
  const ownedHash = location.hash;
  const titleMatches = expected && [expected.title, `${expected.title} - ChatGPT`,
    `${expected.title} | ChatGPT`, `${expected.title} — ChatGPT`].includes(title);
  if (!expected || !/^[0-9a-f-]{36}$/i.test(expected.conversation_id || '') ||
      location.protocol !== 'https:' || location.hostname !== 'chatgpt.com' ||
      path !== `/c/${expected.conversation_id}` || !titleMatches ||
      !/^#personal-intel-probe=[A-Za-z0-9_-]{1,100}$/.test(location.hash)) {
    return {ok: false, error: 'unexpected_conversation_or_unowned_page', title, path};
  }
  if (window.innerWidth <= 0 || window.innerHeight <= 0) return {ok: false, error: 'zero_viewport'};
  const nodes = Array.from(document.querySelectorAll('[data-content-search-turn-key]'));
  if (!nodes.length) return {ok: false, error: 'no_rendered_turns'};
  let scroller = null;
  for (let node = nodes[0].parentElement; node; node = node.parentElement) {
    if (!nodes.every(turn => node.contains(turn))) continue;
    const style = getComputedStyle(node);
    if (/(auto|scroll|overlay)/.test(style.overflowY) && node.clientHeight > 0 &&
        node.scrollHeight > node.clientHeight + 2) { scroller = node; break; }
  }
  if (!scroller) {
    const root = document.scrollingElement;
    if (root && root.clientHeight > 0 && root.scrollHeight > root.clientHeight + 2) scroller = root;
  }
  if (!scroller) return {ok: false, error: 'history_scroll_container_not_found',
    atStartEvidence: {complete: false, reason: 'no_verified_conversation_start_marker'}};
  const reverse = getComputedStyle(scroller).flexDirection === 'column-reverse';
  const sample = () => ({top: scroller.scrollTop, height: scroller.scrollHeight,
    clientHeight: scroller.clientHeight,
    turn_count: document.querySelectorAll('[data-content-search-turn-key]').length,
    dates: Array.from(document.querySelectorAll('time[datetime]')).map(time => time.dateTime).filter(Boolean).sort()});
  const before = sample();
  const step = Math.max(200, Math.floor(scroller.clientHeight * 0.8));
  const minimum = reverse ? -(scroller.scrollHeight - scroller.clientHeight) : 0;
  const jumpToTop = options?.jumpToTop === true;
  const reenterTop = options?.reenterTop === true;
  const notifyScroll = options?.notifyScroll === true;
  const targetTop = reenterTop || jumpToTop ? minimum : Math.max(minimum, scroller.scrollTop - step);
  const stillOwned = () => location.protocol === 'https:' && location.hostname === 'chatgpt.com' &&
    location.pathname === path && document.title === title && location.hash === ownedHash && scroller.isConnected;
  const hiddenUnfocused = () => document.visibilityState === 'hidden' &&
    typeof document.hasFocus === 'function' && document.hasFocus() === false;
  const visibilityAtStart = document.visibilityState;
  let reentered = false;
  let excursionTop = null;
  let excursionObservedTop = null;
  let scrollEventDispatched = false;
  if (notifyScroll && !reenterTop) return {ok: false, error: 'scroll_notify_requires_reentry', scroll_event_dispatched: false};
  if (notifyScroll && scroller === document.scrollingElement) {
    return {ok: false, error: 'scroll_notify_document_root_unsupported', scroll_event_dispatched: false};
  }
  if (notifyScroll && !hiddenUnfocused()) {
    return {ok: false, error: 'scroll_notify_requires_hidden_unfocused_page', scroll_event_dispatched: false};
  }
  // No keyboard, focus(), wheel dispatch, pointer, or clipboard interaction.
  // Assigning scrollTop confines movement to this own background document.
  if (reenterTop) {
    const atTop = reverse ? scroller.scrollTop <= minimum + 3 : scroller.scrollTop <= 3;
    if (!atTop) return {ok: false, error: 'reenter_requires_history_top', reentered: false, before};
    if (!stillOwned()) return {ok: false, error: 'page_or_scroll_container_changed', reentered: false, before};
    if (typeof document.hasFocus === 'function' && document.hasFocus()) {
      return {ok: false, error: 'collector_page_has_user_focus', reentered: false, before};
    }
    excursionTop = reverse ? Math.min(0, minimum + scroller.clientHeight) :
      Math.min(scroller.scrollHeight - scroller.clientHeight, scroller.clientHeight);
    scroller.scrollTop = excursionTop;
    await new Promise(resolve => setTimeout(resolve, 650));
    // A navigation, replaced scroller, foregrounded collector, or intervening
    // scroll stops the experiment. Never "restore" a page the user changed.
    if (!stillOwned()) return {ok: false, error: 'page_or_scroll_container_changed', reentered: false, before, excursion_top: excursionTop};
    if (document.visibilityState !== visibilityAtStart ||
        (typeof document.hasFocus === 'function' && document.hasFocus())) {
      return {ok: false, error: 'collector_page_visibility_or_focus_changed', reentered: false, before, excursion_top: excursionTop};
    }
    if (Math.abs(scroller.scrollTop - excursionTop) > 3) {
      return {ok: false, error: 'scroll_position_changed_during_reenter', reentered: false, before,
        excursion_top: excursionTop, after: sample()};
    }
    excursionObservedTop = scroller.scrollTop;
    if (notifyScroll && (!hiddenUnfocused() || Math.abs(excursionObservedTop - before.top) <= 0.5)) {
      return {ok: false, error: 'scroll_notify_excursion_not_confirmed', reentered: false,
        scroll_event_dispatched: false, before, excursion_top: excursionTop, after: sample()};
    }
  }
  // This is the second ownership check for a reentry, immediately before the
  // return movement, and also protects the existing ordinary scroll modes.
  if (!stillOwned()) return {ok: false, error: 'page_or_scroll_container_changed', reentered: false, before};
  scroller.scrollTop = targetTop;
  if (reenterTop) reentered = true;
  await new Promise(resolve => setTimeout(resolve, 650));
  if (!stillOwned()) {
    return {ok: false, error: 'page_or_scroll_container_changed', reentered, before};
  }
  const after = sample();
  if (notifyScroll) {
    // A synthetic scroll notification is only a one-shot diagnostic after two
    // observed DOM movements. It cannot authorize a history checkpoint.
    if (!stillOwned() || !hiddenUnfocused() || Math.abs(scroller.scrollTop - targetTop) > 1 ||
        excursionObservedTop === null || Math.abs(excursionObservedTop - after.top) <= 0.5) {
      return {ok: false, error: 'scroll_notify_return_not_confirmed', reentered,
        scroll_event_dispatched: false, before, after};
    }
    const event = new Event('scroll', {bubbles: false, cancelable: false, composed: false});
    if (event.isTrusted !== false) return {ok: false, error: 'scroll_notify_event_not_synthetic', scroll_event_dispatched: false};
    scroller.dispatchEvent(event);
    scrollEventDispatched = true;
    if (!stillOwned() || !hiddenUnfocused() || Math.abs(scroller.scrollTop - targetTop) > 1) {
      return {ok: false, error: 'scroll_notify_state_changed_after_dispatch', reentered,
        scroll_event_dispatched: true, before, after};
    }
  }
  // Keep this local to the serialized function, identical to inspectPage's
  // evidence rules: only visible, semantic UI state can assert loading.
  const loadingScope = scroller || nodes[0]?.closest('main') || document.body;
  const loaderText = /^(?:正在加载更早的消息|Loading (?:earlier|older) messages)(?:\s*(?:…|\.{1,3}))?$/i;
  const outsideReport = element => !element.closest('[data-content-search-turn-key]') &&
    !element.closest('[data-markdown-text-style]') && !element.closest('[data-message-author-role]');
  const isVisible = element => {
    if (!element.isConnected || typeof element.getBoundingClientRect !== 'function') return false;
    const rect = element.getBoundingClientRect();
    let left = Math.max(0, rect.left), top = Math.max(0, rect.top);
    let right = Math.min(window.innerWidth, rect.right), bottom = Math.min(window.innerHeight, rect.bottom);
    if (!(rect.width > 0 && rect.height > 0 && right > left && bottom > top)) return false;
    for (let node = element; node; node = node.parentElement) {
      const css = getComputedStyle(node);
      if (css.display === 'none' || css.visibility === 'hidden' || css.visibility === 'collapse' ||
          css.contentVisibility === 'hidden' || css.opacity === '0' || node.hidden ||
          node.getAttribute('aria-hidden') === 'true') return false;
      if (node !== element && /(auto|scroll|hidden|clip)/.test(`${css.overflowX} ${css.overflowY}`)) {
        const clip = node.getBoundingClientRect();
        left = Math.max(left, clip.left); top = Math.max(top, clip.top);
        right = Math.min(right, clip.right); bottom = Math.min(bottom, clip.bottom);
        if (!(right > left && bottom > top)) return false;
      }
    }
    return true;
  };
  const uiNodes = loadingScope ? [loadingScope, ...loadingScope.querySelectorAll('*')].filter(outsideReport) : [];
  const textOf = element => (element.textContent || element.getAttribute('aria-label') || '').trim();
  const loaders = uiNodes.filter(element => loaderText.test(textOf(element)) &&
    !Array.from(element.children).some(child => loaderText.test(textOf(child))));
  const loadingSignals = new Set();
  let visibleLoaders = 0;
  for (const loader of loaders) {
    if (!isVisible(loader)) continue;
    visibleLoaders += 1;
    for (let node = loader; node && node !== loadingScope?.parentElement; node = node.parentElement) {
      if (node.getAttribute('aria-busy') === 'true') loadingSignals.add('visible_history_aria_busy');
      if (['status', 'progressbar'].includes(node.getAttribute('role'))) loadingSignals.add('visible_history_status');
      if (['polite', 'assertive'].includes(node.getAttribute('aria-live'))) loadingSignals.add('visible_history_live_region');
    }
  }
  const visibleBusyRegions = uiNodes.filter(element => element.getAttribute('aria-busy') === 'true' &&
    (element === loadingScope || nodes.some(turn => element.contains(turn)) || loaders.some(loader => element.contains(loader))) &&
    isVisible(element)).length;
  if (visibleBusyRegions) loadingSignals.add('visible_conversation_aria_busy');
  const loading = loadingSignals.size > 0;
  const historyLoaderPresent = loaders.length > 0;
  const loadingEvidence = {history_text_nodes: loaders.length, visible_history_text_nodes: visibleLoaders,
    visible_busy_regions: visibleBusyRegions, signals: Array.from(loadingSignals)};
  return {ok: true, before, after, moved: before.top !== after.top,
    reverse, jump_to_top: jumpToTop, reentered, excursion_top: excursionTop, target_top: targetTop,
    excursion_observed_top: excursionObservedTop,
    scroll_event_dispatched: scrollEventDispatched,
    at_top: reverse ? after.top <= minimum + 3 : after.top <= 3,
    loading, history_loader_present: historyLoaderPresent, loading_evidence: loadingEvidence,
    // A stationary or exhausted DOM never becomes a successful checkpoint.
    atStartEvidence: {complete: false, reason: 'no_verified_conversation_start_marker'}};
}
