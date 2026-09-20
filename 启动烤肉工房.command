#!/bin/bash
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"
if [[ ! -x .venv/bin/python || ! -x .runtime/bin/ffmpeg || ! -x .runtime/bin/ffprobe ]]; then
  echo "首次运行：正在准备项目内的本地环境。"
  /bin/bash scripts/setup.sh
fi
if ! .venv/bin/python scripts/launch.py; then
  echo "启动失败。按回车关闭窗口。"
  read -r
  exit 1
fi
