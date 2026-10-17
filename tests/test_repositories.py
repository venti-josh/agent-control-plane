import json
import os
import subprocess
import sys

import pytest

from acp.config import Settings
from acp.service import TaskService
from acp.store import Store


def make_repo(path, remote):
    path.mkdir(parents=True)
    subprocess.run(['git', 'init', '-b', 'main', str(path)], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(path), 'remote', 'add', 'origin', remote], check=True)
    return path


def test_catalog_stays_in_search_roots_and_redacts_remote_credentials(tmp_path):
    from acp.repositories import catalog
    root = tmp_path / 'projects'
    repo = make_repo(root / 'one', 'https://secret:x-token@github.com/example/one.git')
    make_repo(root / '.hidden' / 'two', 'git@github.com:example/two.git')
    make_repo(root / 'node_modules' / 'three', 'git@github.com:example/three.git')
    (repo / 'README.md').write_text('One project')
    settings = Settings(data_dir=tmp_path / 'data')
    settings.repo_search_roots = [str(root)]
    results = catalog(settings)
    assert len(results) == 1
    assert results[0]['path'] == str(repo)
    assert results[0]['remote'] == 'https://github.com/example/one.git'
    assert 'secret' not in json.dumps(results)


@pytest.mark.parametrize('provider', ['codex', 'claude'])
async def test_agent_selects_catalog_repo_and_preserves_run_for_implementation(tmp_path, provider):
    from acp.repositories import select_repository
    repo = make_repo(tmp_path / 'projects' / 'target', 'git@github.com:example/target.git')
    agent = tmp_path / 'agent'
    agent.write_text(f'#!{sys.executable}\n' + '''import json,sys
from pathlib import Path
prompt=json.loads(sys.stdin.read())
selection={'repo_path':prompt['repositories'][0]['path'],'reason':'Issue names the target project'}
if '--output-last-message' in sys.argv:
    assert '--sandbox' in sys.argv and 'read-only' in sys.argv
    Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(json.dumps(selection))
else:
    assert sys.argv[sys.argv.index('--tools')+1] == ''
    print(json.dumps({'structured_output':selection}))
''')
    agent.chmod(0o755)
    settings = Settings(data_dir=tmp_path / 'data', codex_command=str(agent), claude_command=str(agent))
    settings.repo_search_roots = [str(tmp_path / 'projects')]
    settings.validate()
    service = TaskService(Store(settings.db_path), settings)
    task = service.create_task('Work on target', provider=provider, state='todo')
    run = service.claim_next()
    service.mark_started(run['id'])
    assert await select_repository(service, service.get_task(task['id']), run) is True
    selected = service.get_task(task['id'])
    assert selected['repo_path'] == str(repo)
    assert selected['state'] == 'in_progress'
    assert selected['session_id'] is None
    assert 'Issue names the target project' in selected['comments'][0]['body']


async def test_missing_repository_requests_input_instead_of_failing(service):
    from acp.repositories import select_repository
    service.settings.repo_search_roots = []
    task = service.create_task('Unclear task', provider='codex', state='todo')
    run = service.claim_next()
    service.mark_started(run['id'])
    assert await select_repository(service, service.get_task(task['id']), run) is False
    current = service.get_task(task['id'])
    assert current['state'] == 'waiting_on_input'
    assert current['repo_path'] == ''
    assert current['blocker_comment_id']
