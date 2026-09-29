#!/usr/bin/env python3
"""Desktop launcher for the packaged MediaHarvest app.

This entry point is intentionally small: the real product remains the existing
Flask web UI. The launcher only prepares user-writable paths, starts the local
server, and opens the browser.
"""
from __future__ import annotations

import os
import socket
import sys
import webbrowser
from pathlib import Path


APP_NAME = "MediaHarvest"
DEFAULT_PORT = 8848


def app_support_dir() -> Path:
    """Return the per-user writable app support directory."""
    override = os.environ.get("MEDIAHARVEST_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home())
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / APP_NAME


def port_is_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.35):
            return True
    except OSError:
        return False


def prepare_environment() -> Path:
    support = app_support_dir()
    state_dir = support / "state"
    config_dir = support / "config"
    downloads = Path.home() / "Downloads" / "MediaHarvest"
    logs = support / "logs"

    try:
        for path in (state_dir, config_dir, downloads, logs):
            path.mkdir(parents=True, exist_ok=True)
    except OSError:
        support = Path(os.environ.get("TMPDIR") or "/tmp") / APP_NAME
        state_dir = support / "state"
        config_dir = support / "config"
        logs = support / "logs"
        for path in (state_dir, config_dir, logs):
            path.mkdir(parents=True, exist_ok=True)

    config_path = config_dir / "config.toml"
    if not config_path.exists():
        config_path.write_text(
            "[download]\n"
            f'out_dir = "{downloads.as_posix()}"\n\n'
            "[web]\n"
            'host = "127.0.0.1"\n'
            "port = 8848\n"
            "open_browser = true\n",
            encoding="utf-8",
        )

    os.environ.setdefault("MEDIAHARVEST_CONFIG", str(config_path))
    os.environ.setdefault("MEDIAHARVEST_STATE_DIR", str(state_dir))
    os.environ.setdefault("MEDIAHARVEST_DESKTOP_LOG_DIR", str(logs))

    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        browser_candidates = [
            exe_dir / ".playwright-browsers",
            exe_dir / "_internal" / ".playwright-browsers",
        ]
        if sys.platform == "darwin":
            browser_candidates.append(exe_dir.parent / "Resources" / ".playwright-browsers")
        for bundled_browsers in browser_candidates:
            if bundled_browsers.exists():
                os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(bundled_browsers))
                break

    return logs


def main() -> int:
    logs = prepare_environment()
    log_file = logs / "desktop.log"
    url = f"http://127.0.0.1:{DEFAULT_PORT}"

    if port_is_open("127.0.0.1", DEFAULT_PORT):
        webbrowser.open(url)
        return 0

    try:
        from mediaharvest.web import main as web_main
    except Exception as exc:
        log_file.write_text(f"Failed to import mediaharvest.web: {exc}\n", encoding="utf-8")
        raise

    with log_file.open("a", encoding="utf-8") as handle:
        handle.write("\n==> Starting MediaHarvest desktop server\n")
        handle.flush()

    return int(web_main([]) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
