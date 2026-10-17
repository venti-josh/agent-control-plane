from __future__ import annotations

import asyncio
import json
import math
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .config import LABELS, PROVIDERS, STATES, Settings
from .integrations import Integrations, verify_signature
from .service import DomainError, TaskService
from .store import Store
from .worker import Worker


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DesktopConnection(Model):
    thread_id: str | None = None


class DesktopRequest(Model):
    thread_id: str = ''
    repo: str = ''
    action: Literal['get', 'comment', 'input', 'review'] = 'get'
    text: str = Field(default='', max_length=100000)
    output_url: str = ''
    kind: Literal['text', 'file', 'url', 'pr'] = 'text'
    error: str = Field(default='', max_length=5000)


class CreateTask(Model):
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(default="", max_length=100000)
    provider: Literal["claude", "codex", "demo"] | None = None
    repo_path: str | None = None
    state: Literal["planning", "todo"] = "planning"


class UpdateTask(Model):
    title: str | None = Field(default=None, max_length=500)
    description: str | None = Field(default=None, max_length=100000)
    provider: Literal["claude", "codex", "demo"] | None = None
    repo_path: str | None = None
    expected_version: int | None = None


class MoveTask(Model):
    state: str
    comment: str | None = Field(default=None, max_length=100000)
    expected_version: int | None = None


class Comment(Model):
    body: str = Field(min_length=1, max_length=100000)
    in_reply_to: str | None = None
    run_id: str | None = None
    resume: bool = False


class EditComment(Model):
    body: str = Field(min_length=1, max_length=100000)


class InputRequest(Model):
    question: str = Field(min_length=1, max_length=100000)
    run_id: str | None = None


class ReviewSubmission(Model):
    summary: str = Field(min_length=1, max_length=100000)
    output_url: str = ""
    kind: Literal["text", "pr", "file", "url"] = "text"
    run_id: str | None = None


class ChangeRequest(Model):
    feedback: str = Field(min_length=1, max_length=100000)


class Subscription(Model):
    url: str
    types: list[str] = Field(default_factory=lambda: ["task.state_changed", "comment.*"])
    secret: str = ""


class LinearImport(Model):
    issue_id: str = Field(min_length=1)
    provider: Literal["claude", "codex", "demo"] | None = None
    repo_path: str | None = None
    question: str | None = None


class LinearDiscover(Model):
    api_key: SecretStr = SecretStr('')


class LinearConfigure(LinearDiscover):
    team_id: str
    states: dict[str, str] | None = None
    create_missing: bool = False
    provider: Literal['codex', 'claude', 'demo'] = 'codex'


class LinearWebhook(Model):
    url: str


