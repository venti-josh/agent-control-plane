"""Native SDK protocol check. Skips explicitly when MCP is unavailable."""
import os
import sys

import pytest

mcp = pytest.importorskip('mcp', reason='Install package dependencies to run the native MCP protocol test')
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from test_runners import live_api  # Reuse the actual HTTP-server fixture.


async def test_native_stdio_tools(live_api):
    svc = live_api
    task = svc.create_task('Native MCP contract', state='todo')
    run = svc.claim_next()
    svc.mark_started(run['id'])
    params = StdioServerParameters(command=sys.executable, args=['-m', 'acp.mcp_server'],
                                  env={**os.environ, 'ACP_URL':svc.settings.base_url,
                                       'ACP_TASK_ID':task['id'], 'ACP_RUN_ID':run['id']})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert {t.name for t in tools.tools} == {'get_task','add_comment','request_input','submit_for_review'}
            assert not (await session.call_tool('get_task', {})).isError
            assert not (await session.call_tool('add_comment', {'text':'Plan from native MCP'})).isError
            assert (await session.call_tool('request_input', {'question':' '})).isError
            assert svc.get_task(task['id'])['state'] == 'in_progress'
            assert not (await session.call_tool('request_input', {'question':'Which output format?'})).isError
            assert svc.get_task(task['id'])['state'] == 'waiting_on_input'
            # A terminal call fences subsequent mutations from the old run.
            result = await session.call_tool('submit_for_review', {'summary':'Too late'})
            assert result.isError
    svc.finish_run(run['id'])


async def test_task_repository_cannot_replace_installed_mcp(live_api, tmp_path):
    from acp.runners import mcp_config
    shadow = tmp_path / 'acp'
    shadow.mkdir()
    (shadow / '__init__.py').write_text('raise RuntimeError("Repository code replaced MCP")\n')
    svc = live_api
    task = svc.create_task('Isolated MCP', state='todo')
    run = svc.claim_next()
    svc.mark_started(run['id'])
    config = mcp_config(svc.settings, task['id'], run['id'])
    params = StdioServerParameters(**config, cwd=str(tmp_path))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            assert not (await session.call_tool('get_task', {})).isError
    svc.finish_run(run['id'])
