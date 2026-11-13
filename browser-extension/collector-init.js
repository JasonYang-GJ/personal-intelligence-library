/* One fixed maintenance stage only. No browser navigation, activation, input,
 * clipboard access, remote debugging, or repeat reload is performed here.
 */
(async () => {
  'use strict';
  const EXTENSION_ID = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';
  const RUN_ID = 'extension-probe-000000000000000000000001';
  const SHELL_URL = `chrome-extension://${EXTENSION_ID}/collector.html`;
  const MARKER = 'collectorReloadConsumed';
  const validLocation = () => typeof chrome !== 'undefined' && chrome.runtime?.id === EXTENSION_ID &&
    location.href === SHELL_URL && location.pathname === '/collector.html' &&
    location.hash === '' && location.search === '';
  if (!validLocation()) return {status: 'ignored'};
  const deadline = performance.now() + 1500;
  const inTime = () => performance.now() <= deadline;
  let stageConfirmed = false;
  const observeReason = reason => {
    if (!stageConfirmed || !validLocation()) return;
    try {
      // Best effort only: never await a diagnostic before reload or extend the
      // action deadline. Context destruction may discard the acknowledgement.
      const pending = chrome.runtime.sendNativeMessage('com.personal_intel.collector', {
        op: 'observe', run_id: RUN_ID, event: 'phase',
        meta: {phase: 'reload_bootstrap', detail: reason},
      });
      pending?.catch?.(() => {});
    } catch { /* Logging does not authorize or delay a reload. */ }
  };
  const finish = reason => { observeReason(reason); return {status: reason}; };
  const exactTab = tab => tab && Number.isInteger(tab.id) && Number.isInteger(tab.windowId) &&
    tab.url === SHELL_URL && (!tab.pendingUrl || tab.pendingUrl === SHELL_URL);

  async function eligible(consumed, identity = null) {
    if (!inTime()) return null;
    if (!validLocation()) return {stop: true, status: 'identity_mismatch'};
    const [saved, current] = await Promise.all([
      chrome.storage.local.get(['activeRun', 'owned', MARKER]),
      chrome.tabs.getCurrent(),
    ]);
    if (saved.activeRun === RUN_ID) stageConfirmed = true;
    if (!inTime()) return null;
    if (!validLocation()) return {stop: true, status: 'identity_mismatch'};
    if (!consumed && saved[MARKER] === RUN_ID) return {stop: true, status: 'already_consumed'};
    if (consumed && saved[MARKER] !== RUN_ID) return {stop: true, status: 'identity_mismatch'};
    if (saved.activeRun !== RUN_ID || !exactTab(current)) return {stop: true, status: 'identity_mismatch'};
    if (saved.owned == null) return null;
    if (saved.owned.runId !== RUN_ID ||
        saved.owned.url !== SHELL_URL || !exactTab(current) ||
        saved.owned.tabId !== current.id || saved.owned.windowId !== current.windowId ||
        (identity && (identity.tabId !== current.id || identity.windowId !== current.windowId))) return {stop: true, status: 'identity_mismatch'};
    const win = await chrome.windows.get(current.windowId, {populate: true});
    if (!inTime()) return null;
    if (!validLocation()) return {stop: true, status: 'identity_mismatch'};
    if (win.id !== current.windowId || win.focused !== false ||
        win.tabs?.length !== 1 || !exactTab(win.tabs[0]) ||
        win.tabs[0].id !== current.id || win.tabs[0].windowId !== current.windowId) return {stop: true, status: 'identity_mismatch'};
    if (win.state !== 'minimized') return null;
    return {tabId: current.id, windowId: current.windowId};
  }

  try {
    // The old worker writes owned immediately after creating this local shell.
    // Wait briefly for that write; missing/late state never authorizes reload.
    for (let attempt = 0; attempt < 75 && inTime(); attempt++) {
      const identity = await eligible(false);
      if (identity?.stop) return finish(identity.status);
      if (identity) {
        if (!inTime() || !validLocation()) return finish('expired');
        await chrome.storage.local.set({[MARKER]: RUN_ID});
        // A lost race, user focus/navigation, or late response consumes this
        // one-shot grant without acting; it must never retry a reload later.
        const rechecked = await eligible(true, identity);
        if (!rechecked || rechecked.stop || !inTime() || !validLocation()) return finish('changed_after_consume');
        observeReason('reload_requested');
        if (!inTime() || !validLocation()) return finish('expired');
        chrome.runtime.reload();
        return {status: 'reloaded'};
      }
      const remaining = deadline - performance.now();
      if (remaining <= 0) break;
      await new Promise(resolve => setTimeout(resolve, Math.min(20, remaining)));
    }
    return finish('no_matching_owned_shell');
  } catch {
    // A destroyed extension context, missing API, or failed storage write is
    // not a reason to touch the user's tabs or attempt another reload.
    return finish('unavailable');
  }
})();
