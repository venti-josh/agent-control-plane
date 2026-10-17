"""Real AppKit smoke, isolated service and synthetic agents. No model calls.

Run on macOS: uv run --locked python tests/mac_overlay.py
ACP_OVERLAY_UI=1 keeps it up for native dragging/menu/screenshot verification.
"""
import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path

import uvicorn
from fastapi.responses import HTMLResponse, JSONResponse

from mac_surface import check_surface

from acp import overlay
from acp.codex_activity import CodexActivity, process_info
from acp.api import create_app
from acp.config import Settings


def wait_state(settings, predicate):
    for _ in range(150):
        result = overlay.status(settings)
        if predicate(result):
            return result
        time.sleep(.1)
    raise AssertionError(f'Native panel did not reach expected state: {result}')


def main():
    root = Path(os.getenv('ACP_OVERLAY_EVIDENCE', '.acp/native-overlay-review')).resolve()
    root.mkdir(parents=True, exist_ok=True)
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    url = f'http://127.0.0.1:{sock.getsockname()[1]}'
    settings = Settings(data_dir=root / 'data', base_url=url)
    app = create_app(settings, start_worker=False)
    if os.getenv('ACP_OVERLAY_UI') == '1':
        # Only this isolated manual-test server has simulated mood controls.
        preview_activity = None

        @app.middleware('http')
        async def preview_metrics(request, call_next):
            if request.url.path == '/api/activity' and preview_activity is not None:
                return JSONResponse(preview_activity)
            return await call_next(request)

        @app.post('/preview/mood/{mood}')
        async def preview_mood(mood: str):
            nonlocal preview_activity
            counts = {'dead': 0, 'sad': 1, 'happy': 2, 'rainbow': 3}
            if mood not in counts:
                return JSONResponse({'detail': 'Unknown mood'}, status_code=400)
            count = counts[mood]
            preview_activity = {'running': count, 'subagents': 0, 'waiting_on_input': 1,
                                'averages': {'30m': count, '1h': count, '3h': count}}
            return {'mood': mood}

        @app.get('/preview/animation/{mood}', response_class=HTMLResponse)
        async def preview_animation(mood: str):
            counts = {'dead': 0, 'sad': 1, 'happy': 2, 'rainbow': 3}
            if mood not in counts:
                return HTMLResponse('Unknown mood', status_code=404)
            markup = (Path(__file__).parents[1] / 'acp/static/activity.html').read_text()
            markup = markup.replace('<script src="/static/activity.js" defer></script>', '')
            setup = '<style>.activity-overlay{position:relative;left:0;top:0;margin:22px auto;box-shadow:none;max-width:none}#activity-interact{display:none}</style>'
            setup += '<script>document.addEventListener("DOMContentLoaded",()=>{' + f"cometPet.setMood('{mood}');" + """
const tile=document.getElementById('activity-overlay');
tile.classList.add('is-collapsed');
document.getElementById('activity-details').hidden=true;
document.getElementById('activity-running').textContent=COUNT;
document.getElementById('activity-waiting').textContent='1';
document.getElementById('activity-pet').setAttribute('aria-label','MOOD');
for(const period of ['30m','1h','3h'])document.getElementById('activity-'+period).textContent='COUNT.00';
window.addEventListener('message',event=>{
if(event.origin!==location.origin||event.source!==parent||typeof event.data.expanded!=='boolean')return;
tile.classList.toggle('is-collapsed',!event.data.expanded);
document.getElementById('activity-details').hidden=!event.data.expanded;
});});</script>""".replace('COUNT', str(counts[mood])).replace('MOOD', mood.title())
            return markup.replace('</head>', setup + '</head>')

        @app.get('/preview', response_class=HTMLResponse)
        async def preview_controls():
            return Path(__file__).with_name('overlay_preview.html').read_text()

    monitor = CodexActivity(settings.data_dir/'codex-activity.sqlite3')
    owner = f'{os.getpid()}:{process_info(os.getpid())[1]}'
    def emit(name, session='agent-0', **fields):
        monitor.record(dict(hook_event_name=name, session_id=session, turn_id='turn', **fields), owner)
    with monitor.connect() as db:
        for table in ['agents', 'intervals', 'turns', 'waits', 'metadata']:
            db.execute(f'DELETE FROM {table}')
    for i in range(3):
        emit('UserPromptSubmit', session=f'agent-{i}')
    with monitor.connect() as db:
        db.execute('UPDATE intervals SET start=?', (time.time()-1900,))
    emit('SubagentStart', agent_id='synthetic-child')
    emit('PreToolUse', session='waiting', tool_name='request_user_input', tool_use_id='q')
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', ws='none'))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(.03)
        assert server.started
        check_surface(url)
        preferences = overlay.directory(settings) / 'preferences.json'
        preferences.write_text(json.dumps({'displayScale': 1.25}))
        first = overlay.start(settings, url)
        assert first['display_scale'] == 1.25 and first['frame']['width'] == 280, first
        assert first['heading_draggable'], first
        assert first['frame']['width'] == first['frame']['height'], first
        assert first['web_background_alpha'] == 0, first
        assert first['web_draws_background'] is False, first
        assert first['visible'] and first['nonactivating'] and first['level'] > 0
        assert first['all_spaces'] and first['full_screen_auxiliary'] and not first['hides_on_deactivate']
        metrics = wait_state(settings, lambda s: s.get('metrics', {}).get('running') == 4)
        assert metrics['metrics']['subagents'] == 1
        assert metrics['metrics']['waiting_on_input'] == 1
        assert metrics['metrics']['mood'] == 'rainbow'
        assert overlay.start(settings, url)['pid'] == first['pid']
        assert overlay.control(settings, 'hide')['visible'] is False
        assert overlay.control(settings, 'show')['visible'] is True
        if overlay.status(settings)['collapsed']:
            overlay.control(settings, 'toggle')
            wait_state(settings, lambda s: not s['collapsed'])
        expanded = overlay.status(settings)['frame']['height']
        overlay.control(settings, 'toggle')
        collapsed = wait_state(settings, lambda s: s['collapsed'] and s['frame']['height'] < expanded)
        assert collapsed['heading_draggable'], collapsed
        assert collapsed['frame']['width'] == collapsed['frame']['height'], collapsed
        overlay.control(settings, 'toggle')
        wait_state(settings, lambda s: not s['collapsed'] and s['frame']['height'] == expanded)
        overlay.control(settings, 'reset')
        before_restart = overlay.status(settings)
        assert not overlay.stop(settings)['running']
        assert not overlay.stop(settings)['running']
        second = overlay.start(settings, url)
        assert second['pid'] != first['pid']
        assert second['frame'] == before_restart['frame'], (second['frame'], before_restart['frame'])
        # Bring a different macOS app forward. Comet must remain visible and above normal windows.
        subprocess.run(['open', '-a', 'Finder'], check=True)
        other_app = wait_state(settings, lambda s: s.get('frontmost_app') == 'com.apple.finder' and s.get('on_screen'))
        assert other_app['visible']
        report = {'native_state': other_app, 'collapsed_height': collapsed['frame']['height'],
                  'expanded_height': expanded, 'restarted_pid': second['pid']}
        (root / 'native-checks.json').write_text(json.dumps(report, indent=2))
        print('Native macOS checks passed: AppKit floating panel, other-app visibility, real metrics, singleton, '
              'hide/show, collapse/expand resizing, position persistence, restart and stop.', flush=True)
        if os.getenv('ACP_OVERLAY_UI') == '1':
            print('READY_FOR_UI ' + json.dumps({'url': url, 'data': str(settings.data_dir),
                                               'pid': second['pid'], 'window_id': second['window_id'], 'controls': url + '/preview'}), flush=True)
            while overlay.status(settings)['running']:
                time.sleep(.2)
    finally:
        overlay.stop(settings)
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not thread.is_alive()


if __name__ == '__main__':
    main()
