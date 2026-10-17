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
                page.get_by_role('button', name='Create task', exact=True).click()
                expect(page.get_by_role('button', name='Open Browser-created task')).to_be_visible()
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
                # Synthetic desktop transport, real API and UI: delivery is not execution.
                from uuid import uuid4
                from acp.desktop import DesktopBridge
                dispatcher = DesktopBridge(svc)
                dispatcher.connect(str(uuid4()))
                fake_codex = Path(tmp) / 'codex'
                fake_codex.write_text('#!/bin/sh\nprintf "Queued browser test\\n"\n')
                fake_codex.chmod(0o755)
                svc.settings.codex_command = str(fake_codex)
                app_task = svc.create_task('Delayed dispatcher', provider='codex', state='todo')
                page.get_by_role('button', name='Open Delayed dispatcher', exact=True).click()
                expect(page.locator('#task-runs')).to_contain_text('Awaiting dispatcher')
                expect(page.locator('#running-count')).to_have_text('')
                page.get_by_text('Runs & session', exact=True).click()
                page.get_by_role('button', name='Retry delivery', exact=True).click()
                expect(page.locator('#toast')).to_contain_text('same run')
                observed = svc.get_task(app_task['id'])
                assert len(observed['runs']) == 1
                job = observed['runs'][0]
                assert job['desktop']['recovery_at'] and job['started_at'] is None
                screenshot = os.getenv('ACP_RECOVERY_SCREENSHOT')
                if screenshot:
                    page.screenshot(path=screenshot, full_page=True)
                dispatcher.claim(job['id'])
                expect(page.locator('#task-runs')).to_contain_text('Awaiting task start')
                expect(page.get_by_role('button', name='Retry delivery', exact=True)).to_have_count(0)
                session = str(uuid4())
                dispatcher.started(job['id'], session)
                expect(page.locator('#running-count')).to_have_text('1 running')
                dispatcher.action(job['id'], session, 'review', text='Synthetic browser check complete')
                dispatcher.complete(job['id'], session)
                page.locator('[data-close="task-dialog"]').click()
                page.set_viewport_size({'width':390,'height':844})
                expect(page.locator('.lane')).to_have_count(7)
                assert not errors, errors
                browser.close()
                print('Browser smoke passed (transport=' + ('test bridge' if os.getenv('ACP_BROWSER_BRIDGE') == '1' else 'native') + '): 7 lanes, live events, input/review loop, create/move, escaped text, subscriptions, dispatcher delay/recovery/start, mobile viewport.')
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            sock.close()


if __name__ == '__main__':
    main()
