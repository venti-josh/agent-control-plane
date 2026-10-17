'use strict';
// Preferences stay in this browser; metrics always come from the local ACP server.
const activityOverlay = (() => {
  const tile = document.getElementById('activity-overlay');
  const handle = document.getElementById('activity-move');
  const toggle = document.getElementById('activity-toggle');
  const restore = document.getElementById('activity-restore');
  const key = 'acp.activity-overlay.v1';
  let prefs = {collapsed:false, hidden:false}, pointer = null, inFlight = null;
  try {
    const saved = JSON.parse(localStorage.getItem(key));
    if (saved && typeof saved === 'object') prefs = {
      collapsed:saved.collapsed === true, hidden:saved.hidden === true,
      x:Number.isFinite(saved.x) ? saved.x : undefined,
      y:Number.isFinite(saved.y) ? saved.y : undefined,
    };
  } catch (_) { /* Storage may be disabled or contain an older value. */ }
  function save() { try { localStorage.setItem(key, JSON.stringify(prefs)); } catch (_) { /* Optional. */ } }
  function position(x = prefs.x, y = prefs.y) {
    if (tile.hidden) return;
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
    toggle.textContent = prefs.collapsed ? '+' : '−';
    position(); save();
  }
  toggle.onclick = () => { prefs.collapsed = !prefs.collapsed; renderPreferences(); };
  document.getElementById('activity-hide').onclick = () => {
    prefs.hidden = true; renderPreferences(); restore.focus();
  };
  restore.onclick = () => { prefs.hidden = false; renderPreferences(); toggle.focus(); refresh(); };
  handle.addEventListener('pointerdown', event => {
    if (event.button !== 0) return;
    event.preventDefault(); handle.focus();
    pointer = {id:event.pointerId, x:event.clientX, y:event.clientY, left:prefs.x, top:prefs.y};
    handle.setPointerCapture(event.pointerId); tile.classList.add('is-dragging');
  });
  handle.addEventListener('pointermove', event => {
    if (!pointer || pointer.id !== event.pointerId) return;
    position(pointer.left + event.clientX - pointer.x, pointer.top + event.clientY - pointer.y);
  });
  function endDrag() { pointer = null; tile.classList.remove('is-dragging'); save(); }
  handle.addEventListener('pointerup', endDrag);
  handle.addEventListener('pointercancel', endDrag);
  handle.addEventListener('lostpointercapture', endDrag);
  handle.addEventListener('keydown', event => {
    const step = event.shiftKey ? 4 : 16;
    const moves = {ArrowLeft:[-step,0], ArrowRight:[step,0], ArrowUp:[0,-step], ArrowDown:[0,step]};
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
      if (![data.running, data.subagents, data.waiting_on_input, ...['30m','1h','3h'].map(k => data.averages?.[k])]
        .every(n => Number.isFinite(n) && n >= 0)) throw new Error('Invalid activity');
      document.getElementById('activity-running').textContent = data.running;
      document.getElementById('activity-waiting').textContent = data.waiting_on_input;
      document.getElementById('activity-children').textContent = `Includes ${data.subagents} running subagent${data.subagents === 1 ? '' : 's'}`;
      for (const period of ['30m','1h','3h']) document.getElementById(`activity-${period}`).textContent = data.averages[period].toFixed(2);
      const average = data.averages['30m'];
      const mood = average >= 3 ? 'rainbow' : average >= 2 ? 'happy' : average >= 1 ? 'sad' : 'calm';
      tile.dataset.mood = mood;
      const moods = {rainbow:['Rainbow overdrive', 'Leo is ecstatic, surrounded by rainbows'], happy:['In a happy rhythm', 'Leo is happy and bouncing'], sad:['Could use company', 'Leo looks sad and disappointed'], calm:['Looking good, Leo', 'Leo is calm and looking good']};
      document.getElementById('activity-mood').textContent = moods[mood][0];
      document.getElementById('activity-pet').setAttribute('aria-label', moods[mood][1]);
      status.textContent = 'Live · updates every 5s';
      tile.classList.remove('is-unavailable');
    } catch (_) {
      status.textContent = 'Activity unavailable'; tile.classList.add('is-unavailable');
      for (const id of ['running','waiting','30m','1h','3h']) document.getElementById(`activity-${id}`).textContent = '—';
      document.getElementById('activity-children').textContent = 'Reconnecting to local ACP';
      document.getElementById('activity-mood').textContent = 'Taking a little breather';
      tile.dataset.mood = 'calm';
      document.getElementById('activity-pet').setAttribute('aria-label', 'Leo is resting; activity is unavailable');
    } finally { clearTimeout(timeout); }
  }
  renderPreferences(); refresh();
  setInterval(() => { if (!document.hidden && !tile.hidden) refresh(); }, 5000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden && !tile.hidden) refresh(); });
  return {refresh};
})();
