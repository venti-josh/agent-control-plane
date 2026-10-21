'use strict';
// Preferences stay in this browser; metrics always come from the local ACP server.
const activityOverlay = (() => {
  const tile = document.getElementById('activity-overlay');
  const handle = document.getElementById('activity-interact');
  const toggle = handle;
  const restore = document.getElementById('activity-restore');
  const native = window.webkit?.messageHandlers?.leoOverlay;
  if (native) document.body.classList.add('native-overlay');
  const key = native ? 'acp.leo.native.v1' : 'acp.activity-overlay.v1';
  let prefs = {collapsed:false, hidden:false}, pointer = null, inFlight = null;
  try {
    const saved = JSON.parse(localStorage.getItem(key));
    if (saved && typeof saved === 'object') prefs = {
      collapsed:saved.collapsed === true, hidden:saved.hidden === true,
      x:Number.isFinite(saved.x) ? saved.x : undefined,
      y:Number.isFinite(saved.y) ? saved.y : undefined,
    };
  } catch (_) { /* Storage may be disabled or contain an older value. */ }
  if (native) { prefs.hidden = false; prefs.collapsed = window.leoInitialCollapsed === true; }
  function save() { try { localStorage.setItem(key, JSON.stringify(prefs)); } catch (_) { /* Optional. */ } }
  function position(x = prefs.x, y = prefs.y) {
    if (tile.hidden) return;
    if (native) { native.postMessage({action:'resize', width:Math.ceil(tile.offsetWidth), height:Math.ceil(tile.offsetHeight), collapsed:prefs.collapsed}); return; }
    const box = tile.getBoundingClientRect(), edge = 12;
    const maxX = Math.max(0, innerWidth - box.width - edge);
    const maxY = Math.max(0, innerHeight - box.height - edge);
    prefs.x = Math.min(maxX, Math.max(Math.min(edge, maxX), x ?? maxX));
    prefs.y = Math.min(maxY, Math.max(Math.min(edge, maxY), y ?? maxY));
    tile.style.left = `${prefs.x}px`; tile.style.top = `${prefs.y}px`;
  }
  function renderPreferences() {
    tile.hidden = prefs.hidden;
    restore.hidden = !prefs.hidden;
    tile.classList.toggle('is-collapsed', prefs.collapsed);
    document.getElementById('activity-details').hidden = prefs.collapsed;
    toggle.setAttribute('aria-expanded', String(!prefs.collapsed));
    toggle.setAttribute('aria-label', `${prefs.collapsed ? 'Expand' : 'Collapse'} agent overlay`);
    position(); save();
  }
  function toggleExpanded() { prefs.collapsed = !prefs.collapsed; renderPreferences(); }
  function hide() {
    if (native) { native.postMessage({action:'hide'}); return; }
    prefs.hidden = true; renderPreferences(); restore.focus();
  }
  restore.onclick = () => { prefs.hidden = false; renderPreferences(); toggle.focus(); refresh(); };
  handle.addEventListener('click', event => { if (event.detail === 0) toggleExpanded(); });
  handle.addEventListener('pointerdown', event => {
    if (event.button !== 0 || native) return;
    event.preventDefault(); handle.focus();
    pointer = {id:event.pointerId, x:event.clientX, y:event.clientY, left:prefs.x, top:prefs.y, moved:false};
    handle.setPointerCapture(event.pointerId);
  });
  handle.addEventListener('pointermove', event => {
    if (!pointer || pointer.id !== event.pointerId) return;
    const dx = event.clientX-pointer.x, dy = event.clientY-pointer.y;
    if (Math.hypot(dx,dy) >= 4) pointer.moved = true;
    if (pointer.moved) { tile.classList.add('is-dragging'); position(pointer.left+dx,pointer.top+dy); }
  });
  function endDrag(event) {
    const click = pointer && !pointer.moved && event.type === 'pointerup';
    pointer = null; tile.classList.remove('is-dragging'); save();
    if (click) toggleExpanded();
  }
  handle.addEventListener('pointerup', endDrag);
  handle.addEventListener('pointercancel', endDrag);
  handle.addEventListener('lostpointercapture', endDrag);
  handle.addEventListener('keydown', event => {
    if (event.key === 'Escape') { event.preventDefault(); hide(); return; }
    const step = event.shiftKey ? 4 : 16;
    const moves = {ArrowLeft:[-step,0], ArrowRight:[step,0], ArrowUp:[0,-step], ArrowDown:[0,step]};
    if (native && (event.key === 'Home' || moves[event.key])) {
      event.preventDefault();
      if (event.key === 'Home') native.postMessage({action:'reset'});
      else native.postMessage({action:'move', dx:moves[event.key][0], dy:moves[event.key][1]});
      return;
    }
    if (event.key === 'Home') { event.preventDefault(); position(null, null); save(); }
    else if (moves[event.key]) {
      event.preventDefault(); const [dx,dy] = moves[event.key]; position(prefs.x + dx, prefs.y + dy); save();
    }
  });
  window.addEventListener('resize', () => { position(); save(); });
  new ResizeObserver(() => position()).observe(tile);
  function refresh() {
    if (inFlight) return inFlight;
    inFlight = update().finally(() => { inFlight = null; });
    return inFlight;
  }
  async function update() {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);
    const status = document.getElementById('activity-status');
    try {
      const response = await fetch('/api/activity', {signal:controller.signal, cache:'no-store'});
      if (!response.ok) throw new Error('Activity unavailable');
      const data = await response.json();
      if (data.available === false) throw new Error('Activity unavailable');
      if (![data.running, data.subagents, data.waiting_on_input, data.idle, ...['30m','1h','3h'].map(k => data.averages?.[k])]
        .every(n => Number.isFinite(n) && n >= 0)) throw new Error('Invalid activity');
      document.getElementById('activity-running').textContent = data.running;
      // Waiting on you: blocked on an approval or question, or finished and awaiting your next message.
      document.getElementById('activity-waiting').textContent = data.waiting_on_input + data.idle;
      for (const period of ['30m','1h','3h']) document.getElementById(`activity-${period}`).textContent = data.averages[period].toFixed(2);
      const average = data.averages['30m'];
      const mood = average >= 3 ? 'rainbow' : average >= 2 ? 'happy' : average >= 1 ? 'sad' : 'dead';
      tile.dataset.mood = mood;
      const moods = {rainbow:'Comet is ecstatic with a rainbow trail', happy:'Comet is happy and flying', sad:'Comet is sad with drooping ears and falling tears', dead:'Comet is dead: X eyes and a limp tail'};
      cometPet.setMood(mood);
      document.getElementById('activity-pet').setAttribute('aria-label', moods[mood]);
      status.hidden = true;
      tile.classList.remove('is-unavailable');
      native?.postMessage({action:'metrics', data:{...data, mood}});
    } catch {
      status.hidden = false; tile.classList.add('is-unavailable');
      for (const id of ['running','waiting','30m','1h','3h']) document.getElementById(`activity-${id}`).textContent = '—';
      tile.dataset.mood = 'unavailable';
      cometPet.setMood('unavailable');
      native?.postMessage({action:'metrics', data:{unavailable:true}});
      document.getElementById('activity-pet').setAttribute('aria-label', 'Comet is waiting; activity is unavailable');
    } finally { clearTimeout(timeout); }
  }
  renderPreferences(); native?.postMessage({action:'ready'}); refresh();
  setInterval(() => { if (native || (!document.hidden && !tile.hidden)) refresh(); }, 5000);
  document.addEventListener('visibilitychange', () => { if (native || (!document.hidden && !tile.hidden)) refresh(); });
  return {refresh, toggle:toggleExpanded};
})();