def create_app(settings: Settings | None = None, *, start_worker: bool = True,
               hooks_only: bool = False, runners: dict | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.load_linear()
    settings.validate()
    store = Store(settings.db_path)
    service = TaskService(store, settings)
    worker = Worker(service, runners=runners)

    @asynccontextmanager
    async def lifespan(app):
        if start_worker and not hooks_only:
            await worker.start()
        try:
            yield
        finally:
            if start_worker and not hooks_only:
                await worker.stop()

    app = FastAPI(title="ACP" if not hooks_only else "ACP signed webhooks",
                  version=__version__, lifespan=lifespan, docs_url=None if hooks_only else "/docs",
                  redoc_url=None, openapi_url=None if hooks_only else "/openapi.json")
    app.state.service, app.state.worker, app.state.settings = service, worker, settings

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        if request.url.path.startswith('/api/integrations/linear/'):
            return JSONResponse({'detail': 'Invalid Linear setup fields. Check the field types and required values.'}, status_code=422)
        return await request_validation_exception_handler(request, exc)

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        # This isn't a users/permissions system. Keep the unauthenticated local app
        # unreachable from another website; expose only the separate signed-hook app.
        if not hooks_only and request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            allowed = {settings.base_url, str(request.base_url).rstrip("/")}
            if origin and origin.rstrip("/") not in allowed:
                return JSONResponse({"detail": "Cross-origin writes are not accepted"}, status_code=403)
        return await call_next(request)

    if not hooks_only:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver", "[::1]"])

    async def webhook_body(request: Request) -> tuple[bytes, dict]:
        # Enforce a size cap while streaming rather than trusting Content-Length.
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > 4 * 1024 * 1024:
                raise DomainError("Webhook payload is too large", 413)
            chunks.append(chunk)
        raw = b"".join(chunks)
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise DomainError("Expected a JSON webhook", 400)
        if not isinstance(data, dict):
            raise DomainError("Expected a JSON object", 400)
        return raw, data

    @app.post("/hooks/github", status_code=202)
    async def github_hook(request: Request):
        raw, data = await webhook_body(request)
        verify_signature(raw, settings.github_webhook_secret, request.headers.get("x-hub-signature-256", ""), "sha256=")
        delivery_id = request.headers.get("x-github-delivery")
        if not delivery_id:
            raise DomainError("Missing GitHub delivery ID", 400)
        event = request.headers.get("x-github-event", "")
        if event not in {"pull_request_review", "pull_request"}:
            return {"accepted": True, "ignored": True}
        return service.receive("github", delivery_id, {"event": event, "payload": data}, "github.incoming")

    @app.post("/hooks/linear", status_code=200)
    async def linear_hook(request: Request):
        settings.load_linear()
        raw, data = await webhook_body(request)
        verify_signature(raw, settings.linear_webhook_secret, request.headers.get("linear-signature", ""))
        if not settings.linear_workspace_id or data.get("organizationId") != settings.linear_workspace_id:
            raise DomainError("Webhook is not from the configured workspace", 422)
        try:
            age = abs(time.time() - float(data["webhookTimestamp"]) / 1000)
        except (KeyError, TypeError, ValueError):
            raise DomainError("Missing or invalid Linear webhook timestamp", 400)
        if not math.isfinite(age) or age > settings.webhook_max_age_seconds:
            raise DomainError("Expired Linear webhook", 401)
        delivery_id = request.headers.get("linear-delivery")
        if not delivery_id:
            raise DomainError("Missing Linear delivery ID", 400)
        return service.receive("linear", delivery_id, data, "linear.incoming")

    if hooks_only:
        return app

    from .desktop import DesktopBridge
    desktop = DesktopBridge(service)

    @app.get('/api/desktop')
    def desktop_status():
        return desktop.status()

    @app.post('/api/desktop/connection')
    def desktop_connect(data: DesktopConnection):
        return desktop.connect(data.thread_id)

    @app.post('/api/desktop/runs/{run_id}/{operation}')
    async def desktop_run(run_id: str, operation: str, data: DesktopRequest):
        if operation == 'claim':
            return await asyncio.to_thread(desktop.claim, run_id)
        if operation == 'prepare':
            return await desktop.prepare(run_id, data.repo)
        if operation in {'bind', 'started'}:
            return getattr(desktop, operation)(run_id, data.thread_id)
        if operation in {'complete', 'acknowledge'}:
            return desktop.complete(run_id, data.thread_id, data.error)
        if operation == 'action':
            return desktop.action(run_id, data.thread_id, data.action, data.text, data.output_url, data.kind)
        raise DomainError('Unknown desktop operation', 404)

    @app.get("/api/health")
    def health():
        failed = store.one("SELECT count(*) n FROM outbox WHERE status='failed'")["n"]
        return {"version": __version__, "worker_running": bool(worker.tasks),
                "worker_error": worker.last_error, "failed_deliveries": failed}

    @app.get("/api/config")
    def config():
        linear_saved = settings.load_linear()
        return {"states": [{"id": s, "label": label} for s, label in zip(STATES, LABELS)],
                "providers": list(PROVIDERS), "default_provider": settings.default_provider,
                "default_repo": settings.default_repo, "linear_enabled": settings.linear_enabled,
                "linear_user_name": linear_saved.get('user_name', ''),
                "github_enabled": bool(settings.github_webhook_secret), "codex_desktop": desktop.status()["connected"]}

    @app.get("/api/tasks")
    def tasks():
        return service.list_tasks()

    @app.post("/api/tasks", status_code=201)
    def create_task(body: CreateTask):
        return service.create_task(**body.model_dump())

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: str):
        return service.get_task(task_id)

    @app.patch("/api/tasks/{task_id}")
    def update_task(task_id: str, body: UpdateTask):
        return service.update_task(task_id, **body.model_dump(exclude_unset=True))

    @app.post("/api/tasks/{task_id}/transition")
    def transition(task_id: str, body: MoveTask):
        return service.move_task(task_id, **body.model_dump())

    @app.post("/api/tasks/{task_id}/comments", status_code=201)
    def add_comment(task_id: str, body: Comment):
        return service.add_comment(task_id, **body.model_dump())

    @app.patch("/api/tasks/{task_id}/comments/{comment_id}")
    def edit_comment(task_id: str, comment_id: str, body: EditComment):
        return service.edit_comment(task_id, comment_id, body.body)

    @app.delete("/api/tasks/{task_id}/comments/{comment_id}")
    def delete_comment(task_id: str, comment_id: str):
        return service.edit_comment(task_id, comment_id, delete=True)

    @app.post("/api/tasks/{task_id}/request-input")
    def request_input(task_id: str, body: InputRequest):
        return service.request_input(task_id, **body.model_dump())

    @app.post("/api/tasks/{task_id}/submit-for-review")
    def submit_for_review(task_id: str, body: ReviewSubmission):
        return service.submit_for_review(task_id, **body.model_dump())

    @app.post("/api/tasks/{task_id}/request-changes", status_code=202)
    def request_changes(task_id: str, body: ChangeRequest):
        return service.request_changes(task_id, body.feedback)

    @app.post("/api/tasks/{task_id}/cancel")
    def cancel(task_id: str):
        return service.cancel(task_id)

    @app.post("/api/tasks/{task_id}/reset-session")
    def reset_session(task_id: str):
        return service.reset_session(task_id)

    @app.get("/api/runs/{run_id}/log", response_class=PlainTextResponse)
    def run_log(run_id: str):
        if not store.one("SELECT 1 FROM runs WHERE id=?", (run_id,)):
            raise DomainError("Run not found", 404)
        path = settings.data_dir / "logs" / f"{run_id}.jsonl"
        if not path.exists():
            return "No subprocess log. Demo runs do not launch an external provider."
        with path.open("rb") as f:
            length = f.seek(0, 2)
            if length > 512000:
                f.seek(-512000, 2)
                f.readline()
            else:
                f.seek(0)
            return f.read().decode(errors="replace")

    @app.get("/api/events")
    def events(after: int = Query(0, ge=0), task_id: str | None = None, limit: int = Query(200, ge=1, le=1000)):
        return store.events(after, task_id, limit)

    @app.get("/api/events/stream")
    async def event_stream(request: Request, after: int = Query(0, ge=0), task_id: str | None = None):
        try:
            cursor = max(after, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            raise DomainError("Invalid Last-Event-ID", 400)

        async def generate():
            nonlocal cursor
            heartbeat = time.monotonic()
            while not getattr(app.state, "stopping", False) and not await request.is_disconnected():
                batch = store.events(cursor, task_id)
                for event in batch:
                    cursor = event["id"]
                    yield f"id: {cursor}\nevent: board\ndata: {json.dumps(event)}\n\n"
                if time.monotonic() - heartbeat > 15:
                    yield ": keepalive\n\n"
                    heartbeat = time.monotonic()
                await asyncio.sleep(0.3)
        return StreamingResponse(generate(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/subscriptions")
    def subscriptions():
        subs = store.all("SELECT id,url,types,created_at FROM subscriptions ORDER BY created_at")
        for sub in subs:
            sub["types"] = json.loads(sub["types"])
        return subs

    @app.post("/api/subscriptions", status_code=201)
    def subscribe(body: Subscription):
        return service.subscribe(**body.model_dump())

    @app.delete("/api/subscriptions/{subscription_id}")
    def unsubscribe(subscription_id: str):
        with store.transaction() as db:
            db.execute("DELETE FROM subscriptions WHERE id=?", (subscription_id,))
        return {"deleted": True}

    @app.get("/api/deliveries")
    def deliveries(status: Literal["pending", "working", "failed", "done"] | None = None):
        if status:
            return store.all("SELECT * FROM outbox WHERE status=? ORDER BY id DESC LIMIT 200", (status,))
        return store.all("SELECT * FROM outbox ORDER BY id DESC LIMIT 200")

    @app.post("/api/deliveries/{delivery_id}/retry")
    def retry_delivery(delivery_id: int):
        with store.transaction() as db:
            item = db.execute("SELECT status FROM outbox WHERE id=?", (delivery_id,)).fetchone()
            if not item:
                raise DomainError("Delivery not found", 404)
            if item[0] == "working":
                raise DomainError("Delivery is already in progress")
            db.execute("UPDATE outbox SET status='pending',attempts=0,available_at=?,last_error=NULL WHERE id=?",
                       (time.time(), delivery_id))
        return {"queued": True}

    from .linear_setup import LinearSetup
    linear_setup = LinearSetup(service)

    @app.get('/api/integrations/linear/status')
    def linear_status():
        return linear_setup.status()

    @app.post('/api/integrations/linear/discover')
    async def linear_discover(body: LinearDiscover):
        return await linear_setup.discover(body.api_key.get_secret_value())

    @app.post('/api/integrations/linear/configure')
    async def linear_configure(body: LinearConfigure):
        return await linear_setup.configure(body.api_key.get_secret_value(), body.team_id,
                                            body.states, body.create_missing, body.provider)

    @app.post('/api/integrations/linear/webhook')
    async def linear_webhook(body: LinearWebhook):
        return await linear_setup.webhook(body.url)

    @app.post("/api/integrations/linear/import", status_code=201)
    async def import_linear(body: LinearImport):
        async with httpx.AsyncClient(timeout=20) as client:
            try:
                return await Integrations(service, client).import_linear(**body.model_dump())
            except httpx.HTTPError as exc:
                raise DomainError(f"Linear request failed: {exc}", 502)
            except RuntimeError as exc:
                raise DomainError(str(exc), 502)

    @app.post("/api/integrations/linear/reconcile", status_code=202)
    def reconcile_linear():
        if not settings.linear_api_key:
            raise DomainError("Linear is not configured", 503)
        if settings.linear_label_sync:
            with store.transaction() as db:
                Store.enqueue(db, 'linear.scan', f'scan:{time.time_ns()}', {})
            return {'queued': 1}
        linked = store.all("SELECT linear_id FROM tasks WHERE linear_id IS NOT NULL")
        with store.transaction() as db:
            for task in linked:
                Store.enqueue(db, "linear.incoming", f"reconcile:{task['linear_id']}:{time.time_ns()}",
                              {"organizationId": settings.linear_workspace_id, "type": "Issue", "action": "update",
                               "data": {"id": task["linear_id"]}})
        return {"queued": len(linked)}

    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/")
    def index():
        return FileResponse(static / "index.html")

    return app
