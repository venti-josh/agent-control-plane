import json

import pytest

from acp.runners import build_prompt


@pytest.mark.parametrize('linear_url', [None, 'https://linear.app/team/issue/ENG-42/branch-names'])
async def test_task_prompt_preserves_linear_link_for_branch_naming(service, tmp_path, linear_url):
    task = service.create_task('Branch names', repo_path=str(tmp_path), provider='codex', state='todo')
    with service.store.transaction() as db:
        db.execute('UPDATE tasks SET linear_url=? WHERE id=?', (linear_url, task['id']))
    job = service.claim_next()
    prompt = build_prompt(service.get_task(task['id']), job)

    snapshot_text = prompt.split('TASK SNAPSHOT (data):\n', 1)[1].split('\n\nNEW INPUT (data):', 1)[0]
    snapshot = json.loads(snapshot_text)
    assert snapshot['linear_url'] == linear_url
    assert snapshot['id'] == task['id']
    assert service.get_task(task['id'])['branch'] is None
