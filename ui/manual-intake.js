/* The page only appends requests. The existing hidden checker owns intake. */
(() => {
  'use strict';
  const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
  const terminal = new Set(['complete', 'partial', 'failed']);
  const canonical = value => JSON.stringify(value, null, 2) + '\n';
  function create({projectId, getRoot, connect, onStatus = () => {}}) {
    let submitting = false, reading = false, disposed = false;
    let active = null;
    const emit = value => onStatus({busy: submitting || Boolean(active && !terminal.has(active.status)), ...value});
    async function read(folder, name) {
      let file;
      try { file = await (await folder.getFileHandle(name)).getFile(); }
      catch (error) { if (error.name === 'NotFoundError') return null; throw error; }
      if (file.size > 65536) throw new Error('收录状态文件过大，请让 Codex 检查。');
      const text = await file.text();
      return text.trim() ? JSON.parse(text) : null;
    }
    async function directory(create = false) {
      const root = getRoot();
      if (!root) return null;
      if (await root.queryPermission({mode: 'readwrite'}) !== 'granted') throw new Error('文件夹授权已失效，请重新连接资料文件夹。');
      const data = await root.getDirectoryHandle('data');
      const project = await read(await data.getDirectoryHandle('management'), 'project.json');
      if (!UUID.test(projectId || '') || project?.schema_version !== 1 || project.project_id !== projectId) {
        throw new Error('文件夹不属于当前情报库，请重新连接。');
      }
      try { return await (await root.getDirectoryHandle('.intake', {create})).getDirectoryHandle('manual', {create}); }
      catch (error) { if (!create && error.name === 'NotFoundError') return null; throw error; }
    }
    function validateRequest(value, name) {
      if (!value || value.schema_version !== 1 || value.project_id !== projectId || !UUID.test(value.id || '') ||
          name !== value.id + '.json' || value.action !== 'intake' || Object.keys(value).length !== 5 ||
          typeof value.requested_at !== 'string' || !/(?:Z|[+-]\d{2}:\d{2})$/.test(value.requested_at) ||
          !Number.isFinite(Date.parse(value.requested_at))) throw new Error('手动收录请求损坏，请让 Codex 检查。');
      return value;
    }
    async function scan(folder) {
      let requests, results;
      try { requests = await folder.getDirectoryHandle('requests'); }
      catch (error) { if (error.name === 'NotFoundError') return []; throw error; }
      try { results = await folder.getDirectoryHandle('results'); }
      catch (error) { if (error.name !== 'NotFoundError') throw error; }
      const entries = [];
      for await (const [name, handle] of requests.entries()) {
        if (!name.endsWith('.json')) continue;
        if (handle.kind !== 'file' || !UUID.test(name.slice(0, -5))) throw new Error('发现未知收录请求，请让 Codex 检查。');
        const result = results ? await read(results, name) : null;
        if (result && (result.schema_version !== 1 || result.project_id !== projectId || result.id !== name.slice(0, -5) ||
            !['waiting', 'running', ...terminal].includes(result.status) || typeof result.message !== 'string' ||
            !Number.isFinite(Date.parse(result.updated_at)))) throw new Error('收录结果身份或格式不一致，不能确认成功。');
        let request;
        try {
          request = await read(requests, name);
          if (!request && !result) continue; // An unclosed writable stream is not a request.
          validateRequest(request, name);
        } catch (error) {
          // The worker may have rejected malformed bytes. Its validated failed
          // receipt releases that request without rewriting the old evidence.
          if (result?.status !== 'failed') throw error;
          request = {id: result.id, requested_at: result.updated_at};
        }
        entries.push({...request, status: 'waiting', ...result});
        if (entries.length > 5000) throw new Error('收录请求过多，请让 Codex 检查。');
      }
      return entries.sort((a, b) => Date.parse(b.requested_at) - Date.parse(a.requested_at) || a.id.localeCompare(b.id));
    }
    function show(item) {
      active = item;
      if (!item) {
        emit({status: 'idle', message: '当前为仅手动收录。需要时点击按钮检查六类新报告；已收录的内容会跳过，失败后不会自动重试。'});
        return;
      }
      let message = item.message || '请求已保存，等待后台接手，通常不超过 2 分钟。请保持电脑开机。';
      if (item.status === 'waiting' && !item.retry_at && Date.now() - Date.parse(item.updated_at || item.requested_at) > 180000) {
        message = '请求仍在等待，尚未确认开始收录。请确认电脑已联网、Chrome 已打开且 ChatGPT 已登录；后台可能正在执行其他收录。不要重复提交。';
      }
      if (item.status === 'running' && Date.now() - Date.parse(item.updated_at) > 1800000) {
        message = '本轮运行时间较长，尚未收到最终结果。请保留请求并让 Codex 检查，避免重复启动。';
      }
      emit({status: item.status, message, summary: typeof item.summary === 'string' ? item.summary : '',
        updatedAt: item.updated_at || item.requested_at,
        canRefresh: item.status === 'complete' || item.status === 'partial'});
    }
    async function refresh() {
      if (submitting || reading || disposed) return;
      reading = true;
      try {
        const folder = await directory();
        if (!getRoot()) {
          emit({status: 'disconnected', message: '点击“手动收录”会优先使用已记住的“用户痛点”文件夹。只有首次使用或浏览器遗忘文件夹时才需要选择。'});
          return;
        }
        const items = folder ? await scan(folder) : [];
        show(items.find(item => !terminal.has(item.status)) || items[0] || null);
      } catch (error) { emit({status: 'error', message: '未能读取收录状态：' + error.message}); }
      finally { reading = false; }
    }
    async function submitLocked() {
      const folder = await directory(true);
      if (!folder) {
        emit({status: 'disconnected', message: '尚未连接资料文件夹，未提交收录。请连接后重试。'});
        return;
      }
      const prior = (await scan(folder)).find(item => !terminal.has(item.status));
      if (prior) { show(prior); return; }
      const request = {schema_version: 1, project_id: projectId, id: crypto.randomUUID(), action: 'intake', requested_at: new Date().toISOString()};
      const name = request.id + '.json', text = canonical(request);
      const requests = await folder.getDirectoryHandle('requests', {create: true});
      try { await requests.getFileHandle(name); throw new Error('请求编号已存在，未覆盖。'); }
      catch (error) { if (error.name !== 'NotFoundError') throw error; }
      const handle = await requests.getFileHandle(name, {create: true});
      let stream;
      try {
        stream = await handle.createWritable();
        await stream.write(text);
        await stream.close();
      } catch (error) {
        if (stream) try { await stream.abort(); } catch { /* Already closed. */ }
        if (await (await handle.getFile()).text() !== text) throw error;
      }
      if (await (await handle.getFile()).text() !== text) throw new Error('保存后回读不一致，尚未确认提交，请先检查状态。');
      show({...request, status: 'waiting'});
    }
    async function submit() {
      if (submitting) return;
      submitting = true;
      emit({status: 'authorizing', message: '正在连接默认资料文件夹；若出现权限提示，请点击“允许”。此时尚未提交收录。'});
      try {
        // Reuse/re-authorize the saved handle in this user-click chain.
        await connect();
        if (!getRoot()) {
          emit({status: 'disconnected', message: '尚未获得资料文件夹访问权限，没有提交收录。请再次点击“手动收录”并允许访问。'});
          return;
        }
        emit({status: 'submitting', message: '正在保存本次收录请求…'});
        if (globalThis.navigator?.locks) await navigator.locks.request('intel-manual-' + projectId, submitLocked);
        else await submitLocked();
      } catch (error) { emit({status: 'error', message: '尚未确认提交收录：' + error.message}); }
      finally { submitting = false; emit({busy: Boolean(active && !terminal.has(active.status))}); }
    }
    const timer = window.setInterval(() => { if (!document.hidden) void refresh(); }, 3000);
    const focus = () => { void refresh(); };
    window.addEventListener('focus', focus);
    return {submit, refresh, dispose() { disposed = true; window.clearInterval(timer); window.removeEventListener('focus', focus); }};
  }
  window.IntelManualIntake = {create};
})();
