/* MAIN world, document_start. Only the collector's marked conversation pages.
 * No clipboard contents are read, written, or restored by this script.
 */
(() => {
  'use strict';
  const conversationIds = new Set([
    '00000000-0000-4000-8000-000000000010',
    '00000000-0000-4000-8000-000000000008',
    '00000000-0000-4000-8000-000000000009',
    '00000000-0000-4000-8000-000000000007',
    '00000000-0000-4000-8000-000000000011',
    '00000000-0000-4000-8000-000000000012',
  ]);
  if (location.protocol !== 'https:' || location.hostname !== 'chatgpt.com' ||
      !/^#personal-intel-probe=[A-Za-z0-9_-]{1,100}$/.test(location.hash) ||
      !conversationIds.has(location.pathname.slice(3)) ||
      !location.pathname.startsWith('/c/')) return;
  const key = '__personalIntelClipboardGuardV1';
  // A document_start guard must run before the site can cache clipboard APIs.
  // A late or repeated injection never upgrades itself into early protection.
  if (Object.prototype.hasOwnProperty.call(window, key)) return;
  const installedEarly = document.readyState === 'loading';
  const state = {active: null, writes: [], pending: 0, legacy: 0, reads: 0,
    intercepted: 0, ignored: 0, errors: [], installed: false};
  const identities = [];
  const recordError = error => state.errors.push(String(error?.message || error).slice(0, 120));
  const writeText = async value => {
    state.intercepted += 1;
    if (!state.active) { state.ignored += 1; return; }
    state.writes.push(String(value));
  };
  const write = async items => {
    state.intercepted += 1;
    if (!state.active) { state.ignored += 1; return; }
    const capture = state.active;
    state.pending += 1;
    try {
      for (const item of items) {
        if (!Array.from(item.types || []).includes('text/plain')) throw new Error('plain_text_missing');
        const blob = await item.getType('text/plain');
        const value = await blob.text();
        if (state.active !== capture) throw new Error('capture_changed_during_write');
        state.writes.push(value);
      }
    } catch (error) { recordError(error); throw error; }
    finally { state.pending -= 1; }
  };
  const read = async () => {
    state.reads += 1;
    throw new Error('personal_intel_clipboard_read_blocked');
  };
  const define = (target, name, value) => {
    Object.defineProperty(target, name, {value, configurable: false, writable: false});
    identities.push([target, name, value]);
    if (target[name] !== value) throw new Error('clipboard_guard_install_failed');
  };
  let installError = null;
  try {
    if (!globalThis.Clipboard?.prototype || !navigator.clipboard) throw new Error('clipboard_api_missing');
    for (const target of [Clipboard.prototype, navigator.clipboard]) {
      for (const [name, value] of Object.entries({writeText, write, read, readText: read})) define(target, name, value);
    }
    const nativeExec = Document.prototype.execCommand;
    if (typeof nativeExec !== 'function') throw new Error('exec_command_api_missing');
    const guardedExec = function (command, ...args) {
      if (/^(copy|cut)$/i.test(String(command))) { state.legacy += 1; return false; }
      return nativeExec.call(this, command, ...args);
    };
    define(Document.prototype, 'execCommand', guardedExec);
    define(document, 'execCommand', guardedExec);
    const blockEvent = event => {
      event.preventDefault();
      event.stopImmediatePropagation();
      state.legacy += 1;
    };
    // Window capture runs before document/target listeners registered by the site.
    window.addEventListener('copy', blockEvent, true);
    window.addEventListener('cut', blockEvent, true);
    document.addEventListener('copy', blockEvent, true);
    document.addEventListener('cut', blockEvent, true);
    state.installed = true;
  } catch (error) { installError = String(error?.message || error); }
  const check = () => ({
    ok: installedEarly && state.installed && identities.every(([target, name, fn]) => target[name] === fn),
    installed_early: installedEarly,
    install_error: installError,
    version: 1,
  });
  const api = Object.freeze({
    check,
    begin(turnKey) {
      if (!check().ok) throw new Error('early_clipboard_guard_unavailable');
      if (state.active || state.pending) throw new Error('clipboard_capture_busy');
      state.active = String(turnKey);
      state.writes = [];
      state.legacy = state.reads = state.intercepted = 0;
      state.errors = [];
    },
    snapshot() {
      return {active: state.active, writes: state.writes.slice(), pending: state.pending,
        legacy_copy_calls: state.legacy, clipboard_read_calls: state.reads,
        copy_intercepted: state.intercepted, errors: state.errors.slice(),
        // This counts calls made by this guard, not an OS-wide measurement.
        real_clipboard_calls: 0};
    },
    end() { state.active = null; },
  });
  Object.defineProperty(window, key, {value: api, configurable: false, writable: false});
})();
