# ACP installed service

Implement the requested one-command GitHub Release installer and browser-based background service on macOS/Linux. Retain uv for packaging and the existing task engine. No desktop wrapper or login daemon in this version.

1. Use stable user configuration and data paths, with explicit ACP_HOME / ACP_CONFIG_FILE / ACP_DATA_DIR overrides. Never discover arbitrary working-directory .env files in an installed command. Preserve developer setup through explicit config selection.
2. Add setup/start/stop/status/open/logs commands. A detached Python child owns an exclusive service lock, binds localhost, and publishes private instance metadata. Authenticate service control with a random token; never signal a PID from stale metadata. Close event streams on shutdown and wait for worker/process cleanup. Detect occupied ports and duplicate workers.
3. Build wheel, locked runtime constraints, version, checksums and installer assets. Publish only on matching v-prefixed tags through GitHub Actions. Installer downloads release assets, verifies checksums, bootstraps uv if absent, installs into a user tool environment, and initializes configuration without overwriting it.
4. Test real service lifecycle, concurrent start, occupied port, unauthorized stop, stale metadata, persistence, active stream shutdown, and task input/review loops. Exercise the actual installer over HTTP with built release assets and isolated tool directories; then run an authenticated Codex task using the installed executable.
5. Review and run full checks. Document the install command, upgrade/uninstall, storage, logs, background lifecycle and release publishing. Keep local asset verification distinct from a published GitHub release.

Review focus: child environment must use installed modules and absolute paths; shell installers must work when piped to sh; upgrades must not replace a running environment; configuration must survive reinstalls; task subprocesses and browser streams must not prevent stop.
