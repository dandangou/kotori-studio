#!/bin/bash
# All installed files remain in this project. Never changes system Python/Homebrew.
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"
mkdir -p .runtime/bin .runtime/downloads data/logs data/models/hub

if [[ ! -x .venv/bin/python ]]; then
  PYTHON_CANDIDATE=""
  for candidate in "$PROJECT_DIR/.runtime/python/bin/python3" "/opt/homebrew/bin/python3.12" "/opt/homebrew/bin/python3.13" "$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3" python3; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys,platform; sys.exit(not ((3,12) <= sys.version_info[:2] <= (3,13) and platform.machine() == "arm64"))' 2>/dev/null; then
      PYTHON_CANDIDATE="$candidate"
      break
    fi
  done
  if [[ "$PYTHON_CANDIDATE" == "$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3" ]]; then
    "$PYTHON_CANDIDATE" scripts/copy_python.py "$PYTHON_CANDIDATE" "$PROJECT_DIR/.runtime/python"
    PYTHON_CANDIDATE="$PROJECT_DIR/.runtime/python/bin/python3"
  fi
  if [[ -z "$PYTHON_CANDIDATE" ]]; then
    # Bootstrap uv with any available Python, then fetch a project-local Python 3.12.
    BOOTSTRAP_PYTHON="$(command -v python3 || true)"
    if [[ -z "$BOOTSTRAP_PYTHON" ]]; then
      echo "找不到 Python。请先安装 Python 3.12（Apple Silicon），再运行此脚本。"
      exit 1
    fi
    "$BOOTSTRAP_PYTHON" -m venv .runtime/bootstrap
    .runtime/bootstrap/bin/python -m pip install --no-cache-dir uv
    export UV_PYTHON_INSTALL_DIR="$PROJECT_DIR/.runtime/python"
    export UV_CACHE_DIR="$PROJECT_DIR/.runtime/uv-cache"
    .runtime/bootstrap/bin/uv python install 3.12
    PYTHON_CANDIDATE="$(.runtime/bootstrap/bin/uv python find --managed-python 3.12)"
  fi
  "$PYTHON_CANDIDATE" -m venv .venv
fi

echo "安装本地 Python 依赖……"
.venv/bin/python -m pip install --no-cache-dir -r requirements.txt
.venv/bin/python scripts/install_ffmpeg.py
.venv/bin/python -m pip check
echo "安装完成。双击 启动烤肉工房.command 即可打开。"
echo "可选：.venv/bin/python scripts/download_models.py turbo qwen3-4b （约 4 GB）"
