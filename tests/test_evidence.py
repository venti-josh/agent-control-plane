import json

import httpx
import pytest

from acp.integrations import Integrations
from acp.service import DomainError, TaskService
from acp.store import Store
from conftest import started


def linked_task(service):
    task, run = started(service)
    with service.store.transaction() as db:
        db.execute("UPDATE tasks SET linear_id='issue-one' WHERE id=?", (task['id'],))
    return task, run


class LinearFiles:
    def __init__(self):
        self.uploads, self.files, self.comments = [], [], []
        self.fail_put = False
        self.lose_comment_response = False

    def respond(self, request):
        if request.method == 'PUT':
            assert 'authorization' not in request.headers
            assert request.headers['x-upload-test'] == 'signed-header'
            if self.fail_put:
                return httpx.Response(503)
            self.files.append(request.content)
            return httpx.Response(200)
        data = json.loads(request.content)
        query, variables = data['query'], data['variables']
        if 'fileUpload(' in query:
            self.uploads.append(variables)
            number = len(self.uploads)
            result = {'fileUpload': {'success': True, 'uploadFile': {
                'uploadUrl': f'https://storage.example/{number}',
                'assetUrl': f'https://uploads.linear.app/evidence/{number}',
                'headers': [{'key': 'x-upload-test', 'value': 'signed-header'}]}}}
        elif 'commentCreate' in query:
            self.comments.append({'id': 'remote-comment', 'body': variables['input']['body'], 'parent': None})
            if self.lose_comment_response:
                raise httpx.ReadTimeout('Response lost')
            result = {'commentCreate': {'success': True, 'comment': {'id': 'remote-comment'}}}
        elif 'commentUpdate' in query:
            self.comments[0]['body'] = variables['input']['body']
            result = {'commentUpdate': {'success': True}}
        else:
            assert 'comments(first:100' in query
            result = {'issue': {'comments': {'nodes': self.comments,
                      'pageInfo': {'hasNextPage': False, 'endCursor': None}}}}
        return httpx.Response(200, json={'data': result})


async def test_review_uploads_snapshot_of_screenshot_and_document(linear_service, tmp_path):
    task, run = linked_task(linear_service)
    screenshot = tmp_path / 'screen (final).png'
    report = tmp_path / 'verification notes.md'
    screenshot.write_bytes(b'PNG evidence')
    report.write_text('# Verified\n')
    linear_service.submit_for_review(task['id'], f'Verified\n![UI](<{screenshot}>)',
                                     str(report), 'file', run['id'])
    comment = linear_service.get_task(task['id'])['comments'][-1]
    screenshot.unlink()
    report.unlink()
    remote = LinearFiles()
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote.respond)) as client:
        await Integrations(linear_service, client).sync_linear_comment(task['id'], comment['id'])
    body = remote.comments[0]['body']
    assert '![UI](<https://uploads.linear.app/evidence/1>)' in body
    assert '[verification notes.md](<https://uploads.linear.app/evidence/2>)' in body
    assert str(tmp_path) not in body
    assert remote.files == [b'PNG evidence', b'# Verified\n']
    assert remote.uploads[0] == {'filename': 'screen (final).png', 'contentType': 'image/png', 'size': 12}
    assert remote.uploads[1]['contentType'] == 'text/markdown'


async def test_evidence_retry_reuses_upload_after_restart_and_lost_comment_response(linear_service, tmp_path):
    task, _ = linked_task(linear_service)
    file = tmp_path / 'proof.png'
    file.write_bytes(b'proof')
    comment = linear_service.add_comment(task['id'], f'![One]({file})\n![Again]({file})')
    remote = LinearFiles()
    remote.lose_comment_response = True
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote.respond)) as client:
        with pytest.raises(httpx.ReadTimeout):
            await Integrations(linear_service, client).sync_linear_comment(task['id'], comment['id'])
        file.unlink()
        restarted = TaskService(Store(linear_service.settings.db_path), linear_service.settings)
        await Integrations(restarted, client).sync_linear_comment(task['id'], comment['id'])
    assert len(remote.uploads) == len(remote.files) == len(remote.comments) == 1
    assert remote.comments[0]['body'].count('https://uploads.linear.app/evidence/1') == 2


async def test_failed_upload_does_not_publish_broken_comment_and_can_retry(linear_service, tmp_path):
    task, _ = linked_task(linear_service)
    file = tmp_path / 'proof.md'
    file.write_text('Evidence')
    comment = linear_service.add_comment(task['id'], f'[Proof]({file.as_uri()})')
    remote = LinearFiles()
    remote.fail_put = True
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote.respond)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await Integrations(linear_service, client).sync_linear_comment(task['id'], comment['id'])
        assert remote.comments == []
        remote.fail_put = False
        await Integrations(linear_service, client).sync_linear_comment(task['id'], comment['id'])
    assert remote.files == [b'Evidence']
    assert 'https://uploads.linear.app/evidence/2' in remote.comments[0]['body']


