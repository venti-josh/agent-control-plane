'use strict';
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let config, tasks = [], selected = null, dragId = null, refreshTimer, toastTimer, refreshVersion = 0;
const labels = {claude:'Claude Code',codex:'Codex',demo:'Demo'};
const time = (t) => new Date(t * 1000).toLocaleString(undefined, {month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});
function toast(message) { $('toast').textContent = message; $('toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').hidden = true, 6000); }
const pendingCommands = new Map();
async function api(path, method='GET', body) {
  const protectedWrite = method === 'POST' && (path === '/tasks' ||
    /^\/tasks\/[^/]+\/(comments|request-input|submit-for-review|request-changes)$/.test(path));
  const storageKey = 'acp-command:' + JSON.stringify([path, body]);
  let key;
  if (protectedWrite) {
    try { key = sessionStorage.getItem(storageKey); } catch (_) { /* unavailable in embedded surfaces */ }
    key = key || pendingCommands.get(storageKey);
    key = key || crypto.randomUUID();
    pendingCommands.set(storageKey, key);
    try { sessionStorage.setItem(storageKey, key); } catch (_) { /* retain in memory */ }
  }
  const clearKey = () => {
    pendingCommands.delete(storageKey);
    try { sessionStorage.removeItem(storageKey); } catch (_) { /* unavailable */ }
  };
  for (let attempt = 0; attempt < 3; attempt++) {
    let supported = false;
    if (protectedWrite) {
      const health = await fetch('/api/health').then(r => r.json());
      supported = health.capabilities?.includes('command-receipts');
      if (!supported) throw new Error('ACP must support command receipts before this request can safely be retried. Upgrade the server first.');
    }
    let response, data;
    try {
      response = await fetch('/api' + path, {method,
        headers: {...(body ? {'Content-Type':'application/json'} : {}), ...(supported ? {'Idempotency-Key':key} : {})},
        body: body ? JSON.stringify(body) : undefined});
      data = await response.json();
    } catch (error) {
      if (!supported || attempt === 2) throw error;
      await new Promise(resolve => setTimeout(resolve, 500 * 2 ** attempt));
      continue;
    }
    if (!response.ok) {
      const retryAfter = response.headers?.get('Retry-After');
      const retrySeconds = retryAfter == null ? 0 : (Number.isFinite(Number(retryAfter)) ? Number(retryAfter) : (Date.parse(retryAfter) - Date.now()) / 1000);
      if (supported && attempt < 2 && [408,425,429,500,502,503,504].includes(response.status) && !(retrySeconds > 5)) {
        await new Promise(resolve => setTimeout(resolve, Math.max(500 * 2 ** attempt, retrySeconds * 1000 || 0)));
        continue;
      }
      if (response.status < 500 && ![408,425,429].includes(response.status)) clearKey();
      throw new Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail));
    }
    clearKey();
    return data;
  }
}
const attempt = (fn) => async (event) => {
  event?.preventDefault();
  const button = event?.submitter || (event?.currentTarget?.tagName === 'BUTTON' ? event.currentTarget : null);
  if (button?.disabled) return;
  if (button) button.disabled = true;
  try { await fn(event); } catch (error) { toast(error.message); }
  finally { if (button) button.disabled = false; }
};
function safeLink(url, title=url) { return /^https?:\/\//i.test(url) ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(title)}</a>` : `<span>${esc(title)}</span>`; }
function runLabel(status) { return status || 'Not started'; }
const decisionLabels = {accept: 'Accept', 'accept for session': 'Accept for session', decline: 'Decline'};
function drawBoard() {
  $('task-count').textContent = `${tasks.length} task${tasks.length === 1 ? '' : 's'}`;
  const running = tasks.filter(t => t.run_status === 'running' && t.run_started_at).length;
  $('running-count').textContent = running ? `${running} running` : '';
  $('board').innerHTML = config.states.map(state => {
    const cards = tasks.filter(t => t.state === state.id);
    return `<div class="lane" data-state="${state.id}" aria-label="${state.label}"><div class="lane-heading"><span class="lane-dot"></span>${state.label}<span class="lane-count">${cards.length}</span></div>${cards.map(t => `<button class="card" draggable="true" data-id="${t.id}" aria-label="Open ${esc(t.title)}"><span class="card-top"><span>${esc(t.linear_url ? 'LINEAR' : 'TASK')} ${t.id.slice(0,6).toUpperCase()}</span><span>···</span></span><span class="card-title">${esc(t.title)}</span><span class="card-desc">${esc(t.description)}</span><span class="card-bottom"><span class="provider ${t.provider}">${labels[t.provider]}</span><span class="task-status ${esc(t.state)}">${esc(state.label)}</span></span></button>`).join('') || '<div class="empty-lane">Drop a task here</div>'}</div>`;
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
    if (version === refreshVersion && selected?.id === task.id) {
      if (task.hidden) { $('task-dialog').close(); toast('The issue left ACP. Assign it to the board owner and add its acp label in Linear to restore it.'); }
      else { selected = task; drawTask(); }
    }
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
  $('comment-body').value = ''; delete $('comment-body').dataset.blocker;
  $('edit-details').open = false;
  for (const key of ['title','description','provider','repo_path']) $('edit-form').elements[key].value = selected[key] || '';
  drawTask(); $('task-dialog').showModal();
}
function drawTask() {
  const task = selected;
  $('task-short-id').textContent = 'TASK ' + task.id.slice(0,8).toUpperCase();
  $('task-heading').textContent = task.title;
  const blocker = task.comments.find(c => c.id === task.blocker_comment_id);
  $('task-summary').innerHTML = `<div class="meta"><span class="provider ${task.provider}">${labels[task.provider]}</span><span>${esc(config.states.find(s => s.id === task.state).label)}</span>${task.linear_url ? safeLink(task.linear_url,'Open in Linear ↗') : ''}${task.branch ? `<span>${esc(task.branch)}</span>` : ''}</div><p class="task-description">${esc(task.description)}</p>${blocker ? `<div class="blocker"><strong>${task.approval ? 'Codex needs your approval' : 'Waiting on your input'}</strong><br>${esc(blocker.body)}${task.approval ? `<div class="comment-actions">${task.approval.decisions.map(d => `<button type="button" data-decision="${esc(d)}"${d === 'accept' ? ' class="primary"' : ''}>${esc(decisionLabels[d] || d)}</button>`).join('')}</div>` : ''}</div>` : ''}${task.last_error ? `<p class="hint">Last run error: ${esc(task.last_error)}</p>` : ''}`;
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
  $('task-runs').innerHTML = `<p class="hint">Session: ${esc(task.session_id || 'Not started')}<br>Workspace: ${esc(task.workspace || 'Managed by the agent')}</p>` + task.runs.map(r => {
    const current = ['queued','running'].includes(r.status);
    const recorded = r.finished_at ? `Finished: ${time(r.finished_at)}` : `Queued: ${time(r.created_at)}`;
    return `<div class="run"><span class="run-status ${esc(r.status)}">${current ? 'Current run' : 'Past run'}: ${esc(runLabel(r.status))}</span><code>${r.id.slice(0,8)}</code><span>${esc(r.trigger)}</span><a href="/api/runs/${r.id}/log" target="_blank" rel="noopener">View log ↗</a></div><p class="hint">${esc(recorded)}</p>${r.error ? `<p class="hint">Run error: ${esc(r.error)}</p>` : ''}`;
  }).join('');
  document.querySelectorAll('[data-decision]').forEach(button => {
    button.onclick = attempt(async () => {
      await api(`/tasks/${task.id}/comments`, 'POST', {body: button.dataset.decision, resume: true, in_reply_to: task.blocker_comment_id});
      await refresh();
    });
  });
}
async function postComment(mode) {
  if (!selected) return;
  const body = $('comment-body').value.trim();
  if (!body) throw new Error('Write a comment or answer first.');
  if (mode === 'changes') await api(`/tasks/${selected.id}/request-changes`,'POST',{feedback:body});
  else {
    const blocker = $('comment-body').dataset.blocker;
    if (mode === 'answer' && !blocker) throw new Error('This draft was not started for the current question. Clear it and answer the question shown.');
    const result = await api(`/tasks/${selected.id}/comments`,'POST',{body,...(mode === 'answer' ? {resume:true,in_reply_to:blocker} : {})});
    if (result.resume_queued === false) toast(result.notice);
  }
  $('comment-body').value = ''; delete $('comment-body').dataset.blocker; await refresh();
}
let linearDiscovery, linearStatus, linearEditing = false;
async function updateLinearStatus() {
  const state = await api('/integrations/linear/status');
  config.linear_enabled = state.configured;
  $('board-owner').textContent = state.user_name ? `${state.user_name}’s board` : '';
  $('board-owner').hidden = !state.user_name;
  linearStatus = state;
  $('linear-badge').textContent = state.configured ? 'Connected' : 'Not connected';
  $('linear-status').textContent = state.configured ? `${state.user_name || 'Your Linear user'} · ${state.team_name || state.team_id} · ${labels[state.provider]}` : 'Connect Linear to bring your issues onto the board.';
  $('linear-error').textContent = state.sync_error || (state.failed_deliveries ? `${state.failed_deliveries} failed deliveries. Open Advanced → Delivery diagnostics.` : '');
  $('linear-error').hidden = !$('linear-error').textContent;
  $('linear-connected').hidden = !state.configured || linearEditing;
  $('linear-connect-form').hidden = (state.configured && !linearEditing) || !$('linear-setup-form').hidden;
  $('linear-webhook-details').hidden = !state.configured;
  $('linear-import-details').hidden = !state.configured;
  $('linear-webhook-status').textContent = `${state.webhook_registered ? 'Webhook registered. ' : ''}${state.last_webhook_at ? `Last signed delivery: ${time(state.last_webhook_at)}.` : 'No signed delivery received yet.'}`;
  if (!$('linear-webhook-url').value) $('linear-webhook-url').value = state.webhook_url;
}
function updateMappingSummary() {
  const missing = config.states.filter(lane => !document.querySelector(`[data-linear-lane="${lane.id}"]`).value);
  $('linear-mapping-summary').textContent = missing.length ? 'Some board lanes need a new Linear status.' : 'Your Linear statuses are matched automatically.';
  $('linear-create-label').hidden = !missing.length;
  $('linear-create-description').textContent = `Create ${missing.map(lane => lane.label).join(', ')} in Linear`;
}
function drawLinearMapping() {
  const team = linearDiscovery.teams.find(t => t.id === $('linear-team').value);
  $('linear-mapping').innerHTML = team ? config.states.map(lane => `<label>${esc(lane.label)}<select data-linear-lane="${lane.id}"><option value="">Create a status</option>${team.states.map(s => `<option value="${esc(s.id)}" ${team.suggested_states[lane.id] === s.id ? 'selected' : ''}>${esc(s.name)}</option>`).join('')}</select></label>`).join('') : '';
  if (team) updateMappingSummary();
}
async function resetLinearEditor() {
  linearEditing = false; linearDiscovery = null;
  $('linear-key').value = ''; $('linear-setup-form').hidden = true;
  $('linear-workflow-details').open = false;
  await updateLinearStatus();
}

async function linearBusy(id, work) {
  $(id).disabled = true;
  try { await work(); } finally { $(id).disabled = false; }
}
async function showSettings() {
  await updateLinearStatus();
  const [subs, deliveries] = await Promise.all([api('/subscriptions'),api('/deliveries?status=failed')]);
  $('subscription-list').innerHTML = subs.map(s => `<div class="subscription"><button data-unsubscribe="${s.id}">Remove</button>${esc(s.url)}<br><code>${esc(s.types.join(', '))}</code></div>`).join('') || '<p class="hint">No outgoing subscriptions.</p>';
  $('delivery-list').innerHTML = deliveries.map(d => `<div class="delivery"><button data-retry="${d.id}">Retry</button><strong>${esc(d.kind)}</strong> · ${d.attempts} attempts<br>${esc(d.last_error)}</div>`).join('') || '<p class="hint">No failed deliveries.</p>';
  document.querySelectorAll('[data-unsubscribe]').forEach(b => b.onclick = attempt(async () => { await api(`/subscriptions/${b.dataset.unsubscribe}`,'DELETE'); await showSettings(); }));
  document.querySelectorAll('[data-retry]').forEach(b => b.onclick = attempt(async () => { await api(`/deliveries/${b.dataset.retry}/retry`,'POST'); await showSettings(); }));
  $('delivery-summary').textContent = deliveries.length ? `Delivery diagnostics (${deliveries.length} failed)` : 'Delivery diagnostics';
  if (!$('settings-dialog').open) { $('settings-advanced').open = false; $('settings-dialog').showModal(); }
}
async function health() {
  try {
    const data = await api('/health');
    $('runtime-version').textContent = `ACP ${data.version}${data.build_revision ? ' · ' + data.build_revision.slice(0, 8) : ''}`;
    const problems = [data.failed_deliveries ? `${data.failed_deliveries} failed integration/event deliveries — click to inspect` : '', data.worker_error ? 'Worker reported an error; check server logs.' : '', !data.worker_running ? 'The task worker is not running.' : ''].filter(Boolean);
    $('health-warning').hidden = !problems.length;
    $('health-warning').textContent = problems.join(' · ');
  } catch (_) { /* EventSource shows connection status. */ }
}
async function init() {
  config = await api('/config');
  $('board-owner').textContent = config.linear_user_name ? `${config.linear_user_name}’s board` : '';
  $('board-owner').hidden = !config.linear_user_name;
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
  $('comment-body').oninput = () => {
    const draft = $('comment-body');
    if (!draft.value.trim()) delete draft.dataset.blocker;
    else if (!('blocker' in draft.dataset)) draft.dataset.blocker = selected?.blocker_comment_id || '';
  };
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
  $('linear-connect-form').onsubmit = attempt(() => linearBusy('linear-discover', async () => {
    linearDiscovery = await api('/integrations/linear/discover','POST',{api_key:$('linear-key').value});
    linearEditing = true;
    $('linear-owner').textContent = `Connected as ${linearDiscovery.user.name}`;
    $('linear-team').innerHTML = linearDiscovery.teams.map(t => `<option value="${esc(t.id)}">${esc(t.name)} (${esc(t.key)})</option>`).join('');
    if (linearDiscovery.teams.some(t => t.id === linearStatus.team_id)) $('linear-team').value = linearStatus.team_id;
    $('linear-provider').value = linearStatus.provider || 'codex';
    $('linear-setup-form').hidden = !linearDiscovery.teams.length;
    $('linear-connect-form').hidden = !!linearDiscovery.teams.length;
    $('linear-save').textContent = linearStatus.configured ? 'Save changes' : 'Connect Linear';
    drawLinearMapping();
    if (!linearDiscovery.teams.length) toast('No teams are accessible with this API key.');
  }));
  $('linear-team').onchange = drawLinearMapping;
  $('linear-mapping').onchange = updateMappingSummary;
  $('linear-edit').onclick = attempt(async () => { linearEditing = true; document.querySelector('#linear-connect-form .linear-cancel').hidden = false; await updateLinearStatus(); });
  document.querySelectorAll('.linear-cancel').forEach(button => button.onclick = attempt(resetLinearEditor));
  $('linear-setup-form').onsubmit = attempt(() => linearBusy('linear-save', async () => {
    const states = Object.fromEntries([...document.querySelectorAll('[data-linear-lane]')].map(s => [s.dataset.linearLane,s.value]));
    await api('/integrations/linear/configure','POST',{api_key:$('linear-key').value,team_id:$('linear-team').value,states,create_missing:$('linear-create-missing').checked,provider:$('linear-provider').value});
    await resetLinearEditor(); toast('Linear connected. Your assigned acp issues will sync automatically.');
  }));
  $('linear-webhook-form').onsubmit = attempt(() => linearBusy('linear-register', async () => {
    await api('/integrations/linear/webhook','POST',{url:$('linear-webhook-url').value.trim()});
    await updateLinearStatus(); toast('Webhook registered. Change an imported issue in Linear to verify delivery.');
  }));
  $('settings-dialog').addEventListener('close', () => { $('linear-key').value = ''; linearEditing = false; linearDiscovery = null; $('linear-setup-form').hidden = true; $('linear-workflow-details').open = false; });
  $('reconcile-linear').onclick = attempt(async () => { const result = await api('/integrations/linear/reconcile','POST'); toast('Queued a scan for issues with the acp label.'); });
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
  setInterval(() => { if ($('settings-dialog').open) updateLinearStatus().catch(() => {}); },5000);
}
init().catch(error => toast(error.message));
