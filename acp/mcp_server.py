"""Four task-scoped MCP tools, shared by the Claude and Codex adapters."""
from __future__ import annotations

import os

from .cli import request
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("acp")


def call(path: str = "", payload: dict | None = None, request_id: str | None = None) -> dict:
    base = os.environ['ACP_URL'].rstrip('/')
    task_id = os.environ['ACP_TASK_ID']
    return request(base, 'GET' if payload is None else 'POST', f'/api/tasks/{task_id}{path}',
                   None if payload is None else {**payload, 'run_id': os.environ['ACP_RUN_ID']},
                   idempotency_key=request_id)


@mcp.tool()
def get_task() -> dict:
    """Read this task's current state, comments, outputs and run history."""
    return call()


@mcp.tool()
def add_comment(text: str, request_id: str | None = None) -> dict:
    """Record progress and evidence. Link local files with absolute Markdown paths for Linear upload.

    Screenshots: ![description](</absolute/path.png>); files: [report](</absolute/path.md>).
    This never starts another agent.
    """
    return call("/comments", {"body": text}, request_id)


@mcp.tool()
def request_input(question: str, request_id: str | None = None) -> dict:
    """Save a specific blocking question and move to Waiting on Input atomically. Then stop work."""
    return call("/request-input", {"question": question}, request_id)


@mcp.tool()
def submit_for_review(summary: str, output_url: str = "", kind: str = "text", request_id: str | None = None) -> dict:
    """Submit tested work and evidence; move to Ready for Review, then stop work.

    kind: text, pr, file, or url. Use kind=file for a local output_url. Include other
    evidence as absolute Markdown links/images in summary so Linear can receive it.
    """
    return call("/submit-for-review", {"summary": summary, "output_url": output_url, "kind": kind}, request_id)


if __name__ == "__main__":
    mcp.run(transport="stdio")
