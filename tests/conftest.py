from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from acp.api import create_app
from acp.config import STATES, Settings
from acp.service import TaskService
from acp.store import Store


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path / 'data', poll_seconds=0.02)


@pytest.fixture
def service(settings):
    settings.validate()
    return TaskService(Store(settings.db_path), settings)


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings, start_worker=False)) as client:
        yield client


@pytest.fixture
def linear_service(tmp_path):
    settings = Settings(data_dir=tmp_path / 'linear', linear_api_key='test-key',
                        linear_workspace_id='workspace-one', linear_team_id='team-one',
                        linear_states={s: f'state-{s}' for s in STATES},
                        linear_webhook_secret='linear-secret', github_webhook_secret='github-secret')
    settings.validate()
    return TaskService(Store(settings.db_path), settings)


def started(service, **kwargs):
    task = service.create_task('Example task', state='todo', **kwargs)
    run = service.claim_next()
    service.mark_started(run['id'])
    return service.get_task(task['id']), run


def reviewed(service, pr=False):
    task, run = started(service)
    service.submit_for_review(task['id'], 'Tests passed',
                              'https://github.com/example/project/pull/7' if pr else '',
                              'pr' if pr else 'text', run['id'])
    service.finish_run(run['id'])
    return service.get_task(task['id']), run
