from __future__ import annotations

import asyncio
import fcntl
import logging
import time

import httpx

from .integrations import Integrations
from .runners import CLIRunner, DemoRunner
from .desktop import DesktopRunner
from .service import TaskService
from .store import row
from .retries import retry_delay

logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, service: TaskService, runners: dict | None = None):
        self.service, self.settings = service, service.settings
        self.runners = runners or {"claude": CLIRunner(), "codex": DesktopRunner(), "demo": DemoRunner()}
        self.tasks: list[asyncio.Task] = []
        self.lock = None
        self.last_error: str | None = None

    async def start(self) -> None:
        self.lock = (self.settings.data_dir / "worker.lock").open("a")
        try:
            fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            self.lock = None
            raise RuntimeError("Another ACP worker is already using this database; run one server worker only.")
        retained = self.service.recover()
        self.tasks = [asyncio.create_task(self.run_loop(retained[i] if i < len(retained) else None),
                                         name=f"agent-slot-{i}")
                      for i in range(max(self.settings.max_concurrent_runs, len(retained)))]
        self.tasks.append(asyncio.create_task(self.delivery_loop(), name="event-deliveries"))

    async def stop(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        if self.lock:
            fcntl.flock(self.lock.fileno(), fcntl.LOCK_UN)
            self.lock.close()
            self.lock = None

    async def run_once(self, retained=None) -> bool:
        run = retained or self.service.claim_next()
        if not run:
            return False
        task = self.service.get_task(run["task_id"])
        try:
            await self.runners[task["provider"]].run(self.service, task, run)
        except asyncio.CancelledError:
            if not self.service.resumable_desktop(run["id"]):
                self.service.finish_run(run["id"], "interrupted", "Server stopped. Review the retained worktree and retry.")
            raise
        except Exception as exc:
            logger.exception("Run %s failed", run["id"])
            self.service.finish_run(run["id"], "failed", str(exc))
        else:
            self.service.finish_run(run["id"])
        return True

    async def run_loop(self, retained=None) -> None:
        if retained:
            await self.run_once(retained)
        while True:
            try:
                worked = await self.run_once()
                if not worked:
                    await asyncio.sleep(self.settings.poll_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)
                logger.exception("Scheduler iteration failed")
                await asyncio.sleep(2)

    async def deliver_once(self, integrations: Integrations) -> bool:
        with self.service.store.transaction() as db:
            item = row(db.execute("SELECT * FROM outbox WHERE status='pending' AND available_at<=? "
                                  "ORDER BY id LIMIT 1", (time.time(),)).fetchone())
            if not item:
                return False
            db.execute("UPDATE outbox SET status='working' WHERE id=?", (item["id"],))
        try:
            await integrations.deliver(item)
        except asyncio.CancelledError:
            with self.service.store.transaction() as db:
                db.execute("UPDATE outbox SET status='pending' WHERE id=?", (item["id"],))
            raise
        except Exception as exc:
            attempts = item["attempts"] + 1
            delay = retry_delay(exc, attempts)
            failed = delay is None or attempts >= self.settings.max_delivery_attempts
            with self.service.store.transaction() as db:
                db.execute("UPDATE outbox SET status=?,attempts=?,available_at=?,last_error=? WHERE id=?",
                           ("failed" if failed else "pending", attempts,
                            time.time() + (delay or 0), str(exc), item["id"]))
                if failed and item["kind"] != "webhook":
                    self.service._event(db, "integration.failed", None,
                                        {"delivery_id": item["id"], "kind": item["kind"], "error": str(exc)})
            logger.warning("Delivery %s failed: %s", item["id"], exc)
        else:
            with self.service.store.transaction() as db:
                db.execute("UPDATE outbox SET status='done',last_error=NULL WHERE id=?", (item["id"],))
        return True

    async def delivery_loop(self) -> None:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            integrations = Integrations(self.service, client)
            next_linear_scan = 0
            while True:
                try:
                    if self.settings.linear_label_sync and time.monotonic() >= next_linear_scan:
                        try:
                            await integrations.sync_labelled()
                        except Exception:
                            with self.service.store.transaction() as db:
                                db.execute("INSERT OR REPLACE INTO metadata VALUES('linear_sync_error',?)",
                                           ('Linear label sync failed. Check credentials, team access and connection.',))
                        else:
                            with self.service.store.transaction() as db:
                                db.execute("DELETE FROM metadata WHERE key='linear_sync_error'")
                                db.execute("INSERT OR REPLACE INTO metadata VALUES('linear_last_sync',?)", (str(time.time()),))
                        next_linear_scan = time.monotonic() + 60
                    if not await self.deliver_once(integrations):
                        await asyncio.sleep(self.settings.poll_seconds)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.last_error = str(exc)
                    logger.exception("Delivery iteration failed")
                    await asyncio.sleep(2)
