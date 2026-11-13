/* Diagnostic only; serialize this function into the collector-owned MAIN world.
 * It never clicks, scrolls, focuses, returns report prose, or decides coverage.
 */
export async function probeRendering(expected) {
  const title = document.title;
  const path = location.pathname;
  const hash = location.hash;
  const titleMatches = expected && [expected.title, `${expected.title} - ChatGPT`,
    `${expected.title} | ChatGPT`, `${expected.title} — ChatGPT`].includes(title);
  if (!expected || !/^[0-9a-f-]{36}$/i.test(expected.conversation_id || '') ||
      location.protocol !== 'https:' || location.hostname !== 'chatgpt.com' ||
      path !== `/c/${expected.conversation_id}` || !titleMatches ||
      !/^#personal-intel-probe=[A-Za-z0-9_-]{1,100}$/.test(hash)) {
    return {ok: false, error: 'unexpected_conversation_or_unowned_page', title, path};
  }
  if (typeof requestAnimationFrame !== 'function' || typeof cancelAnimationFrame !== 'function') {
    return {ok: false, error: 'animation_frame_api_missing'};
  }
  const visibilityAtStart = document.visibilityState;
  const started = performance.now();
  const frameResult = await new Promise(resolve => {
    let settled = false;
    let frameId;
    let timeoutId;
    const finish = fired => {
      if (settled) return;
      settled = true;
      // Cancel the outstanding frame on timeout; there is no persistent loop.
      if (frameId !== undefined) cancelAnimationFrame(frameId);
      if (timeoutId !== undefined) clearTimeout(timeoutId);
      resolve({raf_fired: fired, delay_ms: Math.round((performance.now() - started) * 10) / 10});
    };
    frameId = requestAnimationFrame(() => finish(true));
    timeoutId = setTimeout(() => finish(false), 1500);
  });
  if (location.protocol !== 'https:' || location.hostname !== 'chatgpt.com' ||
      location.pathname !== path || location.hash !== hash || document.title !== title) {
    return {ok: false, error: 'page_changed_during_render_probe'};
  }
  // Inspect only UI nodes outside turn/report containers. No text is returned.
  const loaderPattern = /^(?:正在加载更早的消息|Loading (?:earlier|older) messages)(?:\s*(?:…|\.{1,3}))?$/i;
  const outsideReport = element => !element.closest('[data-content-search-turn-key]') &&
    !element.closest('[data-markdown-text-style]') && !element.closest('[data-message-author-role]');
  const textOf = element => (element.textContent || element.getAttribute('aria-label') || '').trim();
  const nodes = document.body ? Array.from(document.body.querySelectorAll('*')).filter(outsideReport) : [];
  const loaders = nodes.filter(element => loaderPattern.test(textOf(element)) &&
    !Array.from(element.children).some(child => loaderPattern.test(textOf(child))));
  const clickableAncestors = new Set();
  for (const loader of loaders) {
    for (let node = loader; node && node !== document.body; node = node.parentElement) {
      if ((node.tagName.toLowerCase() === 'button' || node.getAttribute('role') === 'button') &&
          !node.disabled && node.getAttribute('disabled') === null && node.getAttribute('aria-disabled') !== 'true') {
        clickableAncestors.add(node);
        break;
      }
    }
  }
  return {ok: true, title, path, visibility: document.visibilityState,
    visibility_at_start: visibilityAtStart, ...frameResult, timeout_ms: 1500,
    viewport: {width: window.innerWidth, height: window.innerHeight,
      device_pixel_ratio: window.devicePixelRatio || null},
    history_loader_present: loaders.length > 0, history_loader_count: loaders.length,
    loader_has_clickable_button_ancestor: clickableAncestors.size > 0,
    clickable_loader_button_count: clickableAncestors.size,
    purpose: 'rendering_diagnostic_only_not_history_coverage'};
}
