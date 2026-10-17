import asyncio
import hashlib
import hmac
import json
import time

import httpx
import pytest

from acp.integrations import Integrations
from acp.worker import Worker


async def test_demo_full_input_and_review_roundtrip(service):
    task = service.create_task('Demo', description='[ask] Need input', state='todo')
    worker = Worker(service)
    assert await worker.run_once()
    current = service.get_task(task['id'])
    assert current['state'] == 'waiting_on_input'
    session = current['session_id']
    assert not await worker.run_once()
    service.add_comment(task['id'], 'Use the strict option', in_reply_to=current['blocker_comment_id'], resume=True)
    assert await worker.run_once()
    assert service.get_task(task['id'])['state'] == 'ready_for_review'
    assert not await worker.run_once()
    service.request_changes(task['id'], 'Improve it')
    assert await worker.run_once()
    current = service.get_task(task['id'])
    assert current['state'] == 'ready_for_review'
    assert current['session_id'] == session
    assert len(current['runs']) == 3
    assert all(r['status'] == 'succeeded' for r in current['runs'])


async def test_failure_is_visible_and_never_auto_retries(service):
    class Failure:
        async def run(self, *args):
            raise RuntimeError('Provider authentication failed')
    task = service.create_task('Bad provider', state='todo')
    worker = Worker(service, runners={'demo':Failure()})
    await worker.run_once()
    current = service.get_task(task['id'])
    assert current['state'] == 'waiting_on_input'
    assert current['last_error'] == 'Provider authentication failed'
    assert current['comments'][0]['body'] == current['last_error']
    assert not await worker.run_once()


async def test_one_worker_per_database(service):
    first, second = Worker(service), Worker(service)
    await first.start()
    try:
        with pytest.raises(RuntimeError, match='already'):
            await second.start()
    finally:
        await first.stop()
    await second.start()
    await second.stop()


async def test_outgoing_events_are_signed_and_persisted(service):
    subscription = service.subscribe('https://receiver.example/hook', ['task.*','comment.*'], 'shared-secret')
    task = service.create_task('Event test')
    service.add_comment(task['id'], 'Comment')
    assert len(service.store.all("SELECT * FROM outbox")) == 2
    received = []
    def transport(request):
        received.append(request)
        return httpx.Response(204)
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        worker = Worker(service)
        integrations = Integrations(service, client)
        assert await worker.deliver_once(integrations)
        assert await worker.deliver_once(integrations)
        assert not await worker.deliver_once(integrations)
    assert len(received) == 2
    for request in received:
        expected = 'sha256=' + hmac.new(b'shared-secret', request.content, hashlib.sha256).hexdigest()
        assert request.headers['X-ACP-Signature'] == expected
        assert request.headers['X-ACP-Event-ID'] == str(json.loads(request.content)['id'])
    assert all(x['status'] == 'done' for x in service.store.all('SELECT * FROM outbox'))


async def test_delivery_backoff_deadletter_and_retry(service):
    service.settings.max_delivery_attempts = 2
    service.subscribe('https://receiver.example/hook', ['task.created'])
    service.create_task('Task')
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as client:
        worker = Worker(service)
        integrations = Integrations(service, client)
        assert await worker.deliver_once(integrations)
        item = service.store.one('SELECT * FROM outbox')
        assert item['attempts'] == 1 and item['status'] == 'pending' and item['available_at'] > time.time()
        assert not await worker.deliver_once(integrations)
        with service.store.transaction() as db:
            db.execute('UPDATE outbox SET available_at=0')
        assert await worker.deliver_once(integrations)
        item = service.store.one('SELECT * FROM outbox')
        assert item['status'] == 'failed' and item['attempts'] == 2


def test_deleted_subscriber_no_longer_receives(service):
    sub = service.subscribe('https://receiver.example/hook', ['task.created'])
    service.create_task('Task')
    with service.store.transaction() as db:
        db.execute('DELETE FROM subscriptions WHERE id=?', (sub['id'],))
    assert len(service.store.all('SELECT * FROM outbox')) == 1  # Delivery safely becomes a no-op.


def test_subscription_api_does_not_return_secret(client):
    created = client.post('/api/subscriptions', json={'url':'http://localhost:9000/hook','types':['*'],'secret':'hidden'})
    assert created.status_code == 201
    assert 'hidden' not in client.get('/api/subscriptions').text


def test_receipt_deduplication_is_atomic(service):
    assert not service.receive('test', 'delivery-1', {}, 'test')['duplicate']
    assert service.receive('test', 'delivery-1', {}, 'test')['duplicate']
    assert len(service.store.all('SELECT * FROM outbox')) == 1
