from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

STATES = ("planning", "todo", "in_progress", "waiting_on_input", "ready_for_review", "deploy", "done")
LABELS = ("Planning", "Todo", "In Progress", "Waiting on Input", "Ready for Review", "Deploy", "Done")
# "demo" is a hidden engine provider kept for tests and the release smoke test.
PROVIDERS = ("claude", "codex", "demo")
UI_PROVIDERS = ("claude", "codex")


def user_home() -> Path:
    data_root = Path(os.getenv("XDG_DATA_HOME", str(Path.home() / ".local/share")))
    return Path(os.getenv("ACP_HOME", str(data_root / "acp"))).expanduser().resolve()


def config_path() -> Path:
    return Path(os.getenv("ACP_CONFIG_FILE", str(user_home() / "config.env"))).expanduser().resolve()


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(".acp").resolve())
    base_url: str = "http://127.0.0.1:8765"
    default_provider: str = "demo"  # in-code default for the test suite; users get "claude" from from_env()
    default_repo: str = ""
    repo_search_roots: list[str] = field(default_factory=lambda: [str(Path.home() / name) for name in
                                                               ('Documents', 'Projects', 'Developer', 'code', 'src')])
    max_concurrent_runs: int = 1
    run_timeout_seconds: float = 1800
    poll_seconds: float = 0.5
    claude_command: str = "claude"
    codex_command: str = "codex"
    claude_model: str = ""
    codex_model: str = ""
    claude_run_mode: str = ""  # background | headless; empty probes `claude --help` for --bg
    codex_approvals: str = "acp"  # acp | auto_review | never
    codex_run_mode: str = ""  # app-server | exec; empty probes `codex app-server` once
    linear_api_key: str = ""
    linear_workspace_id: str = ""
    linear_team_id: str = ""
    linear_webhook_secret: str = ""
    linear_states: dict[str, str] = field(default_factory=dict)
    linear_label_sync: bool = False
    linear_provider: str = 'codex'
    linear_user_id: str = ''
    github_webhook_secret: str = ""
    github_token: str = ""
    max_delivery_attempts: int = 8
    webhook_max_age_seconds: int = 60
    overlay_enabled: bool = True  # ACP_OVERLAY=0 keeps Comet out of acp start on macOS

    @property
    def db_path(self) -> Path:
        return self.data_dir / "board.sqlite3"

    @property
    def linear_enabled(self) -> bool:
        return bool(self.linear_api_key and self.linear_workspace_id and self.linear_team_id and self.linear_states)

    def load_linear(self) -> dict:
        """App-managed settings override the legacy environment configuration."""
        path = self.data_dir.expanduser().resolve() / "linear.json"
        if not path.exists():
            return {}
        data = json.loads(path.read_text())
        for name in ("linear_api_key", "linear_workspace_id", "linear_team_id",
                     "linear_webhook_secret", "linear_states", "linear_label_sync", "linear_provider", "linear_user_id"):
            if name in data:
                setattr(self, name, data[name])
        return data

    def validate(self) -> None:
        if self.default_provider not in PROVIDERS:
            raise ValueError(f"DEFAULT_PROVIDER must be one of {PROVIDERS}")
        if self.max_concurrent_runs < 1 or self.run_timeout_seconds <= 0 or self.poll_seconds <= 0:
            raise ValueError("Concurrency, timeouts and polling interval must be positive")
        if self.claude_run_mode not in ("", "background", "headless"):
            raise ValueError("CLAUDE_RUN_MODE must be background or headless")
        if self.codex_approvals not in {"acp", "auto_review", "never"}:
            raise ValueError("CODEX_APPROVALS must be acp, auto_review, or never")
        if self.codex_run_mode not in {"", "app-server", "exec"}:
            raise ValueError("CODEX_RUN_MODE must be app-server or exec")
        if self.linear_states:
            if set(self.linear_states) != set(STATES):
                raise ValueError("LINEAR_STATES_JSON must map all seven internal state names to Linear state IDs")
            if len(set(self.linear_states.values())) != len(STATES):
                raise ValueError("Each lane must map to a different Linear state")
        if (self.linear_states or self.linear_team_id or self.linear_workspace_id) and not self.linear_enabled:
            raise ValueError("Linear requires one workspace ID, one team ID, and the seven-state mapping")
        self.data_dir = self.data_dir.expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls) -> Settings:
        path = config_path()
        configured = {key: value for key, value in dotenv_values(path).items() if value is not None}
        for key in ("ACP_DATA_DIR", "DEFAULT_REPO", "CODEX_COMMAND", "CLAUDE_COMMAND"):
            value = configured.get(key)
            if value and (key in {"ACP_DATA_DIR", "DEFAULT_REPO"} or "/" in value):
                location = Path(value).expanduser()
                configured[key] = str((path.parent / location).resolve())
        values = {**configured, **os.environ}
        getenv = values.get
        s = cls(
            data_dir=Path(getenv("ACP_DATA_DIR", str(user_home() / "data"))),
            base_url=getenv("ACP_URL", "http://127.0.0.1:8765").rstrip("/"),
            default_provider=getenv("DEFAULT_PROVIDER", "claude"),
            default_repo=getenv("DEFAULT_REPO", ""),
            max_concurrent_runs=int(getenv("MAX_CONCURRENT_RUNS", "1")),
            run_timeout_seconds=float(getenv("RUN_TIMEOUT_SECONDS", "1800")),
            poll_seconds=float(getenv("POLL_SECONDS", "0.5")),
            claude_command=getenv("CLAUDE_COMMAND", "claude"),
            codex_command=getenv("CODEX_COMMAND", "codex"),
            claude_model=getenv("CLAUDE_MODEL", ""),
            codex_model=getenv("CODEX_MODEL", ""),
            claude_run_mode=getenv("CLAUDE_RUN_MODE", "").strip().lower(),
            codex_approvals=getenv("CODEX_APPROVALS", "acp"),
            codex_run_mode=getenv("CODEX_RUN_MODE", ""),
            linear_api_key=getenv("LINEAR_API_KEY", ""),
            linear_workspace_id=getenv("LINEAR_WORKSPACE_ID", ""),
            linear_team_id=getenv("LINEAR_TEAM_ID", ""),
            linear_webhook_secret=getenv("LINEAR_WEBHOOK_SECRET", ""),
            linear_states=json.loads(getenv("LINEAR_STATES_JSON", "{}")),
            github_webhook_secret=getenv("GITHUB_WEBHOOK_SECRET", ""),
            github_token=getenv("GITHUB_TOKEN", ""),
            overlay_enabled=getenv("ACP_OVERLAY", "1") != "0",
        )
        s.load_linear()
        if getenv('REPO_SEARCH_ROOTS') is not None:
            s.repo_search_roots = [p for p in getenv('REPO_SEARCH_ROOTS').split(os.pathsep) if p]
        s.validate()
        return s
