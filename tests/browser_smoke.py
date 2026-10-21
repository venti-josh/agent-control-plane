"""Optional real-browser smoke test (not collected by pytest).

Install playwright + Chromium, then:
  CHROMIUM_EXECUTABLE=/usr/bin/chromium python tests/browser_smoke.py
No model or external integration is called. All data is in a temporary directory.
"""
import os
import socket
import tempfile
import threading
import time
from pathlib import Path

import uvicorn
import httpx
from playwright.sync_api import sync_playwright, expect

from acp.api import create_app
from acp.config import Settings


def main():
    with tempfile.TemporaryDirectory() as tmp:
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        url = f'http://127.0.0.1:{sock.getsockname()[1]}'
        settings = Settings(data_dir=Path(tmp), base_url=url, poll_seconds=.03)
        server = uvicorn.Server(uvicorn.Config(create_app(settings), log_level='error', ws='none'))
        thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
        thread.start()
        for _ in range(150):
            if server.started:
                break
            time.sleep(.02)
        assert server.started
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, executable_path=os.getenv('CHROMIUM_EXECUTABLE'))
                page = browser.new_page(viewport={'width':1600,'height':1000}, device_scale_factor=1)
                errors=[]
                page.on('pageerror', lambda e: errors.append(str(e)))
                if os.getenv('ACP_BROWSER_BRIDGE') == '1':
                    # Offline DOM test mode for environments that block browser networking.
                    # Calls still reach the real local backend; only browser HTTP/SSE
                    # transport is replaced. This mode does NOT validate native EventSource.
                    def bridge(_source, path, method='GET', body=None):
                        response = httpx.request(method, url + path, json=body, timeout=10, trust_env=False)
                        return {'status': response.status_code, 'body': response.json()}
                    page.expose_binding('acpTestRequest', bridge)
                    page.evaluate("""() => {
                        window.fetch = async (path, options={}) => {
                            const result = await window.acpTestRequest(path, options.method || 'GET', options.body ? JSON.parse(options.body) : null);
                            return {ok: result.status >= 200 && result.status < 300, status: result.status, json: async () => result.body};
                        };
                        window.EventSource = class extends EventTarget {
                            constructor() {
                                super(); this.cursor=0; setTimeout(() => this.onopen?.(), 20);
                                this.timer=setInterval(async () => {
                                    const result=await window.acpTestRequest('/api/events?after='+this.cursor);
                                    for (const event of result.body) {
                                        this.cursor=event.id;
                                        this.dispatchEvent(new MessageEvent('board', {data: JSON.stringify(event)}));
                                    }
                                }, 100);
                            }
                            close() {clearInterval(this.timer);}
                        };
                    }""")
                    static=Path(__file__).parents[1] / 'acp' / 'static'
                    html=(static/'index.html').read_text().replace('<script src="/static/app.js" defer></script>', '')
                    html=html.replace('<link rel="stylesheet" href="/static/style.css">', '<style>'+(static/'style.css').read_text()+'</style>')
                    page.set_content(html)
                    page.add_script_tag(content=(static/'app.js').read_text())
                else:
                    page.goto(url)
                expect(page.locator('.lane')).to_have_count(7)
                expect(page.locator('#connection')).to_have_text('Live')
                page.get_by_role('button', name='Try a demo').click()
                waiting = page.locator('[data-state="waiting_on_input"] .card')
                review = page.locator('[data-state="ready_for_review"] .card')
                expect(waiting).to_have_count(1, timeout=10000)
                expect(review).to_have_count(1, timeout=10000)
                page.screenshot(path=os.getenv('ACP_SCREENSHOT', str(Path(tmp)/'board.png')), full_page=True)
                # Close a blocked task through the actual dialog without restarting it.
                svc = server.config.app.state.service
                blocked = svc.create_task('Blocked completion')
                with svc.store.transaction() as db:
                    svc._move(db, svc._task(db, blocked['id']), 'todo', 'api')
                    svc._move(db, svc._task(db, blocked['id']), 'waiting_on_input', 'api', comment='No further work needed')
                page.get_by_role('button', name='Open Blocked completion', exact=True).click()
                page.locator('#task-state').select_option('done')
                page.get_by_role('button', name='Move', exact=True).click()
                expect(page.locator('#resume-task')).to_be_hidden()
                assert svc.get_task(blocked['id'])['state'] == 'done'
                page.locator('[data-close="task-dialog"]').click()
                # A live refresh must not retarget an answer already drafted for an older blocker.
                draft = svc.create_task('Draft blocker scope')
                with svc.store.transaction() as db:
                    svc._move(db, svc._task(db, draft['id']), 'todo', 'api')
                    svc._move(db, svc._task(db, draft['id']), 'waiting_on_input', 'api', comment='Approve A?')
                page.get_by_role('button', name='Open Draft blocker scope', exact=True).click()
                page.locator('#comment-body').fill('Approve A only')
                old_blocker = svc.get_task(draft['id'])['blocker_comment_id']
                with svc.store.transaction() as db:
                    new_blocker = svc._comment(db, draft['id'], 'Approve B?', 'agent', 'agent')['id']
                    db.execute('UPDATE tasks SET blocker_comment_id=? WHERE id=?', (new_blocker, draft['id']))
                page.evaluate('refresh()')
                page.locator('#resume-task').click()
                expect(page.locator('#toast')).to_contain_text('Saved as context only')
                expect(page.locator('#task-comments')).to_contain_text('Approve A only')
                assert svc.get_task(draft['id'])['comments'][-1]['in_reply_to'] == old_blocker
                assert not svc.store.one("SELECT 1 FROM runs WHERE task_id=? AND status IN ('queued','running')", (draft['id'],))
                page.locator('[data-close="task-dialog"]').click()
                waiting = page.locator('[data-state="waiting_on_input"] .card').filter(has_text='Clarify the acceptance criteria')
                waiting.click()
                expect(page.locator('#resume-task')).to_be_visible()
                page.locator('#comment-body').fill('Acceptance criterion: a plain text report.')
                page.locator('#resume-task').click()
                expect(page.locator('#request-changes')).to_be_visible(timeout=10000)
                page.locator('#comment-body').fill('Add a short verification section.')
                page.locator('#request-changes').click()
                expect(page.locator('#task-comments')).to_contain_text('Add a short verification section.')
                page.wait_for_function('document.querySelectorAll("#task-runs .run").length === 3')
                expect(page.locator('#task-runs')).to_contain_text('succeeded', timeout=10000)
                page.locator('[data-close="task-dialog"]').click()
                page.get_by_role('button', name='+ New task').click()
                page.locator('#create-form [name="title"]').fill('Browser-created task')
                page.locator('#create-form [name="description"]').fill('<script>noHTML()</script>')
                if os.getenv('ACP_BROWSER_BRIDGE') != '1':
                    lost_responses = []
                    def lose_create_response(route):
                        if route.request.method != 'POST':
                            route.continue_(); return
                        response = route.fetch()
                        lost_responses.append(response.json()['id'])
                        if len(lost_responses) == 1:
                            route.abort('failed')
                        else:
                            route.fulfill(response=response)
                    page.route('**/api/tasks', lose_create_response)
                page.get_by_role('button', name='Create task', exact=True).click()
                expect(page.get_by_role('button', name='Open Browser-created task')).to_be_visible()
                expect(page.locator('#create-dialog')).not_to_be_visible()
                if os.getenv('ACP_BROWSER_BRIDGE') != '1':
                    assert len(lost_responses) == 2 and len(set(lost_responses)) == 1
                    assert svc.store.one("SELECT count(*) n FROM tasks WHERE title='Browser-created task'")['n'] == 1
                    page.unroute('**/api/tasks', lose_create_response)
                    # A downgrade after a committed write must never cause an unkeyed replay.
                    downgraded_writes = []
                    def downgrade_after_commit(route):
                        if route.request.method != 'POST':
                            route.continue_(); return
                        response = route.fetch()
                        downgraded_writes.append(response.json()['id'])
                        route.abort('failed')
                    def capability(route):
                        route.fulfill(json={'capabilities': [] if downgraded_writes else ['command-receipts']})
                    page.route('**/api/tasks', downgrade_after_commit)
                    page.route('**/api/health', capability)
                    result = page.evaluate("""async () => {
                        try { await api('/tasks', 'POST', {title:'Downgrade boundary'}); }
                        catch (e) { return e.message; }
                    }""")
                    assert 'safely be retried' in result
                    # A manual retry retains the key and also refuses the old server.
                    result = page.evaluate("""async () => {
                        try { await api('/tasks', 'POST', {title:'Downgrade boundary'}); }
                        catch (e) { return e.message; }
                    }""")
                    assert 'safely be retried' in result
                    assert len(downgraded_writes) == 1
                    result = page.evaluate("""async () => {
                        try { await api('/tasks', 'POST', {title:'Old server first write'}); }
                        catch (e) { return e.message; }
                    }""")
                    assert 'Upgrade the server first' in result
                    assert len(downgraded_writes) == 1
                    page.unroute('**/api/tasks', downgrade_after_commit)
                    page.unroute('**/api/health', capability)

                page.get_by_role('button', name='Open Browser-created task').click()
                expect(page.locator('.task-description')).to_have_text('<script>noHTML()</script>')
                page.locator('#task-state').select_option('todo')
                page.wait_for_timeout(300)  # A live refresh must preserve this unsaved selection.
                expect(page.locator('#task-state')).to_have_value('todo')
                page.get_by_role('button', name='Move', exact=True).click()
                expect(page.locator('#request-changes')).to_be_visible(timeout=10000)
                page.locator('[data-close="task-dialog"]').click()
                page.locator('#settings-btn').click()
                page.locator('#settings-advanced > summary').click()
                page.get_by_text('Event subscriptions', exact=True).click()
                page.locator('#subscription-form [name="url"]').fill('http://127.0.0.1:9000/events')
                page.get_by_role('button', name='Subscribe', exact=True).click()
                expect(page.locator('#subscription-list')).to_contain_text('http://127.0.0.1:9000/events')
                page.locator('#subscription-list button').click()
                expect(page.locator('#subscription-list')).to_contain_text('No outgoing subscriptions.')
                page.locator('[data-close="settings-dialog"]').click()
                # A Codex approval parked on the board, answered with its button (synthetic live run).
                from uuid import uuid4
                app_task = svc.create_task('Needs approval', provider='codex')
                job = str(uuid4())
                with svc.store.transaction() as db:
                    db.execute("UPDATE tasks SET state='todo' WHERE id=?", (app_task['id'],))
                    db.execute("INSERT INTO runs(id,task_id,status,trigger,inputs,created_at) VALUES(?,?,'running','todo','[]',?)",
                               (job, app_task['id'], time.time()))
                svc.mark_started(job)
                svc.request_approval(job, {'id': 0, 'method': 'item/commandExecution/requestApproval',
                                           'params': {'command': 'curl https://example.com'}},
                                     'Codex asks to run a command:\n\n```\ncurl https://example.com\n```')
                page.evaluate('refresh()')
                page.get_by_role('button', name='Open Needs approval', exact=True).click()
                expect(page.locator('#task-summary')).to_contain_text('Codex needs your approval')
                screenshot = os.getenv('ACP_APPROVAL_SCREENSHOT')
                if screenshot:
                    page.screenshot(path=screenshot, full_page=True)
                page.get_by_role('button', name='Accept for session', exact=True).click()
                expect(page.locator('#task-summary')).not_to_contain_text('Codex needs your approval')
                assert [a['decision'] for a in svc.answered_approvals(job)] == ['acceptForSession']
                assert svc.get_task(app_task['id'])['state'] == 'in_progress'
                svc.cancel(app_task['id'])
                svc.finish_run(job)
                page.locator('[data-close="task-dialog"]').click()
                # Reconciliation changes current task state, not historical run outcomes.
                for lane, label, outcome in (
                    ('done', 'Done', 'failed'),
                    ('ready_for_review', 'Ready for Review', 'failed'),
                    ('waiting_on_input', 'Waiting on Input', 'failed'),
                    ('waiting_on_input', 'Waiting on Input', 'succeeded'),
                    ('deploy', 'Deploy', 'succeeded'),
                    ('planning', 'Planning', 'cancelled'),
                ):
                    item = svc.create_task(f'Status regression {lane} {outcome}')
                    run_id = str(uuid4())
                    with svc.store.transaction() as db:
                        db.execute('UPDATE tasks SET state=? WHERE id=?', (lane, item['id']))
                        db.execute("INSERT INTO runs(id,task_id,status,trigger,inputs,created_at,finished_at,error) VALUES(?,?,?,'todo','[]',?,?,?)",
                                   (run_id, item['id'], outcome, time.time()-120, time.time()-60,
                                    'Historical run timeout' if outcome == 'failed' else None))
                    page.evaluate('refresh()')
                    card = page.get_by_role('button', name='Open ' + item['title'], exact=True)
                    expect(card.locator('.card-bottom')).to_contain_text(label)
                    expect(card.locator('.run-status')).to_have_count(0)
                    card.click()
                    history = page.locator('details').filter(has=page.locator('#task-runs'))
                    if history.get_attribute('open') is None:
                        history.locator('summary').click()
                    expect(history.locator('summary')).to_have_text('Run history & session')
                    expect(page.locator('#task-runs .run')).to_contain_text('Past run: ' + outcome)
                    expect(page.locator('#task-runs')).to_contain_text('Finished:')
                    if outcome == 'failed':
                        expect(page.locator('#task-runs')).to_contain_text('Run error: Historical run timeout')
                    assert svc.get_task(item['id'])['runs'][0]['status'] == outcome
                    page.locator('[data-close="task-dialog"]').click()
                page.set_viewport_size({'width':390,'height':844})
                expect(page.locator('.lane')).to_have_count(7)
                assert not errors, errors
                browser.close()
                print('Browser smoke passed (transport=' + ('test bridge' if os.getenv('ACP_BROWSER_BRIDGE') == '1' else 'native') + '): 7 lanes, live events, input/review loop, create/move, escaped text, subscriptions, Codex approval buttons, mobile viewport.')
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            sock.close()


if __name__ == '__main__':
    main()
