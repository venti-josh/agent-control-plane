"""Native launcher safeguards; AppKit interaction checks live in mac_overlay.py."""
import json

import pytest

from acp import overlay


def test_overlay_url_accepts_only_local_http_service_origins():
    assert overlay.local_url('http://127.0.0.1:8877/') == 'http://127.0.0.1:8877'
    for url in ['https://example.com', 'http://example.com:8765', 'file:///tmp/overlay.html',
                'http://user:secret@127.0.0.1:8765', 'http://127.0.0.1:8765/path',
                'http://127.0.0.1:8765/?remote=true', 'http://127.0.0.1:99999']:
        with pytest.raises(ValueError):
            overlay.local_url(url)


def test_stale_native_status_never_signals_a_pid(settings):
    directory = settings.data_dir / 'overlay'
    directory.mkdir(parents=True)
    (directory / 'state.json').write_text(json.dumps({'pid': 1, 'visible': True}))
    assert overlay.status(settings)['running'] is False
    assert overlay.stop(settings)['running'] is False


def test_overlay_refuses_non_macos_without_compiling(settings, monkeypatch):
    monkeypatch.setattr(overlay.sys, 'platform', 'linux')
    with pytest.raises(ValueError, match='macOS'):
        overlay.start(settings)
    assert not (settings.data_dir / 'overlay' / 'Leo.app').exists()


def test_native_surface_is_separate_from_board(client):
    board = client.get('/').text
    assert 'id="activity-overlay"' not in board
    assert '/static/activity.js' not in board
    page = client.get('/overlay')
    assert page.status_code == 200
    assert 'id="activity-overlay"' in page.text
    assert '/static/activity.js' in page.text
    assert 'id="board"' not in page.text
    assert client.get('/api/activity').status_code == 200
