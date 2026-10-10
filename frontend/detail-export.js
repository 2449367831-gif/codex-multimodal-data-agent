(function (global) {
  "use strict";
  const MAX_BYTES = 32 * 1024 * 1024;
  const validId = id => /^ex_[a-f0-9]{32}$/.test(id || "");
  const hex = bytes => Array.from(new Uint8Array(bytes), n => n.toString(16).padStart(2, "0")).join("");
  const digest = async bytes => hex(await crypto.subtle.digest("SHA-256", bytes));
  const fail = code => Object.assign(new Error(code), {code});
  const messages = {
    unsupported: "此浏览器不支持连续文件写入，请使用桌面版 Chrome 导出全部明细。",
    file_changed: "文件内容与已保存进度不一致，已停止续写。原文件未被覆盖。",
    file_not_empty: "请选择新的空文件，原有文件未被覆盖。",
    detail_source_changed: "数据或口径已变化，不能继续拼接旧文件。请重新查询后导出。",
    export_expired: "导出已过期，请重新查询。已保存文件尚不完整。",
    export_busy: "后台仍在处理导出，请稍后继续。",
    export_unavailable: "本批导出未完成，已保存进度保留。请稍后继续。",
    query_timeout: "本批查询超时，已保存进度保留。可继续导出。",
    query_backend_unavailable: "数仓暂时无法连接，已保存进度保留。请稍后继续。",
    query_unavailable: "取数服务暂时无法连接，已保存进度保留。请稍后继续。",
    query_failed: "本批查询失败，已保存进度保留。可重试续传。",
    query_truncated: "本批未完整返回，未写入文件。已保存进度保留。",
    query_invalid_result: "本批结果校验未通过，未写入文件。已保存进度保留。",
    query_rejected: "本批查询被后端拒绝，未写入文件。已保存进度保留。",
    invalid_result: "本批未通过完整性校验，未继续写入。可重试续传。",
    identity_changed: "会话身份已变化，已停止写入。",
    storage_failed: "无法保存导出进度，已停止写入。请检查浏览器存储和磁盘空间。",
    network_error: "连接中断，已保存进度保留。服务恢复后可继续导出。"
  };

  function metadata(raw, id) {
    if (!raw || raw.export_id !== id || !validId(id)
        || !["total_rows", "chunk_rows", "chunks", "expires_at"].every(k => Number.isSafeInteger(raw[k]) && raw[k] > 0)
        || raw.total_rows > 100000000 || raw.chunk_rows > 50000 || raw.chunks > 2000
        || raw.chunks !== Math.ceil(raw.total_rows / raw.chunk_rows)) throw fail("invalid_result");
    return raw;
  }

  function checkpoint(raw, meta) {
    if (!raw || raw.exportId !== meta.export_id || raw.total !== meta.total_rows || raw.chunkRows !== meta.chunk_rows
        || !Number.isSafeInteger(raw.index) || raw.index < 0 || raw.index > meta.chunks
        || !Array.isArray(raw.parts) || raw.parts.length !== raw.index
        || !Number.isSafeInteger(raw.bytes) || raw.bytes < 0) throw fail("storage_failed");
    let bytes = 0;
    for (let i = 0; i < raw.parts.length; i++) {
      const part = raw.parts[i];
      if (!part || !Number.isSafeInteger(part.bytes) || part.bytes < 1 || part.bytes > MAX_BYTES
          || !/^[a-f0-9]{64}$/.test(part.hash || "")
          || part.rows !== Math.min(meta.chunk_rows, meta.total_rows - i * meta.chunk_rows)) throw fail("storage_failed");
      bytes += part.bytes;
    }
    if (bytes !== raw.bytes || typeof raw.complete !== "boolean"
        || (raw.complete && raw.index !== meta.chunks) || (raw.pending && raw.index >= meta.chunks)) throw fail("storage_failed");
    return raw;
  }

  function storage() {
    let opening;
    const open = () => opening ||= new Promise((resolve, reject) => {
      const request = indexedDB.open("royal-tiger-detail-exports", 1);
      request.onupgradeneeded = () => request.result.createObjectStore("jobs", {keyPath:"key"});
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(fail("storage_failed"));
    });
    return async (key, value) => {
      try {
        const db = await open();
        return await new Promise((resolve, reject) => {
          const transaction = db.transaction("jobs", value ? "readwrite" : "readonly");
          const request = value ? transaction.objectStore("jobs").put(value) : transaction.objectStore("jobs").get(key);
          transaction.oncomplete = () => resolve(value || request.result);
          transaction.onerror = transaction.onabort = () => reject(fail("storage_failed"));
        });
      } catch (_) { throw fail("storage_failed"); }
    };
  }

  async function verifiedFile(record, save) {
    const file = await record.handle.getFile();
    let offset = 0;
    for (const part of record.parts) {
      if (await digest(await file.slice(offset, offset + part.bytes).arrayBuffer()) !== part.hash) throw fail("file_changed");
      offset += part.bytes;
    }
    // A write-ahead receipt distinguishes a committed append from an unrelated
    // user edit. Never truncate an unexpected tail to force recovery.
    if (record.pending) {
      const pending = record.pending;
      if (!Number.isSafeInteger(pending.bytes) || pending.bytes < 1 || pending.bytes > MAX_BYTES
          || !/^[a-f0-9]{64}$/.test(pending.hash || "")
          || pending.rows !== Math.min(record.chunkRows, record.total - record.index * record.chunkRows)) throw fail("storage_failed");
      if (file.size === offset + pending.bytes
          && await digest(await file.slice(offset).arrayBuffer()) === pending.hash) {
        record.parts.push(pending); record.bytes += pending.bytes; record.index++;
      } else if (file.size !== offset) throw fail("file_changed");
      record.pending = null;
      await save(record.key, record);
    } else if (file.size !== offset) throw fail("file_changed");
  }

  async function chunkBytes(response, meta, index) {
    if (!response.ok) {
      let code = "network_error";
      try { code = (await response.json()).error?.code || code; } catch (_) { /* No internal error text. */ }
      throw fail(Object.hasOwn(messages, code) ? code : "network_error");
    }
    const rows = Math.min(meta.chunk_rows, meta.total_rows - index * meta.chunk_rows);
    const hash = response.headers.get("X-Chunk-SHA256");
    if (response.headers.get("X-Export-Id") !== meta.export_id
        || response.headers.get("X-Chunk-Index") !== String(index)
        || response.headers.get("X-Chunk-Rows") !== String(rows)
        || response.headers.get("X-Loaded-Rows") !== String(index * meta.chunk_rows + rows)
        || response.headers.get("X-Export-Complete") !== String(index === meta.chunks - 1)
        || !/^[a-f0-9]{64}$/.test(hash || "")) throw fail("invalid_result");
    const reader = response.body.getReader();
    const pieces = []; let size = 0;
    try {
      while (true) {
        const {value, done} = await reader.read();
        if (done) break;
        size += value.byteLength;
        if (size > MAX_BYTES) throw fail("invalid_result");
        pieces.push(value);
      }
    } finally { await reader.cancel(); }
    const data = new Uint8Array(size); let offset = 0;
    for (const piece of pieces) { data.set(piece, offset); offset += piece.length; }
    if (!size || await digest(data) !== hash) throw fail("invalid_result");
    return {data, receipt:{hash, bytes:size, rows}};
  }

  function create({auth, changed}) {
    const save = storage(), states = new Map();
    const stateFor = id => {
      const key = `${auth().conversation}:${id}`;
      if (!states.has(key)) states.set(key, {id, key, ready:false, running:false, record:null, message:""});
      return states.get(key);
    };
    function hydrate(id) {
      if (!validId(id)) return;
      const state = stateFor(id);
      if (state.ready || state.loading) return;
      state.loading = true;
      save(state.key).then(record => { state.record = record || null; state.ready = true; })
        .catch(() => { state.message = messages.storage_failed; state.ready = true; })
        .finally(() => { state.loading = false; changed(); });
    }
    function status(id) {
      const state = stateFor(id), record = state.record;
      return {...state, loaded:record ? Math.min(record.index * record.chunkRows, record.total) : 0,
        complete:record?.complete === true, total:record?.total || 0};
    }
    function pause(id) {
      const state = stateFor(id); state.paused = true; state.abort?.abort();
      state.message = "正在暂停，已保存文件尚不完整"; changed();
    }
    async function start(id, fresh = false) {
      if (!validId(id)) return;
      const state = stateFor(id);
      if (state.running || !state.ready) return;
      if (!global.showSaveFilePicker || !navigator.locks) { state.message = messages.unsupported; changed(); return; }
      const identity = auth();
      const sameIdentity = () => {
        if (auth().conversation !== identity.conversation || !auth().csrf) throw fail("identity_changed");
      };
      state.running = true; state.paused = false; state.errorCode = ""; state.message = "正在准备文件"; changed();
      try {
        // Invoke the native permission/picker in the click's activation window.
        let handle = fresh ? null : state.record?.handle;
        if (handle) {
          if (await handle.requestPermission({mode:"readwrite"}) !== "granted") throw fail("storage_failed");
        } else handle = await global.showSaveFilePicker({suggestedName:`皇家小虎出库明细-${id.slice(-8)}.csv`,
          types:[{description:"CSV", accept:{"text/csv":[".csv"]}}]});
        await navigator.locks.request("royal-tiger-export-writer", {ifAvailable:true}, async lock => {
          if (!lock) throw fail("export_busy");
          sameIdentity();
          const response = await fetch(`/v1/exports/${id}`, {credentials:"same-origin", cache:"no-store", signal:AbortSignal.timeout(10000)});
          if (!response.ok) {
            const code = (await response.json()).error?.code;
            throw fail(Object.hasOwn(messages, code) ? code : "network_error");
          }
          const meta = metadata(await response.json(), id);
          const latest = fresh ? null : await save(state.key);
          let record = latest || {key:state.key, exportId:id, total:meta.total_rows, chunkRows:meta.chunk_rows,
            handle, index:0, bytes:0, parts:[], pending:null, complete:false};
          checkpoint(record, meta);
          if (!latest && (await handle.getFile()).size) throw fail("file_not_empty");
          state.record = record;
          await save(record.key, record);
          state.message = "正在核对已保存内容"; changed();
          await verifiedFile(record, save);
          while (record.index < meta.chunks) {
            if (state.paused) break;
            sameIdentity();
            state.message = "正在取回并核对下一批，文件尚不完整"; changed();
            const controller = new AbortController(); state.abort = controller;
            const timer = setTimeout(() => controller.abort(), 210000);
            let part;
            try {
              part = await chunkBytes(await fetch(`/v1/exports/${id}/chunks/${record.index}`, {
                method:"POST", credentials:"same-origin", cache:"no-store", signal:controller.signal,
                headers:{"X-CSRF-Token":auth().csrf}}), meta, record.index);
            } finally { clearTimeout(timer); state.abort = null; }
            sameIdentity();
            if (state.paused) break;
            if ((await record.handle.getFile()).size !== record.bytes) throw fail("file_changed");
            record.pending = part.receipt;
            await save(record.key, record);
            let writer;
            try {
              writer = await record.handle.createWritable({keepExistingData:true});
              await writer.seek(record.bytes); await writer.write(part.data); await writer.close(); writer = null;
            } finally { if (writer) await writer.abort().catch(() => {}); }
            record.parts.push(part.receipt); record.index++; record.bytes += part.data.length; record.pending = null;
            await save(record.key, record); changed();
          }
          if (record.index === meta.chunks) {
            sameIdentity(); state.message = "正在核验完整文件"; changed();
            await verifiedFile(record, save);
            record.complete = true; await save(record.key, record);
            state.message = "全部明细已保存，文件完整性校验通过";
          } else state.message = "已暂停，已保存文件尚不完整";
        });
      } catch (error) {
        state.errorCode = error.code || (error.name === "NotAllowedError" || error.name === "QuotaExceededError" ? "storage_failed" : "network_error");
        state.message = state.paused ? "已暂停，已保存文件尚不完整" : error.name === "AbortError"
          ? "操作已中断，已保存进度保留" : messages[state.errorCode] || messages.network_error;
        // Reload the committed journal, not a possibly unpersisted local mutation.
        try {
          state.record = await save(state.key) || null;
          if (state.record?.complete && state.errorCode === "file_changed") {
            state.record.complete = false;
            await save(state.key, state.record);
          }
        } catch (_) { state.message = messages.storage_failed; }
      } finally { state.running = false; state.abort = null; changed(); }
    }
    return {hydrate, status, start, pause};
  }
  global.DetailExport = {create, metadata, checkpoint, verifiedFile, chunkBytes};
})(typeof window === "undefined" ? globalThis : window);
