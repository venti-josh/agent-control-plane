'use strict';
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let config, tasks = [], selected = null, dragId = null, refreshTimer, toastTimer, refreshVersion = 0;
const labels = {claude:'Claude Code',codex:'Codex',demo:'Demo'};
const time = (t) => new Date(t * 1000).toLocaleString(undefined, {month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});
function toast(message) { $('toast').textContent = message; $('toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').hidden = true, 6000); }
async function api(path, method='GET', body) {
  const response = await fetch('/api' + path, {method, headers: body ? {'Content-Type':'application/json'} : {}, body: body ? JSON.stringify(body) : undefined});
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail));
  return data;
}
const attempt = (fn) => async (event) => { event?.preventDefault(); try { await fn(event); } catch (error) { toast(error.message); } };
function safeLink(url, title=url) { return /^https?:\/\//i.test(url) ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(title)}</a>` : `<span>${esc(title)}</span>`; }
function drawBoard() {
  $('task-count').textContent = `${tasks.length} task${tasks.length === 1 ? '' : 's'}`;
  const running = tasks.filter(t => t.run_status === 'running').length;
  $('running-count').textContent = running ? `${running} running` : '';
  $('board').innerHTML = config.states.map(state => {
    const cards = tasks.filter(t => t.state === state.id);
    return `<div class="lane" data-state="${state.id}" aria-label="${state.label}"><div class="lane-heading"><span class="lane-dot"></span>${state.label}<span class="lane-count">${cards.length}</span></div>${cards.map(t => `<button class="card" draggable="true" data-id="${t.id}" aria-label="Open ${esc(t.title)}"><span class="card-top"><span>${esc(t.linear_url ? 'LINEAR' : 'TASK')} ${t.id.slice(0,6).toUpperCase()}</span><span>···</span></span><span class="card-title">${esc(t.title)}</span><span class="card-desc">${esc(t.description)}</span><span class="card-bottom"><span class="provider ${t.provider}">${labels[t.provider]}</span><span class="run-status ${esc(t.run_status)}">${esc(t.run_status || 'Not started')}</span></span></button>`).join('') || '<div class="empty-lane">Drop a task here</div>'}</div>`;
  }).join('');
  document.querySelectorAll('.card').forEach(card => {
    card.onclick = attempt(() => openTask(card.dataset.id));
    card.ondragstart = event => { dragId = card.dataset.id; event.dataTransfer.setData('text/plain', dragId); event.dataTransfer.effectAllowed = 'move'; };
    card.ondragend = () => document.querySelectorAll('.lane').forEach(l => l.classList.remove('dragover'));
  });
  document.querySelectorAll('.lane').forEach(lane => {
    lane.ondragover = event => { event.preventDefault(); lane.classList.add('dragover'); };
    lane.ondragleave = () => lane.classList.remove('dragover');
    lane.ondrop = attempt(async event => { lane.classList.remove('dragover'); await moveTask(event.dataTransfer.getData('text/plain') || dragId, lane.dataset.state); });
  });
}
async function refresh() {
  const version = ++refreshVersion;
  const data = await api('/tasks');
  if (version !== refreshVersion) return;
  tasks = data; drawBoard();
  if (selected && $('task-dialog').open) {
    const task = await api(`/tasks/${selected.id}`);
    if (version === refreshVersion && selected?.id === task.id) { selected = task; drawTask(); }
  }
}
async function moveTask(id, state) {
  const task = tasks.find(t => t.id === id) || selected;
  if (!task || task.state === state) return;
  let comment;
  if (state === 'waiting_on_input') { comment = prompt('What is blocking this task? A question or comment is required.'); if (!comment?.trim()) return; }
  await api(`/tasks/${id}/transition`, 'POST', {state, comment, expected_version:task.version});
  await refresh();
}
async function openTask(id) {
  selected = await api(`/tasks/${id}`);
  $('comment-body').value = '';
  $('edit-details').open = false;
  for (const key of ['title','description','provider','repo_path']) $('edit-form').elements[key].value = selected[key] || '';
  drawTask(); $('task-dialog').showModal();
}
function drawTask() {
  const task = selected;
  $('task-short-id').textContent = 'TASK ' + task.id.slice(0,8).toUpperCase();
  $('task-heading').textContent = task.title;
  const blocker = task.comments.find(c => c.id === task.blocker_comment_id);
  $('task-summary').innerHTML = `<div class="meta"><span class="provider ${task.provider}">${labels[task.provider]}</span><span>${esc(config.states.find(s => s.id === task.state).label)}</span>${task.linear_url ? safeLink(task.linear_url,'Open in Linear ↗') : ''}${task.branch ? `<span>${esc(task.branch)}</span>` : ''}</div><p class="task-description">${esc(task.description)}</p>${blocker ? `<div class="blocker"><strong>Waiting on your input</strong><br>${esc(blocker.body)}</div>` : ''}${task.last_error ? `<p class="hint">Last run error: ${esc(task.last_error)}</p>` : ''}`;
  const laneSelect = $('task-state');
  // Live events must not overwrite a lane the user selected but hasn't applied.
  const chosenLane = laneSelect.dataset.task === task.id && laneSelect.dataset.state === task.state
    ? laneSelect.value : task.state;
  laneSelect.innerHTML = config.states.map(s => `<option value="${s.id}" ${s.id === chosenLane ? 'selected' : ''} ${s.id === 'in_progress' ? 'disabled' : ''}>${s.label}</option>`).join('');
  laneSelect.dataset.task = task.id; laneSelect.dataset.state = task.state;
  $('cancel-task').disabled = !task.runs.some(r => ['queued','running'].includes(r.status));
  $('output-section').hidden = !task.artifacts.length;
  $('task-outputs').innerHTML = task.artifacts.map(a => `<div class="output"><span class="chip">${esc(a.kind)}</span> ${a.kind === 'text' ? '' : safeLink(a.url)}<p>${esc(a.summary)}</p></div>`).join('');
  $('task-comments').innerHTML = task.comments.map(c => `<div class="comment"><div class="comment-author"><strong>${esc(c.author)}${c.in_reply_to ? ' · reply' : ''}</strong><span>${time(c.created_at)}</span></div><div class="comment-body">${esc(c.body)}</div></div>`).join('') || '<p class="hint">Plans, questions, and feedback appear here.</p>';
  $('resume-task').hidden = task.state !== 'waiting_on_input';
  $('request-changes').hidden = task.state !== 'ready_for_review';
  $('task-runs').innerHTML = `<p class="hint">Session: ${esc(task.session_id || 'Not started')}<br>Worktree: ${esc(task.workspace || 'Created on the first real run')}</p>` + task.runs.map(r => `<div class="run"><span class="run-status ${esc(r.status)}">${esc(r.status)}</span><code>${r.id.slice(0,8)}</code><span>${esc(r.trigger)}</span><a href="/api/runs/${r.id}/log" target="_blank" rel="noopener">View log ↗</a></div>`).join('');
}
async function postComment(mode) {
  if (!selected) return;
  const body = $('comment-body').value.trim();
  if (!body) throw new Error('Write a comment or answer first.');
  if (mode === 'changes') await api(`/tasks/${selected.id}/request-changes`,'POST',{feedback:body});
  else await api(`/tasks/${selected.id}/comments`,'POST',{body,...(mode === 'answer' ? {resume:true,in_reply_to:selected.blocker_comment_id} : {})});
  $('comment-body').value = ''; await refresh();
}
async function showSettings() {
  $('linear-status').textContent = config.linear_enabled ? 'Connected configuration: one workspace, one team.' : 'Not configured. Add LINEAR_* values to .env and restart. No issues are imported automatically.';
  $('linear-form').hidden = !config.linear_enabled;
  const [subs, deliveries] = await Promise.all([api('/subscriptions'),api('/deliveries?status=failed')]);
  $('subscription-list').innerHTML = subs.map(s => `<div class="subscription"><button data-unsubscribe="${s.id}">Remove</button>${esc(s.url)}<br><code>${esc(s.types.join(', '))}</code></div>`).join('') || '<p class="hint">No outgoing subscriptions.</p>';
  $('delivery-list').innerHTML = deliveries.map(d => `<div class="delivery"><button data-retry="${d.id}">Retry</button><strong>${esc(d.kind)}</strong> · ${d.attempts} attempts<br>${esc(d.last_error)}</div>`).join('') || '<p class="hint">No failed deliveries.</p>';
  document.querySelectorAll('[data-unsubscribe]').forEach(b => b.onclick = attempt(async () => { await api(`/subscriptions/${b.dataset.unsubscribe}`,'DELETE'); await showSettings(); }));
  document.querySelectorAll('[data-retry]').forEach(b => b.onclick = attempt(async () => { await api(`/deliveries/${b.dataset.retry}/retry`,'POST'); await showSettings(); }));
  if (!$('settings-dialog').open) $('settings-dialog').showModal();
}
async function health() {
  try {
    const data = await api('/health');
    const problems = [data.failed_deliveries ? `${data.failed_deliveries} failed integration/event deliveries — click to inspect` : '', data.worker_error ? 'Worker reported an error; check server logs.' : '', !data.worker_running ? 'The task worker is not running.' : ''].filter(Boolean);
    $('health-warning').hidden = !problems.length;
    $('health-warning').textContent = problems.join(' · ');
  } catch (_) { /* EventSource shows connection status. */ }
}
async function init() {
  config = await api('/config');
  $('default-provider').textContent = 'Default: ' + labels[config.default_provider];
  $('create-form').elements.provider.value = config.default_provider;
  $('create-form').elements.repo_path.value = config.default_repo;
  document.querySelectorAll('[data-close]').forEach(b => b.onclick = () => $(b.dataset.close).close());
  $('task-dialog').addEventListener('close', () => selected = null);
  $('new-task').onclick = () => $('create-dialog').showModal();
  $('create-form').onsubmit = attempt(async () => {
    const data = Object.fromEntries(new FormData($('create-form')));
    await api('/tasks','POST',data); $('create-dialog').close(); $('create-form').reset();
    $('create-form').elements.provider.value = config.default_provider; $('create-form').elements.repo_path.value = config.default_repo;
    await refresh();
  });
  $('edit-form').onsubmit = attempt(async () => {
    const form = Object.fromEntries(new FormData($('edit-form'))), changed = {expected_version:selected.version};
    for (const key of Object.keys(form)) if (form[key] !== selected[key]) changed[key] = form[key];
    await api(`/tasks/${selected.id}`,'PATCH',changed); $('edit-details').open = false; await refresh();
  });
  $('move-task').onclick = attempt(() => moveTask(selected.id, $('task-state').value));
  $('cancel-task').onclick = attempt(async () => { await api(`/tasks/${selected.id}/cancel`,'POST'); await refresh(); });
  $('comment-form').onsubmit = attempt(() => postComment('comment'));
  $('resume-task').onclick = attempt(() => postComment('answer'));
  $('request-changes').onclick = attempt(() => postComment('changes'));
  $('reset-session').onclick = attempt(async () => { await api(`/tasks/${selected.id}/reset-session`,'POST'); await refresh(); });
  $('settings-btn').onclick = attempt(showSettings);
  $('health-warning').onclick = attempt(showSettings);
  $('subscription-form').onsubmit = attempt(async () => {
    const data = Object.fromEntries(new FormData($('subscription-form'))); data.types = data.types.split(',').map(t => t.trim()).filter(Boolean);
    await api('/subscriptions','POST',data); $('subscription-form').reset(); await showSettings();
  });
  $('linear-form').onsubmit = attempt(async () => {
    const data = Object.fromEntries(new FormData($('linear-form'))); if (!data.repo_path) delete data.repo_path;
    await api('/integrations/linear/import','POST',data); $('settings-dialog').close(); await refresh();
  });
  $('reconcile-linear').onclick = attempt(async () => { const result = await api('/integrations/linear/reconcile','POST'); toast(`Queued ${result.queued} linked issue checks.`); });
  $('demo-btn').onclick = attempt(async () => {
    $('demo-btn').disabled = true;
    try {
      for (const item of [{title:'Clarify the acceptance criteria',description:'[ask] Demo: the agent pauses with a question. Reply to resume.',state:'todo'}, {title:'Review an agent’s output',description:'Demo: request changes to run the same task again.',state:'todo'}, {title:'Plan your first real task',description:'Choose a repository and Claude or Codex, then move this task into Todo.',state:'planning'}]) await api('/tasks','POST',{...item,provider:'demo'});
      await refresh();
    } finally { $('demo-btn').disabled = false; }
  });
  await refresh(); await health();
  const stream = new EventSource('/api/events/stream');
  stream.onopen = () => { $('connection').textContent = 'Live'; $('connection').classList.add('live'); };
  stream.onerror = () => { $('connection').textContent = 'Reconnecting'; $('connection').classList.remove('live'); };
  stream.addEventListener('board', () => { clearTimeout(refreshTimer); refreshTimer = setTimeout(() => refresh().catch(e => toast(e.message)),150); });
  setInterval(health,5000);
}
init().catch(error => toast(error.message));
