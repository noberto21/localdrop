/* Phone / browser side of LocalDrop. */
(() => {
  const { h, $, api, fmtBytes, fmtSpeed, fmtEta, fmtTime, icon, toast } = LD;
  $('#theme').onclick = LD.toggleTheme;
  const paired = document.body.dataset.paired === 'yes';

  // ---------- pairing ----------
  if (!paired) {
    $('#pair-view').classList.remove('hidden');
    const err = (m) => { const e = $('#pair-err'); e.textContent = m; e.classList.toggle('hidden', !m); };
    const q = new URLSearchParams(location.search).get('pair');
    if (q === 'failed') err('That QR code is no longer valid. Scan the current one, or enter the PIN.');
    if (q === 'locked') err('Too many wrong attempts. Wait a minute and try again.');
    const go = async () => {
      err('');
      try { await api('/api/pair', { method: 'POST', body: { code: $('#pin').value } }); location.replace('/'); }
      catch (e) { err(e.message); }
    };
    $('#pair-go').onclick = go;
    $('#pin').addEventListener('keydown', (e) => { if (e.key === 'Enter') go(); });
    return;
  }

  $('#app-view').classList.remove('hidden');
  $('#conn').classList.remove('hidden');
  let me = { max_concurrent: 3, max_file_mb: 0 };
  const queue = [];            // local upload items
  let running = 0, nextId = 1;
  const server = new Map();    // server-side transfers for this session

  // ---------- connection status / live events ----------
  LD.connectWS('/ws', (m) => {
    if (m.type === 'transfer') { server.set(m.transfer.id, m.transfer); renderRecent(); }
    if (m.type === 'history_cleared') { server.clear(); renderRecent(); }
  }, (ok) => {
    const c = $('#conn'); c.textContent = ok ? 'Connected' : 'Reconnecting…'; c.className = 'pill ' + (ok ? 'on' : 'off');
  });

  api('/api/me').then((d) => { me = d; $('#device').textContent = d.device; }).catch(sessionLost);
  api('/api/transfers').then((d) => { d.transfers.forEach((t) => server.set(t.id, t)); renderRecent(); }).catch(() => {});

  function sessionLost(e) {
    if (e.status === 401 || e.status === 503) location.replace('/');
  }
  $('#logout').onclick = async (e) => { e.preventDefault(); try { await api('/api/logout', { method: 'POST' }); } catch (_) {} location.replace('/'); };

  // ---------- picking files ----------
  const fi = $('#file-input'), di = $('#folder-input');
  $('#pick').onclick = () => fi.click();
  if ('webkitdirectory' in di) $('#pick-folder').classList.remove('hidden');
  $('#pick-folder').onclick = () => di.click();
  fi.onchange = () => { addFiles([...fi.files]); fi.value = ''; };
  di.onchange = () => { addFiles([...di.files], true); di.value = ''; };

  const drop = $('#drop');
  if (matchMedia('(hover: hover)').matches) {
    drop.classList.remove('hidden');
    ['dragenter', 'dragover'].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add('over'); }));
    ['dragleave', 'drop'].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove('over'); }));
    drop.addEventListener('drop', (e) => addFiles([...e.dataTransfer.files]));
    drop.onclick = () => fi.click();
  }

  function addFiles(files, folder = false) {
    for (const f of files) {
      const it = { id: nextId++, file: f, rel: folder ? f.webkitRelativePath : '', status: 'queued', loaded: 0, speed: 0, error: '', xhr: null, folderOut: '', renamed: '' };
      if (me.max_file_mb && f.size > me.max_file_mb * 1048576) { it.status = 'failed'; it.error = `Larger than the ${me.max_file_mb} MB limit set on the PC.`; }
      queue.push(it);
    }
    renderQueue(); pump();
  }

  function pump() {
    while (running < me.max_concurrent) {
      const it = queue.find((q) => q.status === 'queued');
      if (!it) break;
      start(it);
    }
    $('#keep-open').classList.toggle('hidden', !queue.some((q) => q.status === 'uploading' || q.status === 'queued'));
  }

  function start(it) {
    it.status = 'uploading'; it.loaded = 0; it.speed = 0; it.error = ''; running++;
    const xhr = new XMLHttpRequest(); it.xhr = xhr;
    xhr.open('POST', '/api/upload');
    xhr.setRequestHeader('X-LocalDrop', '1');
    xhr.setRequestHeader('X-File-Name', encodeURIComponent(it.file.name));
    xhr.setRequestHeader('X-File-Size', String(it.file.size));
    xhr.setRequestHeader('X-Last-Modified', String(it.file.lastModified || 0));
    if (it.rel) xhr.setRequestHeader('X-Relative-Path', encodeURIComponent(it.rel));
    let last = performance.now(), lastLoaded = 0;
    xhr.upload.onprogress = (e) => {
      const now = performance.now();
      if (now - last >= 500) { const inst = ((e.loaded - lastLoaded) / (now - last)) * 1000; it.speed = it.speed ? 0.6 * it.speed + 0.4 * inst : inst; last = now; lastLoaded = e.loaded; }
      it.loaded = e.loaded; renderQueue();
    };
    const end = () => { running--; it.xhr = null; renderQueue(); pump(); };
    xhr.onload = () => {
      let d = {}; try { d = JSON.parse(xhr.responseText); } catch (_) {}
      if (xhr.status === 200 && d.ok) { it.status = 'done'; it.loaded = it.file.size; it.folderOut = d.folder; it.renamed = d.renamed ? d.name : ''; it.serverId = d.id; }
      else if (xhr.status === 429) { it.status = 'queued'; running--; it.xhr = null; setTimeout(pump, 1500); renderQueue(); return; }
      else if (xhr.status === 401 || xhr.status === 503) { it.status = 'failed'; it.error = d.detail || d.error || 'Disconnected from the PC.'; sessionLostSoon(); }
      else { it.status = 'failed'; it.error = d.error || d.detail || `Transfer failed (${xhr.status}).`; }
      end();
    };
    xhr.onerror = () => { it.status = 'failed'; it.error = 'Connection to the PC was lost. Check that both devices are on the same Wi-Fi.'; end(); };
    xhr.onabort = () => { it.status = 'cancelled'; it.error = 'Cancelled.'; end(); };
    const fd = new FormData(); fd.append('file', it.file, it.file.name);
    xhr.send(fd);
  }
  let lostTimer; const sessionLostSoon = () => { clearTimeout(lostTimer); lostTimer = setTimeout(() => location.replace('/'), 2500); };

  // ---------- rendering ----------
  function renderQueue() {
    const ul = $('#queue');
    $('#queue-card').classList.toggle('hidden', queue.length === 0);
    ul.replaceChildren(...queue.map((it) => {
      const pct = it.file.size ? Math.min(100, (it.loaded / it.file.size) * 100) : (it.status === 'done' ? 100 : 0);
      let meta, cls = '';
      if (it.status === 'uploading') meta = [fmtBytes(it.loaded) + ' / ' + fmtBytes(it.file.size), fmtSpeed(it.speed), fmtEta((it.file.size - it.loaded) / (it.speed || 1))].filter(Boolean).join(' · ');
      else if (it.status === 'queued') meta = 'Waiting · ' + fmtBytes(it.file.size);
      else if (it.status === 'done') { meta = `Sent · ${fmtBytes(it.file.size)} · saved to ${it.folderOut}` + (it.renamed ? ` as “${it.renamed}”` : ''); cls = 'good'; }
      else { meta = it.error; cls = 'bad'; }
      const actions = [];
      if (it.status === 'uploading' || it.status === 'queued') actions.push(h('button', { class: 'btn sm', onclick: () => cancel(it) }, 'Cancel'));
      if (it.status === 'failed' || it.status === 'cancelled') actions.push(h('button', { class: 'btn sm primary', onclick: () => { it.status = 'queued'; renderQueue(); pump(); } }, 'Retry'));
      return h('li', { class: 'item' },
        h('div', { class: 'ico' }, icon(it.file.name)),
        h('div', { style: 'min-width:0' },
          h('div', { class: 'name' }, it.rel || it.file.name),
          h('div', { class: 'bar ' + (it.status === 'done' ? 'done' : it.status === 'failed' ? 'bad' : '') }, h('i', { style: `width:${pct}%` })),
          h('div', { class: 'meta ' + cls }, meta)),
        h('div', {}, actions));
    }));
  }
  function cancel(it) { if (it.xhr) it.xhr.abort(); else { it.status = 'cancelled'; it.error = 'Cancelled.'; renderQueue(); } }
  $('#clear-queue').onclick = () => { for (let i = queue.length - 1; i >= 0; i--) if (['done', 'cancelled', 'failed'].includes(queue[i].status)) queue.splice(i, 1); renderQueue(); };

  function renderRecent() {
    const local = new Set(queue.map((q) => q.serverId).filter(Boolean));
    const rows = [...server.values()].filter((t) => t.status !== 'active' ? !local.has(t.id) : t.direction === 'download').sort((a, b) => b.started - a.started).slice(0, 30);
    $('#recent-list').replaceChildren(...(rows.length ? rows.map((t) => {
      const sym = t.direction === 'download' ? '↓' : '↑';
      let meta = `${sym} ${fmtBytes(t.size)} · ${fmtTime(t.finished || t.started)}`, cls = '';
      if (t.status === 'active') meta = `↓ ${fmtBytes(t.bytes)} / ${fmtBytes(t.size)} ${fmtSpeed(t.speed)}`;
      else if (t.status !== 'done') { meta = t.error || t.status; cls = 'bad'; }
      return h('li', { class: 'item' }, h('div', { class: 'ico' }, icon(t.name)),
        h('div', { style: 'min-width:0' }, h('div', { class: 'name' }, t.name), h('div', { class: 'meta ' + cls }, meta)), h('div'));
    }) : [h('li', { class: 'empty' }, 'Nothing yet. Select files to send them to your PC.')]));
  }
  renderQueue(); renderRecent();

  // ---------- PC files ----------
  const tabs = { recent: [$('#tab-recent'), $('#recent')], pc: [$('#tab-pc'), $('#pc')] };
  function showTab(name) {
    for (const [k, [b, p]] of Object.entries(tabs)) { b.setAttribute('aria-selected', k === name); p.classList.toggle('hidden', k !== name); }
    if (name === 'pc') loadFiles(cwd);
  }
  tabs.recent[0].onclick = () => showTab('recent'); tabs.pc[0].onclick = () => showTab('pc');
  let cwd = '', selected = new Set();
  async function loadFiles(path) {
    try {
      const d = await api('/api/files?path=' + encodeURIComponent(path));
      cwd = d.path; selected.clear(); renderFiles(d);
    } catch (e) { sessionLost(e); $('#files').replaceChildren(h('li', { class: 'empty' }, e.message)); }
  }
  function renderFiles(d) {
    const parts = d.path ? d.path.split('/') : [];
    $('#crumbs').replaceChildren(h('button', { class: 'btn sm ghost', onclick: () => loadFiles('') }, 'Shared folder'),
      ...parts.map((p, i) => h('button', { class: 'btn sm ghost', onclick: () => loadFiles(parts.slice(0, i + 1).join('/')) }, '/ ' + p)));
    const join = (n) => (d.path ? d.path + '/' : '') + n;
    $('#files').replaceChildren(...(d.entries.length ? d.entries.map((e) => {
      const cb = e.dir ? null : h('input', { type: 'checkbox', 'aria-label': 'Select ' + e.name, onchange: (ev) => { ev.target.checked ? selected.add(join(e.name)) : selected.delete(join(e.name)); $('#dl-selected').disabled = !selected.size; } });
      return h('li', { class: 'item' }, h('div', { class: 'ico' }, icon(e.name, e.dir ? 'folder' : e.type)),
        h('div', { style: 'min-width:0' }, h('div', { class: 'name' }, e.name), h('div', { class: 'meta' }, e.dir ? 'Folder' : `${fmtBytes(e.size)} · ${(e.type || '').split('/').pop()}`)),
        h('div', { class: 'row' }, cb,
          e.dir ? h('button', { class: 'btn sm', onclick: () => loadFiles(join(e.name)) }, 'Open') : h('button', { class: 'btn sm primary', onclick: () => download([join(e.name)]) }, 'Download')));
    }) : [h('li', { class: 'empty' }, 'This folder is empty. Add files to the shared folder on the PC.')]));
    $('#dl-selected').disabled = true;
  }
  function download(paths) {
    paths.forEach((p, i) => setTimeout(() => {
      const a = h('a', { href: '/api/download?path=' + encodeURIComponent(p), download: p.split('/').pop() }); document.body.append(a); a.click(); a.remove();
    }, i * 900));
    toast(paths.length > 1 ? `Downloading ${paths.length} files…` : 'Download started');
  }
  $('#dl-selected').onclick = () => download([...selected]);
  $('#refresh').onclick = () => loadFiles(cwd);
})();
