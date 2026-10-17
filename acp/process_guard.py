"""Local process guard: kill the agent group if the owning server disappears.

The server holds the write end of an anonymous pipe. A crash closes it, so this
small non-agent wrapper can reap descendants instead of leaving a paid run alive.
Linux/macOS/WSL only. This is process cleanup, not a workflow manager.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading


def main() -> None:
    fd = int(sys.argv[1])

    def watch_parent() -> None:
        try:
            while os.read(fd, 1):
                pass
        finally:
            os.killpg(os.getpgrp(), signal.SIGKILL)

    threading.Thread(target=watch_parent, daemon=True).start()
    try:
        child = subprocess.Popen(sys.argv[2:], stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr)
    except OSError as exc:
        print(json.dumps({"type": "acp.launch_failed", "message": str(exc)}), flush=True)
        raise SystemExit(127)
    print(json.dumps({"type": "acp.process_started", "pid": child.pid}), flush=True)
    raise SystemExit(child.wait())


if __name__ == "__main__":
    main()
