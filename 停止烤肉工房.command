#!/bin/bash
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"
if [[ ! -x .venv/bin/python ]]; then
  echo "本地运行环境尚未安装，无需停止。"
  exit 0
fi
if ! .venv/bin/python scripts/stop.py; then
  echo "未能自动停止。按回车关闭窗口。"
  read -r
  exit 1
fi
