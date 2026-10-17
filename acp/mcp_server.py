"""Four task-scoped MCP tools, shared by the Claude and Codex adapters."""
from __future__ import annotations

import os

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("acp")


def call(path: str = "", payload: dict | None = None) -> dict:
    base = os.environ["ACP_URL"].rstrip("/")
    task_id = os.environ["ACP_TASK_ID"]
    with httpx.Client(timeout=15, trust_env=False) as client:
        if payload is None:
            response = client.get(f"{base}/api/tasks/{task_id}{path}")
        else:
            response = client.post(f"{base}/api/tasks/{task_id}{path}",
                                   json={**payload, "run_id": os.environ["ACP_RUN_ID"]})
        if response.is_error:
            raise ValueError(response.json().get("detail", response.text))
        return response.json()


@mcp.tool()
def get_task() -> dict:
    """Read this task's current state, comments, outputs and run history."""
    return call()


@mcp.tool()
def add_comment(text: str) -> dict:
    """Record progress and evidence. Link local files with absolute Markdown paths for Linear upload.

    Screenshots: ![description](</absolute/path.png>); files: [report](</absolute/path.md>).
    This never starts another agent.
    """
    return call("/comments", {"body": text})


@mcp.tool()
def request_input(question: str) -> dict:
    """Save a specific blocking question and move to Waiting on Input atomically. Then stop work."""
    return call("/request-input", {"question": question})


@mcp.tool()
def submit_for_review(summary: str, output_url: str = "", kind: str = "text") -> dict:
    """Submit tested work and evidence; move to Ready for Review, then stop work.

    kind: text, pr, file, or url. Use kind=file for a local output_url. Include other
    evidence as absolute Markdown links/images in summary so Linear can receive it.
    """
    return call("/submit-for-review", {"summary": summary, "output_url": output_url, "kind": kind})


if __name__ == "__main__":
    mcp.run(transport="stdio")