def test_missing_evidence_rejects_review_atomically(linear_service, tmp_path):
    task, run = linked_task(linear_service)
    with pytest.raises(DomainError, match='evidence'):
        linear_service.submit_for_review(task['id'], 'Done', str(tmp_path / 'missing.md'), 'file', run['id'])
    current = linear_service.get_task(task['id'])
    assert current['state'] == 'in_progress'
    assert current['artifacts'] == current['comments'] == []


async def test_edits_replace_evidence_but_keep_uploaded_unchanged_files(linear_service, tmp_path):
    task, _ = linked_task(linear_service)
    file = tmp_path / 'proof.md'
    file.write_text('Before')
    comment = linear_service.add_comment(task['id'], f'[Proof]({file})')
    remote = LinearFiles()
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote.respond)) as client:
        integration = Integrations(linear_service, client)
        await integration.sync_linear_comment(task['id'], comment['id'])
        file.unlink()
        linear_service.edit_comment(task['id'], comment['id'], f'New wording [Proof]({file})')
        await integration.sync_linear_comment(task['id'], comment['id'])
        assert len(remote.uploads) == 1
        file.write_text('After')
        linear_service.edit_comment(task['id'], comment['id'], f'Updated [Proof]({file})')
        await integration.sync_linear_comment(task['id'], comment['id'])
    assert remote.files == [b'Before', b'After']
    assert 'https://uploads.linear.app/evidence/2' in remote.comments[0]['body']


async def test_remote_links_code_examples_and_imported_comments_are_not_uploaded(linear_service, tmp_path):
    task, _ = linked_task(linear_service)
    body = ('![Remote](https://example.com/screen.png)\n'
            '`![Example](/missing.png)`\n```md\n[Example](/missing.md)\n```\n'
            '[Source](/src/app.py:12)')
    comment = linear_service.add_comment(task['id'], body)
    # Inbound Linear text must never cause the service to read local files.
    with linear_service.store.transaction() as db:
        linear_service._comment(db, task['id'], '[Remote text](/missing.md)', 'Linear', 'linear')
    remote = LinearFiles()
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote.respond)) as client:
        await Integrations(linear_service, client).sync_linear_comment(task['id'], comment['id'])
    assert remote.uploads == []
    assert remote.comments[0]['body'].endswith(body)


@pytest.mark.parametrize('kind', ['oversized', 'directory', 'fifo'])
def test_invalid_evidence_cannot_create_comment(linear_service, tmp_path, kind):
    import os

    task, _ = linked_task(linear_service)
    path = tmp_path / 'evidence'
    if kind == 'oversized':
        with path.open('wb') as stream:
            stream.truncate(20 * 1024 * 1024 + 1)
    elif kind == 'directory':
        path.mkdir()
    else:
        os.mkfifo(path)
    with pytest.raises(DomainError, match='evidence'):
        linear_service.add_comment(task['id'], f'[Evidence](<{path}>)')
    assert linear_service.get_task(task['id'])['comments'] == []


async def test_normal_link_forms_and_external_destinations(linear_service, tmp_path):
    task, _ = linked_task(linear_service)
    path = tmp_path / 'screenshot (1).png'
    path.write_bytes(b'picture')
    body = (f'![Screenshot]({path})\n'
            f'[Download](<{path.as_uri()}>)\n'
            '[External](https://example.com/report.md)\n'
            '[Relative](./report.md)\n'
            '[Network](file://another-host/report.md)')
    comment = linear_service.add_comment(task['id'], body)
    remote = LinearFiles()
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote.respond)) as client:
        await Integrations(linear_service, client).sync_linear_comment(task['id'], comment['id'])
    assert '![Screenshot](https://uploads.linear.app/evidence/1)' in remote.comments[0]['body']
    assert '[Download](<https://uploads.linear.app/evidence/2>)' in remote.comments[0]['body']
    assert '[External](https://example.com/report.md)' in remote.comments[0]['body']
    assert '[Relative](./report.md)' in remote.comments[0]['body']
    assert '[Network](file://another-host/report.md)' in remote.comments[0]['body']


@pytest.mark.parametrize('example', [
    '    [Example]({path})',
    '\t![Example]({path})',
    r'\[Example]({path})',
    '````md\n```\n[Example]({path})\n```\n````',
    '~~~md\n[Example]({path})\n~~~',
    '``use ` [Example]({path})``',
    ' \t[Example]({path})',
    '- ```md\n  [Example]({path})\n  ```',
    '<pre>\n\n[Example]({path})\n</pre>',
])
async def test_markdown_code_and_escaped_examples_never_upload(linear_service, tmp_path, example):
    task, _ = linked_task(linear_service)
    path = tmp_path / 'not-evidence.txt'
    path.write_text('Must stay local')
    body = example.format(path=path)
    comment = linear_service.add_comment(task['id'], body)
    remote = LinearFiles()
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote.respond)) as client:
        await Integrations(linear_service, client).sync_linear_comment(task['id'], comment['id'])
    assert remote.uploads == []
    assert remote.comments[0]['body'].endswith(body)


