/* PC dashboard. */
(() => {
  const { h, $, api, fmtBytes, fmtSpeed, fmtEta, fmtTime, icon, toast } = LD;
  const A = { admin: true };
  const call = (p, body) => api(p, { ...A, method: body === undefined ? 'GET' : 'POST', body });
  let S = null, tab = 'active', qrKey = '';
  const tr = new Map();

  $('#theme').onclick = LD.toggleTheme;
  const showErr = (m) => { const e = $('#err'); e.textContent = m || ''; e.classList.toggle('hidden', !m); };

  async function refresh() {
    S = await call('/api/admin/state');
    tr.clear(); S.transfers.forEach((t) => tr.set(t.id, t));
    render(); loadShared();
  }

  function render() {
    const on = S.sharing;
    const st = $('#status'); st.textContent = on ? 'Sharing on' : 'Sharing is off'; st.className = 'pill ' + (on ? 'on' : 'off');
    $('#toggle').textContent = on ? 'Stop sharing' : 'Start sharing';
    $('#toggle').className = 'btn ' + (on ? 'danger' : 'primary');
    $('#url').textContent = on ? S.url : (S.ip ? `http://${S.ip}:${S.port}` : 'No network found');
    $('#url').style.opacity = on ? 1 : .45;
    $('#pin').replaceChildren(...((S.pin || '······').split('').map((c) => h('span', {}, c))));
    $('#copy').disabled = !on; $('#regen').disabled = !on;
    const sel = $('#ip');
    sel.classList.toggle('hidden', on || S.ips.length < 2);
    sel.replaceChildren(...S.ips.map((ip) => h('option', { value: ip, selected: ip === S.ip }, ip)));
    $('#warn').classList.toggle('hidden', on === false && false);
    if (!S.ips.length && !on) showErr('No local network address found. Connect this PC to Wi-Fi or Ethernet, then reload.'); else if (!on) showErr('');
    // QR
    const key = on ? S.url + S.pin : '';
    if (key !== qrKey) {
      qrKey = key;
      if (on) fetchQR(); else { $('#qr').classList.add('hidden'); $('#qr-off').classList.remove('hidden'); }
    }
    // devices
    $('#devices').replaceChildren(...(S.sessions.length ? S.sessions.map((s) =>
      h('li', { class: 'item' }, h('div', { class: 'ico' }, '📱'),
        h('div', {}, h('div', { class: 'name' }, s.device), h('div', { class: 'meta' }, 'Paired ' + fmtTime(s.created))),
        h('button', { class: 'btn sm', onclick: async () => { await call('/api/admin/revoke', { id: s.id }); refresh(); } }, 'Disconnect')))
      : [h('li', { class: 'empty' }, on ? 'No phone paired yet.' : 'Sharing is off.')]));
    // settings (don't clobber fields being edited)
    if (!document.activeElement || !document.activeElement.closest('#card-settings')) fillSettings();
    $('#disk').textContent = 'Free space in the incoming folder: ' + fmtBytes(S.free_bytes);
    $('#startup-row').classList.toggle('hidden', !S.startup.supported); $('#s-startup').checked = S.startup.enabled;
    renderTransfers();
  }
  function fillSettings() {
    const s = S.settings; $('#s-upload').value = s.upload_dir; $('#s-shared').value = s.shared_dir;
    $('#s-max').value = s.max_file_mb; $('#s-conc').value = s.max_concurrent; $('#s-port').value = s.port;
  }

  async function fetchQR() {
    try {
      const res = await fetch('/api/admin/qr.svg', { headers: { 'X-LocalDrop': '1', 'X-Admin-Token': document.querySelector('meta[name=ld-admin-token]').content } });
      if (!res.ok) return;
      const img = $('#qr'); img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(await res.text());
      img.classList.remove('hidden', 'placeholder'); $('#qr-off').classList.add('hidden');
    } catch (_) {}
  }

  // ---- sharing on/off ----
  const dlg = $('#confirm');
  $('#toggle').onclick = async () => {
    showErr('');
    if (S.sharing) { try { S = await call('/api/admin/share', { enabled: false }); render(); } catch (e) { showErr(e.message); } return; }
    $('#c-net').textContent = ($('#ip').value || S.ip || 'your network') + ' (your local network)';
    dlg.showModal();
  };
  $('#c-no').onclick = () => dlg.close();
  $('#c-yes').onclick = async () => {
    dlg.close();
    try { S = await call('/api/admin/share', { enabled: true, ip: $('#ip').value || S.ip }); render(); }
    catch (e) { showErr(e.message); }
  };
  $('#copy').onclick = async () => { try { await navigator.clipboard.writeText(S.url); toast('Address copied'); } catch (_) { toast('Select the address and copy it manually'); } };
  $('#regen').onclick = async () => { if (confirm('Create a new QR code and PIN? Connected phones will be disconnected.')) { S = await call('/api/admin/regenerate', {}); render(); } };

  // ---- transfers ----
  $('#tr-tabs').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-t]'); if (!b) return; tab = b.dataset.t;
    document.querySelectorAll('#tr-tabs button').forEach((x) => x.setAttribute('aria-selected', x === b)); renderTransfers();
  });
  $('#clear').onclick = async () => { await call('/api/admin/clear-history', {}); tr.forEach((t, k) => t.status !== 'active' && tr.delete(k)); renderTransfers(); };
  const dirLabel = { upload: '↑ from phone', download: '↓ to phone', share: '＋ added to shared folder' };

  function renderTransfers() {
    const all = [...tr.values()].sort((a, b) => b.started - a.started);
    const count = { active: 0, done: 0, failed: 0 };
    all.forEach((t) => { count[t.status === 'active' ? 'active' : t.status === 'done' ? 'done' : 'failed']++; });
    document.querySelectorAll('#tr-tabs button').forEach((b) => { b.textContent = { active: 'Active', done: 'Completed', failed: 'Failed' }[b.dataset.t] + (count[b.dataset.t] ? ` (${count[b.dataset.t]})` : ''); });
    const rows = all.filter((t) => (tab === 'active' ? t.status === 'active' : tab === 'done' ? t.status === 'done' : t.status === 'failed' || t.status === 'cancelled'));
    $('#transfers').replaceChildren(...(rows.length ? rows.map((t) => {
      const pct = t.size ? Math.min(100, (t.bytes / t.size) * 100) : (t.status === 'done' ? 100 : 0);
      let meta;
      if (t.status === 'active') meta = [`${dirLabel[t.direction]} · ${t.device}`, `${fmtBytes(t.bytes)} / ${fmtBytes(t.size)}`, fmtSpeed(t.speed), fmtEta((t.size - t.bytes) / (t.speed || 1))].filter(Boolean).join(' · ');
      else if (t.status === 'done') meta = `${dirLabel[t.direction]} · ${t.device} · ${fmtBytes(t.size)} · ${fmtTime(t.finished)}` + (t.folder ? ` · ${t.folder}` : '');
      else meta = `${dirLabel[t.direction]} · ${t.device} · ${t.error || t.status} · ${fmtTime(t.finished)}`;
      return h('li', { class: 'item' }, h('div', { class: 'ico' }, icon(t.name)),
        h('div', { style: 'min-width:0' }, h('div', { class: 'name' }, t.name),
          t.status === 'active' || t.status === 'done' ? h('div', { class: 'bar ' + (t.status === 'done' ? 'done' : '') }, h('i', { style: `width:${pct}%` })) : null,
          h('div', { class: 'meta ' + (t.status === 'done' ? 'good' : t.status === 'active' ? '' : 'bad') }, meta)),
        t.status === 'active' ? h('button', { class: 'btn sm', onclick: () => call('/api/admin/cancel', { id: t.id }) }, 'Cancel') : h('div'));
    }) : [h('li', { class: 'empty' }, tab === 'active' ? 'No active transfers.' : tab === 'done' ? 'No completed transfers yet.' : 'No failed transfers.')]));
  }

  // ---- shared folder + drag & drop ----
  async function loadShared() {
    try {
      const d = await call('/api/admin/shared');
      $('#shared').replaceChildren(...(d.entries.length ? d.entries.slice(0, 50).map((e) =>
        h('li', { class: 'item' }, h('div', { class: 'ico' }, icon(e.name, e.dir ? 'folder' : e.type)),
          h('div', { style: 'min-width:0' }, h('div', { class: 'name' }, e.name), h('div', { class: 'meta' }, e.dir ? 'Folder' : fmtBytes(e.size))),
          h('button', { class: 'btn sm', 'aria-label': 'Remove ' + e.name, onclick: async () => { try { await call('/api/admin/shared/delete', { path: e.name }); loadShared(); } catch (er) { toast(er.message); } } }, 'Remove')))
        : [h('li', { class: 'empty' }, 'The shared folder is empty.')]));
    } catch (_) {}
  }
  const drop = $('#drop'), pick = $('#pick');
  drop.onclick = () => pick.click();
  drop.onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') pick.click(); };
  ['dragenter', 'dragover'].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add('over'); }));
  ['dragleave', 'drop'].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove('over'); }));
  drop.addEventListener('drop', (e) => addShared([...e.dataTransfer.files]));
  pick.onchange = () => { addShared([...pick.files]); pick.value = ''; };
  window.addEventListener('dragover', (e) => e.preventDefault());
  window.addEventListener('drop', (e) => e.preventDefault());

  function addShared(files) {
    let i = 0, running = 0;
    const next = () => {
      while (running < 2 && i < files.length) {
        const f = files[i++]; running++;
        const x = new XMLHttpRequest(); x.open('POST', '/api/admin/shared-upload');
        x.setRequestHeader('X-LocalDrop', '1'); x.setRequestHeader('X-Admin-Token', document.querySelector('meta[name=ld-admin-token]').content);
        x.setRequestHeader('X-File-Name', encodeURIComponent(f.name)); x.setRequestHeader('X-File-Size', String(f.size));
        x.setRequestHeader('X-Last-Modified', String(f.lastModified || 0));
        const done = () => { running--; loadShared(); next(); };
        x.onload = () => { if (x.status !== 200) { let m = 'Could not add ' + f.name; try { m = JSON.parse(x.responseText).error || m; } catch (_) {} toast(m); } done(); };
        x.onerror = done;
        const fd = new FormData(); fd.append('file', f, f.name); x.send(fd);
      }
    };
    next();
  }

  // ---- settings ----
  $('#save').onclick = async () => {
    try {
      S = await call('/api/admin/settings', { upload_dir: $('#s-upload').value, shared_dir: $('#s-shared').value, max_file_mb: $('#s-max').value, max_concurrent: $('#s-conc').value, port: $('#s-port').value });
      render(); toast('Settings saved' + (+$('#s-port').value !== S.port ? ' (port changes apply after restart)' : ''));
    } catch (e) { toast(e.message); }
  };
  $('#s-startup').onchange = async (e) => { try { S = await call('/api/admin/startup', { enabled: e.target.checked }); render(); } catch (er) { e.target.checked = !e.target.checked; toast(er.message); } };
  $('#open-shared').onclick = () => call('/api/admin/open-folder', { which: 'shared' }).catch((e) => toast(e.message));
  $('#open-up').onclick = () => call('/api/admin/open-folder', { which: 'uploads' }).catch((e) => toast(e.message));

  // ---- live updates ----
  LD.connectWS('/ws/admin?token=' + encodeURIComponent(document.querySelector('meta[name=ld-admin-token]').content), (m) => {
    if (m.type === 'transfer') { tr.set(m.transfer.id, m.transfer); renderTransfers(); if (m.transfer.status === 'done' && m.transfer.direction === 'share') loadShared(); }
    else if (m.type === 'state') refresh().catch(() => {});
    else if (m.type === 'sessions') { S.sessions = m.sessions; render(); }
    else if (m.type === 'history_cleared') { tr.forEach((t, k) => t.status !== 'active' && tr.delete(k)); renderTransfers(); }
  }, (ok) => { if (ok) refresh().catch((e) => showErr(e.message)); });
})();
