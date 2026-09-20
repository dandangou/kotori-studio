#!/usr/bin/env python3
"""Start/reuse this project's local server, with visible errors and bounded logs."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
URL = "http://127.0.0.1:8765"


def server_state() -> str:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(URL + "/api/system", timeout=2) as response:
            info = json.load(response)
        if isinstance(info, dict) and info.get("app_id") == "yakuniku-workshop" and isinstance(info.get("data_dir"), str) and Path(info["data_dir"]).resolve() == (ROOT / "data").resolve():
            return "ours"
        return "other"
    except (OSError, ValueError, urllib.error.URLError):
        with socket.socket() as sock:
            sock.settimeout(0.25)
            return "other" if sock.connect_ex(("127.0.0.1", 8765)) == 0 else "empty"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    logs = ROOT / "data" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    with (logs / "launch.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = server_state()
        if state == "other":
            print("端口 8765 已被其他程序或其他目录的烤肉工房占用。请先关闭该程序后重试。", file=sys.stderr)
            return 1
        if state == "empty":
            env = os.environ.copy()
            env.update({
                "PATH": str(ROOT / ".runtime" / "bin") + os.pathsep + "/opt/homebrew/bin" + os.pathsep + env.get("PATH", ""),
                "FFMPEG_BINARY": str(ROOT / ".runtime" / "bin" / "ffmpeg"),
                "FFPROBE_BINARY": str(ROOT / ".runtime" / "bin" / "ffprobe"),
                "HF_HOME": str(ROOT / "data" / "models"),
                "HF_HUB_CACHE": str(ROOT / "data" / "models" / "hub"),
                "HF_HUB_DISABLE_TELEMETRY": "1",
                "TOKENIZERS_PARALLELISM": "false",
                "PYTHONUNBUFFERED": "1",
            })
            logfile = logs / "server.log"
            if logfile.exists() and logfile.stat().st_size > 5 * 1024**2:
                logfile.replace(logs / "server.previous.log")
            with logfile.open("ab", buffering=0) as output:
                process = subprocess.Popen(
                    [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8765", "--no-access-log"],
                    cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                    start_new_session=True,
                )
            (logs / "server.pid").write_text(str(process.pid), encoding="utf-8")
            for _ in range(80):
                if server_state() == "ours":
                    break
                if process.poll() is not None:
                    print(f"启动失败，日志：{logfile}\n" + logfile.read_text(errors="replace")[-4000:], file=sys.stderr)
                    return 1
                time.sleep(0.25)
            else:
                print(f"启动等待超时，请查看日志：{logfile}", file=sys.stderr)
                return 1
    if not args.no_browser:
        webbrowser.open(URL)
    print(f"烤肉工房已启动：{URL}\n日志：{logs / 'server.log'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
