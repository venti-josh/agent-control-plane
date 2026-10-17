"""Native browser checks against an isolated database; no model calls.

CHROMIUM_EXECUTABLE='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' \
  uv run --locked --group browser python tests/browser_overlay.py
"""
import os
import socket
import tempfile
import threading
import time
from pathlib import Path

import uvicorn
from playwright.sync_api import expect, sync_playwright

from acp.api import create_app
from acp.config import Settings


def main():
    with tempfile.TemporaryDirectory() as tmp:
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        url = f'http://127.0.0.1:{sock.getsockname()[1]}'
        app = create_app(Settings(data_dir=Path(tmp), base_url=url), start_worker=False)
        service = app.state.service
        server = uvicorn.Server(uvicorn.Config(app, log_level='error', ws='none'))
        thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
        thread.start()
        try:
            for _ in range(150):
                if server.started:
                    break
                time.sleep(.02)
            assert server.started
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, executable_path=os.getenv('CHROMIUM_EXECUTABLE'))
                page = browser.new_page(viewport={'width': 1400, 'height': 900})
                page.clock.install()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(url + '/overlay')
                tile = page.locator('#activity-overlay')
                expect(tile).to_be_visible()
                assert page.evaluate('getComputedStyle(document.documentElement).backgroundColor') == 'rgba(0, 0, 0, 0)'
                expect(page.locator('#activity-running')).to_have_text('0')
                expect(tile).to_have_attribute('data-mood', 'dead')
                bounds = tile.bounding_box()
                assert bounds['width'] == bounds['height'], 'Expanded overlay should be square'
                screenshot_dir = Path(os.getenv('ACP_SCREENSHOT_DIR', tmp))
                screenshot_dir.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(screenshot_dir / 'dead.png'))
                pet = page.locator('#activity-pet')
                expect(pet).to_have_attribute('aria-label', 'Comet is dead: X eyes and a limp tail')
                dead_frame = pet.evaluate('(el) => el.toDataURL()')
                page.clock.run_for(250)
                assert pet.evaluate('(el) => el.toDataURL()') == dead_frame
                # Real backend history drives every mood, including exact thresholds.
                for count, mood in [(1, 'sad'), (2, 'happy'), (3, 'rainbow')]:
                    service.create_task(f'Agent {count}', state='todo')
                    run = service.claim_next()
                    service.mark_started(run['id'])
                    with service.store.transaction() as db:
                        db.execute('UPDATE runs SET started_at=? WHERE id=?', (time.time() - 1900, run['id']))
                    if count != 1:
                        page.evaluate('activityOverlay.refresh()')
                    # First transition proves the automatic five-second refresh works.
                    expect(tile).to_have_attribute('data-mood', mood, timeout=8000)
                    page.clock.run_for(80)
                    frame = pet.evaluate('(el) => el.toDataURL()')
                    page.clock.run_for(250)
                    assert pet.evaluate('(el) => el.toDataURL()') != frame, f'{mood} should animate'
                    page.screenshot(path=str(screenshot_dir / f'{mood}.png'))
                    expect(page.locator('#activity-running')).to_have_text(str(count))
                expect(page.locator('#activity-30m')).to_have_text('3.00')
                task = service.create_task('Need answer', state='todo')
                service.request_input(task['id'], 'Which option?')
                page.evaluate('activityOverlay.refresh()')
                expect(page.locator('#activity-waiting')).to_have_text('1')
                # Pointer drag, keyboard move, and persistence across reloads.
                handle = page.locator('#activity-interact')
                box = handle.bounding_box()
                page.mouse.move(box['x'] + 30, box['y'] + 15)
                page.mouse.down()
                page.mouse.move(120, 200, steps=8)
                page.mouse.up()
                expect(page.locator('#activity-details')).to_be_visible()
                position = tile.bounding_box()
                handle.focus()
                page.keyboard.press('ArrowRight')
                assert tile.bounding_box()['x'] == position['x'] + 16
                page.get_by_role('button', name='Collapse agent overlay').click()
                expect(page.locator('#activity-details')).to_be_hidden()
                bounds = tile.bounding_box()
                assert bounds['width'] == bounds['height'], 'Minimized overlay should be square'
                page.reload()
                expect(page.locator('#activity-details')).to_be_hidden()
                assert tile.bounding_box()['x'] == position['x'] + 16
                page.get_by_role('button', name='Expand agent overlay').click()
                expect(page.locator('#activity-details')).to_be_visible()
                handle.focus()
                page.keyboard.press('Escape')
                expect(tile).to_be_hidden()
                page.reload()
                expect(tile).to_be_hidden()
                page.get_by_role('button', name='Show agent overlay').click()
                expect(tile).to_be_visible()
                # Shrinking a viewport must keep all controls reachable.
                page.set_viewport_size({'width': 320, 'height': 568})
                # Deliver resize/animation frames before checking the clamped position.
                page.clock.run_for(100)
                bounds = tile.bounding_box()
                assert bounds['x'] >= 0 and bounds['x'] + bounds['width'] <= 320
                assert bounds['y'] >= 0 and bounds['y'] + bounds['height'] <= 568
                # Dragging beyond either edge is clamped, including at narrow sizes.
                handle.focus()
                for _ in range(40):
                    page.keyboard.press('ArrowUp')
                    page.keyboard.press('ArrowLeft')
                bounds = tile.bounding_box()
                assert bounds['x'] >= 0 and bounds['y'] >= 0
                for _ in range(40):
                    page.keyboard.press('ArrowDown')
                    page.keyboard.press('ArrowRight')
                bounds = tile.bounding_box()
                assert bounds['x'] + bounds['width'] <= 320
                assert bounds['y'] + bounds['height'] <= 568
                page.emulate_media(reduced_motion='reduce')
                page.clock.run_for(100)
                still = pet.evaluate('(el) => el.toDataURL()')
                page.clock.run_for(250)
                assert pet.evaluate('(el) => el.toDataURL()') == still, 'Reduced motion must freeze canvas'
                page.emulate_media(reduced_motion='no-preference')
                page.wait_for_timeout(100)
                page.clock.run_for(250)
                assert pet.evaluate('(el) => el.toDataURL()') != still, 'Motion should resume'
                page.screenshot(path=os.getenv('ACP_SCREENSHOT', str(Path(tmp) / 'overlay.png')))
                # Failed requests must not present stale counters as current.
                page.route('**/api/activity', lambda route: route.abort())
                page.evaluate('activityOverlay.refresh()')
                expect(page.locator('#activity-status')).to_have_text('Activity unavailable')
                expect(page.locator('#activity-running')).to_have_text('—')
                expect(tile).to_have_attribute('data-mood', 'unavailable')
                page.unroute('**/api/activity')
                page.evaluate('activityOverlay.refresh()')
                expect(page.locator('#activity-running')).to_have_text('3')
                # A response slower than the polling interval must still reach the tile.
                held = []
                page.route('**/api/activity', lambda route: held.append(route))
                with page.expect_request('**/api/activity'):
                    page.evaluate('void activityOverlay.refresh()')
                page.wait_for_timeout(50)
                page.clock.fast_forward(5500)
                page.wait_for_timeout(50)
                assert len(held) == 1, 'Polling must not invalidate an in-flight request'
                held[0].fulfill(json={'running': 7, 'subagents': 4, 'waiting_on_input': 1,
                                     'averages': {'30m': 3, '1h': 2, '3h': 1}})
                expect(page.locator('#activity-running')).to_have_text('7')
                page.unroute('**/api/activity')
                page.evaluate('activityOverlay.refresh()')
                expect(page.locator('#activity-running')).to_have_text('3')
                held.clear()
                page.route('**/api/activity', lambda route: held.append(route))
                with page.expect_request('**/api/activity'):
                    page.evaluate('void activityOverlay.refresh()')
                page.wait_for_timeout(50)
                page.clock.fast_forward(8100)
                expect(page.locator('#activity-status')).to_have_text('Activity unavailable')
                expect(page.locator('#activity-running')).to_have_text('—')
                expect(tile).to_have_attribute('data-mood', 'unavailable')
                for route in held:
                    route.abort()
                page.unroute('**/api/activity')
                page.evaluate('activityOverlay.refresh()')
                expect(page.locator('#activity-running')).to_have_text('3')
                assert not errors, errors
                browser.close()
                print('Overlay browser checks passed: real metrics/moods, drag, keyboard, collapse/hide, '
                      'reload persistence, mobile bounds, reduced motion, failure/recovery, slow response and timeout.')
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            sock.close()
            assert not thread.is_alive()


if __name__ == '__main__':
    main()
