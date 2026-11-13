(() => {
  'use strict';
  const byId = (id) => document.getElementById(id);
  const element = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  };
  let library;
  try {
    library = JSON.parse(byId('library-data').textContent);
    if (!Array.isArray(library.categories) || !Array.isArray(library.records) ||
        (library.weekly_reviews !== undefined && !Array.isArray(library.weekly_reviews))) throw new Error('资料格式不完整');
  } catch (error) {
    byId('collection-info').textContent = '本地资料未能读取';
    const message = element('div', 'fatal-error', '资料读取失败。请回到本项目的 Codex 聊天，说明此提示并重新构建首页；原始资料文件仍保留在项目中。');
    message.setAttribute('role', 'alert');
    byId('record-list').append(message);
    return;
  }

  const records = [...library.records].sort((a, b) => String(b.collected_at).localeCompare(String(a.collected_at)) || String(a.id).localeCompare(String(b.id)));
  const weeklyReviews = [...(library.weekly_reviews || [])].sort((a, b) => String(b.scheduled_for || b.collected_at).localeCompare(String(a.scheduled_for || a.collected_at)) || String(a.id).localeCompare(String(b.id)));
  const reviewIds = new Set(weeklyReviews.map((review) => review.id));
  const allItems = [...records, ...weeklyReviews].sort((a, b) => String(b.collected_at).localeCompare(String(a.collected_at)) || String(a.id).localeCompare(String(b.id)));
  const itemsById = new Map(allItems.map((item) => [item.id, item]));
  const categories = new Map(library.categories.map((item) => [item.id, item]));
  const state = { view: 'reports', category: '', query: '', recommendation: '', status: '' };
  const list = byId('record-list');
  const recordDialog = byId('record-dialog');
  const helpDialog = byId('help-dialog');
  let selectedRecord = null;
  let returnFocus = null;
  let returnRecordId = null;
  const detailHistory = [];
  let management = null;
  let operationPending = false;
  let managementStatus = {level: 'info', connected: false, busy: false,
    message: '当前显示上次生成的阅读快照。连接资料文件夹后，可保存归档和回收站操作。'};
  const textValue = (value) => Array.isArray(value) ? value.join('、') : (value || '未提供');
  const normalize = (value) => String(value || '').normalize('NFKC').toLocaleLowerCase('zh-CN');
  const isReview = (record) => reviewIds.has(record.id);
  const placement = (record) => management?.getState(record.id) || library.management?.states?.[record.id] || {state: 'active', restore_to: 'active'};
  const placementLabel = (record) => ({active: '主列表', archived: '已归档', trashed: '回收站'}[placement(record).state] || '主列表');
  const searchIndex = new Map(allItems.map((record) => [record.id, normalize([
    record.title, record.summary, record.audience, record.relevance, record.recommendation,
    record.recommendation_reason, record.next_action, record.status, record.source_task,
    record.collected_at, record.report_date, categories.get(record.category_id)?.label,
    ...(record.uncertainties || []), ...(record.tags || []),
    ...(record.sources || []).flatMap((source) => [source.label, source.url, source.kind]), record.original,
    record.scheduled_for, record.coverage_start, record.coverage_end, ...(record.unknowns || []),
    ...(record.candidate_record_ids || []).map((id) => itemsById.get(id)?.title || id)
  ].join(' '))]));

  function showDialog(dialog, trigger) {
    returnFocus = trigger || document.activeElement;
    returnRecordId = returnFocus?.closest('.record-card')?.dataset.recordId || null;
    dialog.showModal();
    dialog.scrollTop = 0;
  }

  document.querySelectorAll('.dialog').forEach((dialog) => {
    dialog.querySelector('.dialog-close').addEventListener('click', () => dialog.close());
    dialog.addEventListener('close', () => {
      if (returnFocus?.isConnected) returnFocus.focus();
      else {
        const card = Array.from(list.children).find((node) => node.dataset.recordId === returnRecordId);
        (card?.querySelector('.card-title-button') || nav.querySelector('[aria-pressed="true"]'))?.focus();
      }
    });
    dialog.addEventListener('click', (event) => {
      if (event.target !== dialog) return;
      const rect = dialog.getBoundingClientRect();
      if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close();
    });
  });

  function openHelp(event) {
    byId('copy-feedback').textContent = '';
    showDialog(helpDialog, event?.currentTarget);
  }
  byId('how-to-add').addEventListener('click', openHelp);
  byId('copy-prompt').addEventListener('click', async () => {
    const field = byId('intake-prompt');
    try {
      if (!navigator.clipboard?.writeText) throw new Error('当前浏览器不支持复制');
      await navigator.clipboard.writeText(field.value);
      byId('copy-feedback').textContent = '已复制，可粘贴到本项目聊天。';
    } catch (error) {
      field.focus();
      field.select();
      byId('copy-feedback').textContent = '已选中指令，请按 Ctrl+C 手动复制。';
    }
  });

  const nav = byId('category-nav');
  const navItems = [];
  function addCategoryButton(id, label, view = 'reports') {
    const button = element('button', 'nav-button');
    button.type = 'button';
    button.dataset.category = id;
    button.dataset.view = view;
    const count = element('span', 'nav-count', '0');
    button.append(element('span', '', label), count);
    button.addEventListener('click', () => {
      if (state.view !== view) {
        state.recommendation = '';
        state.status = '';
        byId('recommendation-filter').value = '';
        byId('status-filter').value = '';
      }
      state.view = view;
      state.category = id;
      render();
    });
    nav.append(button);
    navItems.push({id, view, button, count});
  }
  addCategoryButton('', '全部情报');
  library.categories.forEach((category) => addCategoryButton(category.id, category.label));
  const navDivider = element('div', 'nav-divider');
  navDivider.setAttribute('role', 'separator');
  nav.append(navDivider);
  addCategoryButton('', '每周复盘', 'weekly');
  addCategoryButton('', '已归档', 'archived');
  addCategoryButton('', '回收站', 'trashed');
  byId('brand-home').addEventListener('click', (event) => { event.preventDefault(); resetFilters(); });
  byId('collection-info').textContent = `${library.categories.length} 个来源栏目 · 总计 ${records.length} 份报告 · ${weeklyReviews.length} 份周复盘`;
  byId('updated-date').textContent = `资料更新于 ${library.updated_at || '未知日期'}`;
  const intakeStatus = byId('intake-status');
  if (intakeStatus && library.last_intake) {
    intakeStatus.hidden = false;
    const resultLabel = library.last_intake.status === 'complete' ? '已完成' : '未完成';
    intakeStatus.append(element('p', '', `上次已保存结果（${resultLabel}）：${library.last_intake.summary || '请查看收录记录'}`));
    const when = library.last_intake.finished_at;
    if (typeof when === 'string') intakeStatus.append(element('small', '', when.replace('T', ' ').replace(/(?:\.\d+)?\+08:00$/, '')));
    const failures = library.last_intake.failures || {};
    if (Object.keys(failures).length) {
      const details = element('details', '');
      details.append(element('summary', '', `${Object.keys(failures).length} 类尚未完成，查看原因`));
      for (const category of Object.keys(failures)) {
        const failure = failures[category];
        const knownReasons = {
          collector_interrupted: '读取窗口意外中断',
          collector_window_unavailable: '读取窗口已不可用',
          collector_window_user_interaction: '读取窗口状态发生变化，已停止读取',
          collector_creation_took_focus: '读取窗口意外切到前台，已停止读取',
          collector_url_changed: '读取页面发生变化，已停止读取',
          login_required: 'ChatGPT 需要重新登录',
          page_loading: 'ChatGPT 页面加载未完成',
          undated_report: '有报告未显示可核对的生成时间',
          history_loading_timeout_without_boundary: '历史报告页面加载超时',
          summary_failed: '报告整理失败',
          summary_batch_limit: '新报告较多，已达到本轮处理上限'
        };
        const reason = knownReasons[failure?.error] || knownReasons[failure?.reason] || '未能完成读取或入库';
        details.append(element('p', 'intake-warning', `${categories.get(category)?.label || category}：${reason}，已保留断点。`));
      }
      intakeStatus.append(details);
    }
    const notices = library.intake_notifications || [];
    if (notices.length) {
      const details = element('details', '');
      details.append(element('summary', '', `重复报告提醒（${notices.length} 条，已跳过）`));
      notices.forEach((notice) => {
        const record = records.find((item) => item.id === notice.existing_id);
        const row = element('p', '');
        if (record) row.append(detailButton(record, 'text-button', record.title));
        row.append(element('span', '', ` ${notice.message || '已跳过重复内容。'}`));
        details.append(row);
      });
      intakeStatus.append(details);
    }
  }

  function resetFilters() {
    state.view = 'reports';
    state.category = '';
    state.query = '';
    state.recommendation = '';
    state.status = '';
    byId('search-input').value = '';
    byId('recommendation-filter').value = '';
    byId('status-filter').value = '';
    render();
  }
  byId('clear-filters').addEventListener('click', clearCurrentFilters);
  byId('search-input').addEventListener('input', (event) => { state.query = event.target.value; render(); });
  byId('recommendation-filter').addEventListener('change', (event) => { state.recommendation = event.target.value; render(); });
  byId('status-filter').addEventListener('change', (event) => { state.status = event.target.value; render(); });

  function detailButton(record, className, label, linked = false) {
    const button = element('button', className, label);
    button.type = 'button';
    button.addEventListener('click', () => openRecord(record, button, linked));
    return button;
  }

  function managementActions(record) {
    const row = element('div', 'management-actions');
    const current = placement(record);
    const actions = current.state === 'trashed' ? [['restore', current.restore_to === 'archived' ? '恢复到已归档' : '恢复到主列表']] :
      [[current.state === 'archived' ? 'unarchive' : 'archive', current.state === 'archived' ? '移回主列表' : '归档'], ['trash', '移入回收站']];
    for (const [action, label] of actions) {
      const button = element('button', 'management-action', label);
      button.type = 'button';
      button.dataset.manageRecord = record.id;
      button.dataset.action = action;
      button.setAttribute('aria-label', `${label}：${record.title}`);
      button.disabled = operationPending || managementStatus.busy;
      button.addEventListener('click', () => changePlacement(record, action));
      row.append(button);
    }
    return row;
  }

  function updateManagementStatus(value) {
    managementStatus = {...managementStatus, ...value};
    for (const id of ['management-status', 'detail-management-status']) {
      const message = byId(id);
      message.textContent = managementStatus.message;
      message.dataset.level = managementStatus.level;
      message.setAttribute('role', managementStatus.level === 'error' ? 'alert' : 'status');
    }
    byId('management-panel').dataset.level = managementStatus.level;
    byId('management-panel').setAttribute('aria-busy', String(managementStatus.busy || operationPending));
    for (const id of ['connect-library', 'detail-connect-library']) {
      byId(id).disabled = managementStatus.busy || operationPending || !management;
      byId(id).textContent = managementStatus.connected ? '重新连接资料文件夹' : '连接资料文件夹';
    }
    byId('detail-connect-library').hidden = managementStatus.connected;
    byId('refresh-management').hidden = !managementStatus.connected;
    byId('refresh-management').disabled = managementStatus.busy || operationPending;
    document.querySelectorAll('[data-manage-record]').forEach((button) => {
      button.disabled = managementStatus.busy || operationPending || !management;
    });
  }

  async function runManagement(operation) {
    if (operationPending || managementStatus.busy) return;
    if (!management) {
      updateManagementStatus({level: 'error', message: '管理功能未能加载。请回到本项目的 Codex 聊天检查；现有资料仍可阅读。'});
      return;
    }
    operationPending = true;
    updateManagementStatus({});
    try { await operation(); }
    catch (error) {
      updateManagementStatus({level: 'error', busy: false,
        message: error?.message || '本次操作未能确认保存。请连接资料文件夹后刷新管理状态，再检查这份资料的位置。'});
    } finally {
      operationPending = false;
      updateManagementStatus({});
    }
  }

  function changePlacement(record, action) {
    return runManagement(() => management.change(record.id, action));
  }
  byId('connect-library').addEventListener('click', () => runManagement(() => management.connect()));
  byId('detail-connect-library').addEventListener('click', () => runManagement(() => management.connect()));
  byId('refresh-management').addEventListener('click', () => runManagement(() => management.refresh()));

  function updateDetailManagement() {
    if (!selectedRecord) return;
    const container = byId('detail-management-actions');
    const restoreActionFocus = container.contains(document.activeElement);
    const current = placement(selectedRecord);
    byId('detail-placement').textContent = current.state === 'trashed' ?
      '当前在回收站。资料可以恢复，完整原文仍保留。' : current.state === 'archived' ?
        '当前已归档，不在主列表显示；随时可以移回。' : '当前在主列表。归档可收起资料，移入回收站后仍可恢复。';
    container.replaceChildren(managementActions(selectedRecord));
    if (restoreActionFocus) {
      const target = container.querySelector('button');
      if (target && !target.disabled) target.focus();
      else byId(byId('original-tab').getAttribute('aria-selected') === 'true' ? 'original-tab' : 'summary-tab').focus();
    }
    updateManagementStatus({});
  }

  function makeCard(record) {
    const article = element('article', 'record-card');
    article.dataset.recordId = record.id;
    const content = element('div', 'card-content');
    const meta = element('div', 'card-meta');
    const status = element('span', 'status-pill', record.status);
    status.dataset.status = record.status;
    meta.append(element('span', 'category-label', categories.get(record.category_id)?.label || record.category_id), status, element('span', 'card-date', `${record.collected_at} 收录`));
    if (placement(record).state !== 'active') meta.append(element('span', 'placement-label', placementLabel(record)));
    const title = element('h2');
    title.append(detailButton(record, 'card-title-button', record.title));
    const relevance = element('p', 'card-relevance');
    relevance.append(element('strong', '', '与我有关'), document.createTextNode(record.relevance || '尚未判断'));
    const decision = element('div', 'card-decision');
    const recommendation = element('span', 'recommendation', record.recommendation);
    recommendation.dataset.recommendation = record.recommendation;
    decision.append(recommendation, element('span', 'decision-reason', record.recommendation_reason));
    content.append(meta, title, element('p', 'card-summary', record.summary), relevance, decision);
    const next = element('div', 'card-next');
    next.append(element('span', 'next-label', '下一步'), element('p', '', record.next_action));
    const footer = element('div', 'card-footer');
    footer.append(managementActions(record), detailButton(record, 'view-details', '查看详情与原文 →'));
    article.append(content, next, footer);
    return article;
  }

  function makeReviewCard(review) {
    const article = element('article', 'record-card review-card');
    article.dataset.recordId = review.id;
    article.dataset.kind = 'weekly-review';
    const content = element('div', 'card-content');
    const meta = element('div', 'card-meta');
    meta.append(element('span', 'category-label', '每周复盘'),
      element('span', 'card-date', `${String(review.scheduled_for || review.collected_at).slice(0, 10)} 本期`));
    if (placement(review).state !== 'active') meta.append(element('span', 'placement-label', placementLabel(review)));
    const title = element('h2');
    title.append(detailButton(review, 'card-title-button', review.title));
    content.append(meta, title, element('p', 'card-summary', review.summary));
    const next = element('div', 'card-next');
    next.append(element('span', 'next-label', '本周唯一行动'), element('p', '', review.next_action));
    const footer = element('div', 'card-footer');
    footer.append(managementActions(review), detailButton(review, 'view-details', '查看复盘与原文 →'));
    article.append(content, next, footer);
    return article;
  }

  function viewItems(view, category = '') {
    if (view === 'weekly') return weeklyReviews.filter((record) => placement(record).state === 'active');
    if (view === 'archived' || view === 'trashed') return allItems.filter((record) => placement(record).state === view);
    return records.filter((record) => placement(record).state === 'active' && (!category || record.category_id === category));
  }

  function render() {
    const category = categories.get(state.category);
    const titles = {weekly: '每周复盘', archived: '已归档', trashed: '回收站'};
    const descriptions = {
      weekly: '把已有发现变成一次小验证。每期查看唯一行动、真实反馈和待核实事项。',
      archived: '暂时从主列表收起的报告和周复盘。资料仍然保留，随时可以移回。',
      trashed: '移入回收站的报告和周复盘可以恢复，完整原文仍保留。这里不会永久删除资料。'
    };
    byId('page-title').textContent = titles[state.view] || (category ? category.label : '全部情报');
    byId('page-description').textContent = descriptions[state.view] || (category ? category.description : '先看简明结论，再决定下一步。这里显示主列表中的六类报告。');
    for (const item of navItems) {
      item.button.setAttribute('aria-pressed', String(item.view === state.view && item.id === state.category));
      item.count.textContent = viewItems(item.view, item.id).length;
    }
    byId('recommendation-field').hidden = state.view !== 'reports';
    byId('progress-field').hidden = state.view !== 'reports';
    const terms = normalize(state.query).trim().split(/\s+/u).filter(Boolean);
    const pool = viewItems(state.view, state.category);
    const filtered = pool.filter((record) => (
      (state.view !== 'reports' || !state.recommendation || record.recommendation === state.recommendation) &&
      (state.view !== 'reports' || !state.status || record.status === state.status) &&
      terms.every((term) => searchIndex.get(record.id).includes(term))
    ));
    const active = Boolean(terms.length || state.recommendation || state.status);
    byId('clear-filters').hidden = !active;
    const reportCount = filtered.filter((record) => !isReview(record)).length;
    const reviewCount = filtered.length - reportCount;
    byId('result-count').textContent = state.view === 'reports' ? `${active ? '找到' : '主列表'} ${reportCount} 份报告` :
      state.view === 'weekly' ? `${active ? '找到' : '已收录'} ${reviewCount} 份周复盘` : `${reportCount} 份报告 · ${reviewCount} 份周复盘`;
    const activeCard = document.activeElement?.closest('.record-card');
    const activeCardIndex = activeCard ? Array.from(list.children).indexOf(activeCard) : -1;
    const activeCardId = activeCard?.dataset.recordId;
    list.replaceChildren(...filtered.map((record) => isReview(record) ? makeReviewCard(record) : makeCard(record)));
    if (activeCardId && !recordDialog.open) {
      const nextCard = Array.from(list.children).find((node) => node.dataset.recordId === activeCardId) ||
        list.children[Math.min(activeCardIndex, list.children.length - 1)];
      (nextCard?.querySelector('.card-title-button') || nav.querySelector('[aria-pressed="true"]'))?.focus();
    }
    const empty = byId('empty-state');
    empty.hidden = filtered.length > 0;
    if (!filtered.length) {
      const action = byId('empty-action');
      action.hidden = false;
      if (active) {
        byId('empty-title').textContent = '当前栏目没有匹配的资料';
        byId('empty-description').textContent = '可以换个关键词，或清除筛选。搜索会同时查找标题、摘要、来源和完整原文。';
        action.textContent = '清除筛选';
        action.onclick = clearCurrentFilters;
      } else if (state.view === 'weekly') {
        byId('empty-title').textContent = '这里等待第一份周复盘';
        byId('empty-description').textContent = weeklyReviews.length ? '已有周复盘已被归档或移入回收站，可到对应栏目查看。' : '每周决策与验证复盘完成并入库后，会出现在这里。尚未生成的复盘不会提前显示。';
        action.hidden = true;
      } else if (state.view === 'archived' || state.view === 'trashed') {
        byId('empty-title').textContent = state.view === 'archived' ? '还没有归档资料' : '回收站是空的';
        byId('empty-description').textContent = state.view === 'archived' ? '在报告或周复盘上选择“归档”，即可先从主列表收起，之后随时移回。' : '移入回收站的资料会出现在这里，之后可以恢复，原文仍保留。';
        action.textContent = '回到主列表';
        action.onclick = resetFilters;
      } else {
        const existing = records.some((record) => !state.category || record.category_id === state.category);
        byId('empty-title').textContent = existing ? '这个栏目暂时没有在主列表中的报告' : '这个栏目，等你的第一份报告';
        byId('empty-description').textContent = existing ? '已有报告已被归档或移入回收站，可到对应栏目查看和恢复。' : '新报告完成收录后会出现在这里，完整原文也会留下。';
        action.textContent = '如何收录';
        action.onclick = openHelp;
      }
    }
    updateManagementStatus({});
  }

  function clearCurrentFilters() {
    state.query = '';
    state.recommendation = '';
    state.status = '';
    byId('search-input').value = '';
    byId('recommendation-filter').value = '';
    byId('status-filter').value = '';
    render();
  }

  function detailBlock(label, value, full = false) {
    const block = element('section', `detail-block${full ? ' full-width' : ''}`);
    block.append(element('h3', '', label), element('p', '', textValue(value)));
    return block;
  }

  function setTab(name, focus = false) {
    ['summary', 'original'].forEach((tabName) => {
      const tab = byId(`${tabName}-tab`);
      const selected = tabName === name;
      tab.setAttribute('aria-selected', String(selected));
      tab.tabIndex = selected ? 0 : -1;
      byId(`${tabName}-panel`).hidden = !selected;
      if (selected && focus) tab.focus();
    });
  }
  ['summary', 'original'].forEach((name) => {
    const tab = byId(`${name}-tab`);
    tab.addEventListener('click', () => setTab(name));
    tab.addEventListener('keydown', (event) => {
      if (['ArrowRight', 'ArrowLeft', 'Home', 'End'].includes(event.key)) {
        event.preventDefault();
        const next = event.key === 'Home' ? 'summary' : event.key === 'End' ? 'original' : name === 'summary' ? 'original' : 'summary';
        setTab(next, true);
      }
    });
  });

  function safeSourceUrl(value) {
    try {
      const url = new URL(value);
      return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password ? url.href : null;
    } catch (error) { return null; }
  }

  function fillReviewDetails(content, review) {
    const grid = element('div', 'detail-grid');
    const next = detailBlock('本周唯一行动', review.next_action, true);
    next.classList.add('detail-next');
    grid.append(next);
    const uncertainty = element('section', 'detail-block full-width');
    uncertainty.append(element('h3', '', '未知反馈与待核实事项'));
    const unknowns = element('ul');
    (review.unknowns?.length ? review.unknowns : ['未提供反馈记录，执行结果未知。']).forEach((item) => unknowns.append(element('li', '', item)));
    uncertainty.append(unknowns);
    grid.append(uncertainty, detailBlock('本期覆盖范围', `${review.coverage_start || '未知'} 至 ${review.coverage_end || '未知'}`),
      detailBlock('实际生成时间', review.generated_at || '未知'));
    content.append(grid);
    const sources = element('section', 'detail-section');
    sources.append(element('h3', '', '这次复盘依据哪些报告'),
      element('p', '', '可打开来源报告查看原文与核验状态。复盘建议不等于需求或收入已经验证。'));
    const ids = [...new Set([review.selected_record_id, ...(review.candidate_record_ids || [])].filter(Boolean))];
    const sourceList = element('ul', 'review-source-list');
    for (const id of ids) {
      const item = itemsById.get(id);
      const row = element('li', '');
      if (id === review.selected_record_id) row.append(element('span', 'review-source-choice', '本周选择'));
      else row.append(element('span', 'review-source-choice', '候选报告'));
      if (item && !isReview(item)) {
        row.append(detailButton(item, 'text-button', item.title, true));
        if (placement(item).state !== 'active') row.append(element('span', 'source-kind', placementLabel(item)));
      } else row.append(element('span', '', `来源报告暂不可用（${id}）`));
      sourceList.append(row);
    }
    if (ids.length) sources.append(sourceList);
    else sources.append(element('p', '', '本期未关联具体报告，请查看完整原文中的行动依据。'));
    content.append(sources);
  }

  function openRecord(record, trigger, linked = false) {
    const alreadyOpen = recordDialog.open;
    if (linked && alreadyOpen && selectedRecord?.id !== record.id) detailHistory.push(selectedRecord);
    else if (!alreadyOpen) detailHistory.length = 0;
    selectedRecord = record;
    byId('detail-context').textContent = isReview(record) ? '每周决策与验证复盘' : categories.get(record.category_id)?.label || '情报详情';
    byId('detail-title').textContent = record.title;
    byId('detail-meta').textContent = isReview(record) ?
      `本期计划时间：${record.scheduled_for || '未知'} · 收录于 ${record.collected_at || '未知'}` :
      `来自 ${record.source_task || '未知任务'} · 收录于 ${record.collected_at} · 报告日期：${record.report_date || '未知'} · 进度：${record.status}`;
    const content = byId('detail-content');
    content.replaceChildren(element('p', 'detail-summary', record.summary));
    if (isReview(record)) {
      fillReviewDetails(content, record);
    } else {
    const grid = element('div', 'detail-grid');
    grid.append(detailBlock('谁会遇到', record.audience), detailBlock('与我有什么关系', record.relevance));
    grid.append(detailBlock(`整理建议：${record.recommendation}（主观判断）`, record.recommendation_reason, true));
    const next = detailBlock('下一步做什么', record.next_action, true);
    next.classList.add('detail-next');
    grid.append(next);
    const uncertainty = element('section', 'detail-block full-width');
    uncertainty.append(element('h3', '', '还有什么待验证'));
    const uncertainties = element('ul');
    (record.uncertainties?.length ? record.uncertainties : ['尚未记录，请以原报告为准。']).forEach((item) => uncertainties.append(element('li', '', item)));
    uncertainty.append(uncertainties);
    grid.append(uncertainty);
    content.append(grid);
    const sources = element('section', 'detail-section');
    sources.append(element('h3', '', '原报告引用的来源'), element('p', '', '链接保留自原报告，核验状态逐条标注。引用数量不代表独立需求证据数量。'));
    const sourceList = element('ul', 'source-list');
    (record.sources || []).forEach((source) => {
      const row = element('li');
      const main = element('div', 'source-main');
      const url = safeSourceUrl(source.url);
      if (url) {
        const link = element('a', '', source.label || url);
        link.href = url;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        link.title = '在新标签页查看外部来源';
        main.append(link);
      } else {
        main.append(element('span', '', `${source.label || '来源'}（链接不可用）`));
      }
      main.append(element('span', 'source-kind', source.kind || '来源资料'));
      if (source.verification === '已核验') {
        main.append(element('span', 'source-kind', `核验日期：${source.checked_at || '未记录'}`));
        if (source.verification_note) main.append(element('span', 'source-kind', `核验说明：${source.verification_note}`));
      }
      const verification = element('span', 'source-verification', source.verification || '未核验');
      verification.dataset.verification = source.verification || '未核验';
      row.append(main, verification);
      sourceList.append(row);
    });
    if (!sourceList.children.length) sources.append(element('p', '', '此报告尚未提供来源链接。'));
    sources.append(sourceList);
    content.append(sources);
    const related = element('section', 'detail-section');
    related.append(element('h3', '', '相关情报'));
    const relatedRecords = (record.related_ids || []).map((id) => records.find((item) => item.id === id)).filter(Boolean);
    if (!relatedRecords.length) related.append(element('p', '', '暂未关联其他记录。'));
    relatedRecords.forEach((item) => {
      related.append(detailButton(item, 'text-button', item.title, true));
    });
    content.append(related);
    const tags = element('div', 'tag-list');
    (record.tags || []).forEach((tag) => tags.append(element('span', 'tag', tag)));
    content.append(tags);
    }
    const archive = element('details', 'archive-info');
    archive.append(element('summary', '', '查看归档信息'));
    const info = element('dl');
    info.append(element('dt', '', '记录编号'), element('dd', '', record.id), element('dt', '', '原文文件'), element('dd', '', record.original_file), element('dt', '', '原文校验指纹（用于检查文件是否意外改动）'), element('dd', 'hash', record.original_sha256));
    archive.append(info);
    content.append(archive);
    byId('original-text').textContent = record.original || '';
    byId('detail-back').hidden = detailHistory.length === 0;
    setTab('summary');
    updateDetailManagement();
    if (alreadyOpen) {
      recordDialog.scrollTop = 0;
      byId('detail-title').tabIndex = -1;
      byId('detail-title').focus();
    } else showDialog(recordDialog, trigger);
  }

  byId('detail-back').addEventListener('click', () => {
    const previous = detailHistory.pop();
    if (previous) openRecord(previous, null);
  });

  byId('download-original').addEventListener('click', () => {
    if (!selectedRecord) return;
    const blob = new Blob([selectedRecord.original || ''], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const link = element('a');
    link.href = url;
    link.download = `${selectedRecord.title.replace(/[<>:"/\\|?*\x00-\x1F]/g, '_')}.md`;
    document.body.append(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  });

  render();
  try {
    if (!window.IntelManagement?.create) throw new Error('管理功能未能加载。请回到本项目的 Codex 聊天检查；现有资料仍可阅读。');
    management = window.IntelManagement.create({library,
      onChange: () => { render(); if (recordDialog.open) updateDetailManagement(); },
      onStatus: updateManagementStatus});
    updateManagementStatus({});
    Promise.resolve(management.ready).catch((error) => updateManagementStatus({level: 'error', busy: false,
      message: error?.message || '暂时无法读取最新管理状态。请连接资料文件夹后重试。'}));
  } catch (error) {
    updateManagementStatus({level: 'error', message: error.message});
  }
  let manual = null;
  let manualStatus = {busy: false, status: 'idle'};
  function updateManualStatus(value) {
    manualStatus = {...manualStatus, ...value};
    const panel = byId('manual-intake-panel');
    panel.dataset.status = manualStatus.status;
    panel.setAttribute('aria-busy', String(manualStatus.busy));
    byId('manual-intake').disabled = manualStatus.busy;
    const buttonLabels = {restoring: '恢复连接…', authorizing: '等待授权…', submitting: '提交中…',
      waiting: '等待收录…', running: '正在收录…'};
    byId('manual-intake-label').textContent = manualStatus.busy ?
      (buttonLabels[manualStatus.status] || '处理中…') : '手动收录';
    if (value.message !== undefined) byId('manual-intake-message').textContent = value.message;
    if (value.status !== undefined) {
      byId('manual-intake-summary').textContent = value.summary || '';
      byId('manual-intake-summary').hidden = !value.summary;
      byId('reload-library').hidden = !value.canRefresh;
      const time = byId('manual-intake-time');
      time.hidden = !value.updatedAt;
      if (value.updatedAt) time.textContent = '状态时间：' + new Date(value.updatedAt).toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai', hour12: false});
    }
  }
  byId('manual-intake').addEventListener('click', () => {
    if (manual) void manual.submit();
    else updateManualStatus({status: 'error', message: '手动收录未能加载，请让 Codex 检查。现有资料仍可阅读。'});
  });
  byId('refresh-intake').addEventListener('click', () => { if (manual) void manual.refresh(); });
  byId('reload-library').addEventListener('click', () => window.location.reload());
  if (management && window.IntelManualIntake?.create) {
    updateManualStatus({busy: true, status: 'restoring', message: '正在恢复已记住的资料文件夹…'});
    manual = window.IntelManualIntake.create({projectId: library.management?.project_id,
      getRoot: () => management.getRoot(), connect: () => management.useDefault(), onStatus: updateManualStatus});
    Promise.resolve(management.ready).then(() => manual.refresh()).catch(() =>
      updateManualStatus({busy: false, status: 'error', message: '未能恢复文件夹连接，请连接资料文件夹后重试。'}));
  }
})();
