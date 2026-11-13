/* Disk-backed, append-only archive/trash actions. IndexedDB stores only a folder handle. */
(() => {
  'use strict';
  const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
  const RECORD = /^(?:intel-\d{8}-\d{3,}|review-\d{8})$/;
  const keys = ['schema_version', 'project_id', 'id', 'record_id', 'action', 'state', 'restore_to', 'order', 'created_at'];
  const targets = {archive: ['archived'], unarchive: ['active'], trash: ['trashed'], restore: ['active', 'archived']};
  const encoder = new TextEncoder();
  const defaultState = () => ({state: 'active', restore_to: 'active'});
  const canonical = event => JSON.stringify(event, null, 2) + '\n';
  async function digest(bytes) {
    return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)))
      .map(value => value.toString(16).padStart(2, '0')).join('');
  }
  function validateEvent(event, name, projectId) {
    if (!event || typeof event !== 'object' || Array.isArray(event) ||
        Object.keys(event).length !== keys.length || !keys.every(key => Object.hasOwn(event, key)) ||
        event.schema_version !== 1 || event.project_id !== projectId ||
        typeof event.id !== 'string' || !UUID.test(event.id) || name !== event.id + '.json' ||
        typeof event.record_id !== 'string' || !RECORD.test(event.record_id) ||
        !Object.hasOwn(targets, event.action) || !targets[event.action].includes(event.state) ||
        !['active', 'archived'].includes(event.restore_to) ||
        (event.state !== 'trashed' && event.restore_to !== event.state) ||
        !Number.isSafeInteger(event.order) || event.order < 0 ||
        typeof event.created_at !== 'string' || !/(?:Z|[+-]\d{2}:\d{2})$/.test(event.created_at) ||
        !Number.isFinite(Date.parse(event.created_at))) {
      throw new Error('整理记录损坏或不属于此项目：' + name);
    }
    return event;
  }
  function foldEvents(events) {
    const states = {};
    for (const event of [...events].sort((a, b) => a.order - b.order || a.id.localeCompare(b.id, 'en'))) {
      states[event.record_id] = {state: event.state, restore_to: event.restore_to, event_id: event.id, order: event.order};
    }
    return states;
  }
  function create({library, onChange = () => {}, onStatus = () => {}}) {
    const projectId = library.management?.project_id;
    const ids = new Set([...library.records, ...(library.weekly_reviews || [])].map(record => record.id));
    let snapshot = library.management || {states: {}, event_count: 0, empty_files: [], fingerprint: '', last_event_at: null};
    let root = null, busy = false, connected = false;
    const cacheKey = String(projectId) + ':' + new URL('.', location.href).href;
    const notify = (level, message) => onStatus({level, message, connected, busy});
    const getState = id => ({...(snapshot.states?.[id] || defaultState())});
    const readJson = async handle => JSON.parse(await (await handle.getFile()).text());
    async function remembered(value) {
      if (!globalThis.indexedDB) return null;
      const db = await new Promise((resolve, reject) => {
        const request = indexedDB.open('personal-intel-folder-handles-v1', 1);
        request.onupgradeneeded = () => request.result.createObjectStore('handles');
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error);
      });
      try {
        return await new Promise((resolve, reject) => {
          const tx = db.transaction('handles', value ? 'readwrite' : 'readonly');
          const request = value ? tx.objectStore('handles').put(value, cacheKey) : tx.objectStore('handles').get(cacheKey);
          let result;
          request.onsuccess = () => { result = request.result; };
          tx.oncomplete = () => resolve(result || null);
          tx.onerror = () => reject(tx.error);
          tx.onabort = () => reject(tx.error || new Error('文件夹连接没有保存'));
        });
      } finally { db.close(); }
    }
    async function directory() {
      let management, project;
      try {
        const data = await root.getDirectoryHandle('data');
        management = await data.getDirectoryHandle('management');
        project = await readJson(await management.getFileHandle('project.json'));
      } catch (error) {
        if (error.name === 'NotFoundError') throw new Error('此文件夹没有情报库资料，请选择此 HTML 所在的项目文件夹。');
        throw error;
      }
      if (!UUID.test(projectId || '') || project.schema_version !== 1 || project.project_id !== projectId) {
        throw new Error('选中的不是当前情报库文件夹，请选择此 HTML 所在的项目文件夹。');
      }
      return management;
    }
    async function readEvents() {
      const management = await directory();
      let folder;
      try { folder = await management.getDirectoryHandle('events'); }
      catch (error) { if (error.name !== 'NotFoundError') throw error; }
      const entries = [];
      if (folder) for await (const [name, handle] of folder.entries()) {
        if (!name.endsWith('.json')) continue; // Browser temporary writable streams are not committed events.
        if (handle.kind !== 'file' || !UUID.test(name.slice(0, -5))) throw new Error('整理目录包含未知记录：' + name);
        entries.push([name, handle]);
      }
      entries.sort((a, b) => a[0].localeCompare(b[0], 'en'));
      const events = [], fingerprints = [], emptyFiles = [];
      for (const [name, handle] of entries) {
        const file = await handle.getFile();
        if (file.size > 16384) throw new Error('整理记录过大：' + name);
        const bytes = await file.arrayBuffer();
        fingerprints.push(name + ':' + await digest(bytes));
        if (!bytes.byteLength) { emptyFiles.push(name); continue; }
        const text = new TextDecoder('utf-8', {fatal: true}).decode(bytes);
        const event = validateEvent(JSON.parse(text), name, projectId);
        if (canonical(event) !== text) throw new Error('整理记录格式发生变化，请核对：' + name);
        events.push(event);
      }
      events.sort((a, b) => a.order - b.order || a.id.localeCompare(b.id, 'en'));
      return {schema_version: 1, project_id: projectId, states: foldEvents(events), event_count: events.length,
        empty_files: emptyFiles, fingerprint: await digest(encoder.encode(fingerprints.join('\n'))),
        last_event_at: events.at(-1)?.created_at || null, max_order: events.at(-1)?.order || 0};
    }
    async function load() {
      snapshot = await readEvents();
      onChange(snapshot);
      return snapshot;
    }
    async function refresh() {
      if (!root || busy) return;
      busy = true;
      onStatus({busy, connected});
      try {
        connected = (await root.queryPermission({mode: 'readwrite'})) === 'granted';
        if (!connected) {
          notify('warning', '已记住资料文件夹，但需要重新允许访问。点击“手动收录”即可授权，无需重新选择文件夹。');
          return;
        }
        await load();
        notify(snapshot.empty_files.length ? 'warning' : 'info', snapshot.empty_files.length ?
          '已读取磁盘状态；有未完成的整理操作未生效，请重新操作需要整理的条目。' : '已连接项目文件夹，整理操作会直接保存到本机。');
      } catch (error) { connected = false; notify('error', '未能读取最新整理状态：' + error.message); }
      finally { busy = false; onStatus({busy, connected}); }
    }
    async function connect() {
      if (busy) return;
      busy = true;
      notify('info', '请选择当前 HTML 所在的项目文件夹，并允许保存整理操作。');
      try {
        // Invoke the picker while the click still has user activation.
        if (typeof showDirectoryPicker !== 'function') throw new Error('请用新版 Chrome 或 Edge 打开本页后连接文件夹。');
        const chosen = await showDirectoryPicker({id: 'personal-intel-library', mode: 'readwrite', ...(root ? {startIn: root} : {})});
        const previous = root;
        root = chosen;
        try { await directory(); } catch (error) { root = previous; throw error; }
        if (await root.queryPermission({mode: 'readwrite'}) !== 'granted') throw new Error('没有获得写入权限，尚未修改资料。');
        connected = true;
        await load();
        let saved = true;
        try { await remembered(root); } catch { saved = false; }
        notify('success', saved ? '项目文件夹已连接，已载入最新归档和回收站状态。' :
          '已连接并载入磁盘状态；浏览器不能记住授权，下次打开需重新连接。');
      } catch (error) {
        if (error.name !== 'AbortError') connected = false;
        notify(error.name === 'AbortError' ? 'info' : 'error', error.name === 'AbortError' ?
          '已取消连接，没有修改资料。' : '连接未完成：' + error.message);
      } finally { busy = false; onStatus({busy, connected}); }
    }
    async function useDefault() {
      if (!root) return connect();
      if (busy) throw new Error('文件夹状态正在读取，请稍后再点击。');
      busy = true;
      notify('info', '正在使用已记住的资料文件夹；若浏览器询问访问权限，点击“允许”即可。');
      try {
        // Start directly in the click handler: the saved handle eliminates the
        // picker, but a browser permission prompt still needs user activation.
        const permission = await root.requestPermission({mode: 'readwrite'});
        if (permission !== 'granted') {
          connected = false;
          notify('warning', '尚未允许访问资料文件夹，没有提交收录。再次点击“手动收录”可重新授权。');
          return;
        }
        await directory();
        await load();
        connected = true;
        notify('success', '已使用记住的资料文件夹，无需重新选择。');
      } catch (error) {
        connected = false;
        notify('error', '未能使用已记住的资料文件夹：' + error.message);
      } finally { busy = false; onStatus({busy, connected}); }
    }
    async function change(recordId, action) {
      if (busy) throw new Error('请等当前整理操作保存完成。');
      if (!ids.has(recordId) || !Object.hasOwn(targets, action)) throw new Error('未知条目或操作。');
      if (!root || !connected) throw new Error('请先点击“连接资料文件夹”，将整理结果保存到本机。');
      busy = true;
      notify('info', '正在保存整理操作…');
      let saved = false;
      try {
        if (await root.queryPermission({mode: 'readwrite'}) !== 'granted') {
          connected = false;
          throw new Error('文件夹授权已失效，请重新连接。');
        }
        const before = await load(), current = getState(recordId);
        let state, restoreTo;
        if (action === 'archive' && current.state === 'active') state = restoreTo = 'archived';
        else if (action === 'unarchive' && current.state === 'archived') state = restoreTo = 'active';
        else if (action === 'trash' && current.state !== 'trashed') { state = 'trashed'; restoreTo = current.state; }
        else if (action === 'restore' && current.state === 'trashed') state = restoreTo = current.restore_to;
        else throw new Error('条目状态已经变化，请根据当前列表重新操作。');
        const event = {schema_version: 1, project_id: projectId, id: crypto.randomUUID(), record_id: recordId,
          action, state, restore_to: restoreTo, order: Math.max(Date.now(), before.max_order + 1), created_at: new Date().toISOString()};
        validateEvent(event, event.id + '.json', projectId);
        const folder = await (await directory()).getDirectoryHandle('events', {create: true});
        const name = event.id + '.json';
        try { await folder.getFileHandle(name); throw new Error('操作编号已存在，未覆盖任何记录。'); }
        catch (error) { if (error.name !== 'NotFoundError') throw error; }
        const handle = await folder.getFileHandle(name, {create: true}), text = canonical(event);
        let stream;
        try {
          stream = await handle.createWritable();
          await stream.write(text);
          await stream.close();
        } catch (error) {
          if (stream) try { await stream.abort(); } catch { /* May already have closed. */ }
          if (await (await handle.getFile()).text() !== text) throw error;
        }
        if (await (await handle.getFile()).text() !== text) throw new Error('写入后的回读不一致，不能确认操作成功。');
        saved = true;
        await load();
        const effective = getState(recordId);
        if (effective.event_id !== event.id) {
          notify('warning', '本次操作已保存，但另一个页面也修改了此条目；列表已显示磁盘上的最终状态。');
        } else {
          const names = {active: '主列表', archived: '已归档', trashed: '回收站'};
          notify('success', '已保存到本机，条目已移至' + names[effective.state] + '。');
        }
        return effective;
      } catch (error) {
        notify('error', (saved ? '操作已写入，但未能刷新状态，请重新连接核对：' : '整理未完成，未确认保存：') + error.message);
        throw error;
      } finally { busy = false; onStatus({busy, connected}); }
    }
    const ready = (async () => {
      notify('info', '当前显示上次生成的阅读快照；连接项目文件夹可载入最新归档状态并保存整理操作。');
      if (!UUID.test(projectId || '')) { notify('error', '此阅读页缺少项目身份，请重新生成首页。'); return; }
      let restored = null;
      try { restored = await remembered(); } catch { /* The disk remains authoritative. */ }
      if (!root && !busy) root = restored;
      if (root && !busy) await refresh();
    })();
    const onFocus = () => { void refresh(); };
    window.addEventListener('focus', onFocus);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) onFocus(); });
    return {connect, useDefault, refresh, change, getState, ready, getRoot: () => connected ? root : null};
  }
  window.IntelManagement = {create, validateEvent, foldEvents};
})();
