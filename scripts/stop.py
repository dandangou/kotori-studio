#!/usr/bin/env python3
"""Gracefully stop only the server started by this project's launcher."""
from __future__ import annotations

import os
import signal
import subprocess
import time

from launch import ROOT, server_state


def main() -> None:
    pidfile = ROOT / "data" / "logs" / "server.pid"
    if server_state() != "ours":
        print("此目录的烤肉工房没有运行。")
        return
    try:
        pid = int(pidfile.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        raise SystemExit("没有此启动器保存的进程信息；请在启动服务的终端按 Control-C。")
    command = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, check=False).stdout
    if str(ROOT / ".venv" / "bin" / "python") not in command or "app.main:app" not in command:
        raise SystemExit("进程信息不匹配，未停止任何程序。")
    os.kill(pid, signal.SIGTERM)
    for _ in range(40):
        if server_state() != "ours":
            pidfile.unlink(missing_ok=True)
            print("烤肉工房已停止。")
            return
        time.sleep(0.25)
    print("已请求停止，正在等待当前服务退出。")


if __name__ == "__main__":
    main()
