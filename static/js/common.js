/* Shared helpers: theme, DOM builder (never innerHTML: file names are untrusted), API, formatting. */
(function () {
  const saved = localStorage.getItem('ld-theme');
  document.documentElement.dataset.theme = saved || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
})();

const LD = (() => {
  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
      else if (v === true) el.setAttribute(k, '');
      else el.setAttribute(k, v);
    }
    for (const kid of kids.flat()) { if (kid != null && kid !== false) el.append(kid); }
    return el;
  }
  const $ = (s, r = document) => r.querySelector(s);

  async function api(path, { method = 'GET', body, headers = {}, admin = false } = {}) {
    const hd = { 'X-LocalDrop': '1', ...headers };
    if (admin) hd['X-Admin-Token'] = document.querySelector('meta[name=ld-admin-token]').content;
    if (body !== undefined) hd['Content-Type'] = 'application/json';
    let res;
    try { res = await fetch(path, { method, headers: hd, body: body !== undefined ? JSON.stringify(body) : undefined, credentials: 'same-origin' }); }
    catch (e) { throw new Error('Cannot reach the PC. Check that LocalDrop is running and you are on the same Wi-Fi.'); }
    let data = null;
    try { data = await res.json(); } catch (_) { /* not JSON */ }
    if (!res.ok) { const err = new Error((data && (data.detail || data.error)) || `Request failed (${res.status})`); err.status = res.status; throw err; }
    return data;
  }

  const fmtBytes = (n) => {
    if (!n) return '0 B';
    const u = ['B', 'KB', 'MB', 'GB', 'TB']; let i = Math.min(4, Math.floor(Math.log(n) / Math.log(1024)));
    const v = n / 1024 ** i; return (v >= 100 || i === 0 ? v.toFixed(0) : v.toFixed(1)) + ' ' + u[i];
  };
  const fmtSpeed = (s) => (s > 0 ? fmtBytes(s) + '/s' : '');
  const fmtEta = (sec) => {
    if (!isFinite(sec) || sec <= 0) return '';
    if (sec < 60) return Math.ceil(sec) + ' s left';
    if (sec < 3600) return Math.floor(sec / 60) + ' min ' + Math.ceil(sec % 60) + ' s left';
    return Math.floor(sec / 3600) + ' h ' + Math.ceil((sec % 3600) / 60) + ' min left';
  };
  const fmtTime = (t) => t ? new Date(t * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '';
  const icon = (name, mime) => {
    const n = (name || '').toLowerCase(), m = mime || '';
    if (m === 'folder') return '📁';
    if (/\.(jpe?g|png|gif|webp|heic|bmp|svg)$/.test(n) || m.startsWith('image/')) return '🖼️';
    if (/\.(mp4|mkv|mov|avi|webm|3gp)$/.test(n) || m.startsWith('video/')) return '🎬';
    if (/\.(mp3|wav|m4a|flac|ogg|aac)$/.test(n) || m.startsWith('audio/')) return '🎵';
    if (/\.apk$/.test(n)) return '📱';
    if (/\.(zip|rar|7z|tar|gz)$/.test(n)) return '📦';
    if (/\.(pdf|docx?|xlsx?|pptx?|txt|csv)$/.test(n)) return '📄';
    return '📎';
  };
  function toggleTheme() {
    const r = document.documentElement, next = r.dataset.theme === 'dark' ? 'light' : 'dark';
    r.dataset.theme = next; localStorage.setItem('ld-theme', next);
  }
  function toast(msg) {
    const t = h('div', { class: 'toast', role: 'status' }, msg); document.body.append(t); setTimeout(() => t.remove(), 3000);
  }
  function connectWS(path, onMessage, onStatus) {
    let ws, retry = 0, closed = false, ping;
    const open = () => {
      ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + path);
      ws.onopen = () => { retry = 0; onStatus && onStatus(true); clearInterval(ping); ping = setInterval(() => { try { ws.send('ping'); } catch (_) {} }, 25000); };
      ws.onmessage = (e) => { try { onMessage(JSON.parse(e.data)); } catch (_) {} };
      ws.onclose = () => { clearInterval(ping); onStatus && onStatus(false); if (!closed) setTimeout(open, Math.min(8000, 500 * 2 ** retry++)); };
    };
    open();
    return { close() { closed = true; ws && ws.close(); } };
  }
  return { h, $, api, fmtBytes, fmtSpeed, fmtEta, fmtTime, icon, toggleTheme, toast, connectWS };
})();