@pytest.mark.parametrize('destination', ['<{path}> "Verification"', '{path} "Verification"',
                                         "<{path}> 'Verification'"])
async def test_link_title_is_preserved_separately_from_file_path(linear_service, tmp_path, destination):
    task, _ = linked_task(linear_service)
    path = tmp_path / 'report.md'
    path.write_text('Evidence')
    body = '[Report](' + destination.format(path=path) + ')'
    comment = linear_service.add_comment(task['id'], body)
    remote = LinearFiles()
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote.respond)) as client:
        await Integrations(linear_service, client).sync_linear_comment(task['id'], comment['id'])
    assert 'https://uploads.linear.app/evidence/1' in remote.comments[0]['body']
    assert 'Verification' in remote.comments[0]['body']
    assert str(path) not in remote.comments[0]['body']


@pytest.mark.parametrize('hidden', [
    'Notes <!-- [example](<{path}>) -->',
    'Notes <!-- metadata\n![example](<{path}>)\n--> end',
    'Notes <!-- unfinished [example](<{path}>)',
])
async def test_inline_html_comments_do_not_upload_files(linear_service, tmp_path, hidden):
    task, _ = linked_task(linear_service)
    secret = tmp_path / 'not-evidence.env'
    secret.write_text('Must remain local')
    visible = tmp_path / 'report.md'
    visible.write_text('Public evidence')
    body = f'[Visible](<{visible}>)\n\n' + hidden.format(path=secret)
    comment = linear_service.add_comment(task['id'], body)
    remote = LinearFiles()
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote.respond)) as client:
        await Integrations(linear_service, client).sync_linear_comment(task['id'], comment['id'])
    assert remote.files == [b'Public evidence']
    assert remote.comments[0]['body'].endswith(hidden.format(path=secret))
    assert '[Visible](<https://uploads.linear.app/evidence/1>)' in remote.comments[0]['body']


@pytest.mark.parametrize('label', ['Settings [dark mode]', 'Settings [dark [compact] mode]', r'Settings \[dark\]'])
@pytest.mark.parametrize('image', ['', '!'])
async def test_balanced_link_labels_upload_and_retain_label(linear_service, tmp_path, label, image):
    task, _ = linked_task(linear_service)
    file = tmp_path / 'screen.png'
    file.write_bytes(b'Image evidence')
    comment = linear_service.add_comment(task['id'], f'{image}[{label}](<{file}> "Preview")')
    remote = LinearFiles()
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote.respond)) as client:
        await Integrations(linear_service, client).sync_linear_comment(task['id'], comment['id'])
    assert remote.files == [b'Image evidence']
    assert f'{image}[{label}](<https://uploads.linear.app/evidence/1> "Preview")' in remote.comments[0]['body']


@pytest.mark.parametrize('host', ['another-host', 'user@localhost', 'localhost:8000'])
def test_file_review_rejects_nonlocal_authority_atomically(linear_service, tmp_path, host):
    task, run = linked_task(linear_service)
    file = tmp_path / 'report.md'
    file.write_text('Wrong local file')
    with pytest.raises(DomainError, match='local'):
        linear_service.submit_for_review(task['id'], 'Report', f'file://{host}{file}', 'file', run['id'])
    current = linear_service.get_task(task['id'])
    assert current['state'] == 'in_progress'
    assert current['artifacts'] == current['comments'] == []
    assert linear_service.store.all('SELECT * FROM comment_files') == []


async def test_literal_comment_opener_in_code_does_not_hide_evidence(linear_service, tmp_path):
    task, _ = linked_task(linear_service)
    file = tmp_path / 'report.md'
    file.write_text('Evidence')
    body = f'Document the `<!--` syntax.\n\n[Evidence](<{file}>)'
    comment = linear_service.add_comment(task['id'], body)
    remote = LinearFiles()
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote.respond)) as client:
        await Integrations(linear_service, client).sync_linear_comment(task['id'], comment['id'])
    assert remote.files == [b'Evidence']
    assert 'Document the `<!--` syntax.' in remote.comments[0]['body']
    assert '[Evidence](<https://uploads.linear.app/evidence/1>)' in remote.comments[0]['body']


async def test_linked_screenshot_uploads_image_and_preserves_external_link(linear_service, tmp_path):
    task, _ = linked_task(linear_service)
    file = tmp_path / 'screen.png'
    file.write_bytes(b'Image evidence')
    comment = linear_service.add_comment(task['id'], f'[![Screenshot](<{file}>)](https://example.com)')
    remote = LinearFiles()
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote.respond)) as client:
        await Integrations(linear_service, client).sync_linear_comment(task['id'], comment['id'])
    assert remote.files == [b'Image evidence']
    assert '[![Screenshot](<https://uploads.linear.app/evidence/1>)](https://example.com)' in remote.comments[0]['body']
