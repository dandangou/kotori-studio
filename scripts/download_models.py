#!/usr/bin/env python3
"""Explicit, resumable model downloads. No media or transcript is uploaded."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "data" / "models" / "hub"
MODELS = {
    "turbo": "mlx-community/whisper-large-v3-turbo",
    "large-v3": "mlx-community/whisper-large-v3-mlx",
    "qwen3-4b": "mlx-community/Qwen3-4B-Instruct-2507-4bit",
}


def main() -> None:
    parser = argparse.ArgumentParser(description="下载烤肉工房离线模型；已有文件自动复用，失败后可重试。")
    parser.add_argument("models", nargs="*", metavar="MODEL", help="turbo / large-v3 / qwen3-4b；默认下载 turbo 和 qwen3-4b")
    parser.add_argument("--endpoint", default=os.environ.get("HF_ENDPOINT", "https://huggingface.co"), help="模型下载站点，可自行指定可信镜像，例如 https://hf-mirror.com")
    args = parser.parse_args()
    args.models = args.models or ["turbo", "qwen3-4b"]
    if any(model not in MODELS for model in args.models):
        parser.error("模型必须是 turbo、large-v3 或 qwen3-4b。")
    CACHE.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("HF_HOME", str(ROOT / "data" / "models"))
    os.environ.setdefault("HF_HUB_CACHE", str(CACHE))
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "120")
    os.environ["HF_ENDPOINT"] = args.endpoint
    if args.endpoint != "https://huggingface.co":
        os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    from huggingface_hub import HfApi, snapshot_download

    for model in args.models:
        repo = MODELS[model]
        info = HfApi(endpoint=args.endpoint, token=False).model_info(repo, files_metadata=True)
        size = sum(f.size or 0 for f in info.siblings if not f.rfilename.startswith("."))
        free = shutil.disk_usage(ROOT).free
        if free < size + 5 * 1024 ** 3:
            raise SystemExit(f"空间不足：{model} 约需 {size / 1024**3:.2f} GiB，另保留 5 GiB。")
        print(f"下载 {model}：{repo}；约 {size / 1024**3:.2f} GiB", flush=True)
        started = time.monotonic()
        path = snapshot_download(repo, revision=info.sha, cache_dir=CACHE, max_workers=3, endpoint=args.endpoint, token=False)
        for file in info.siblings:
            lfs = file.lfs
            expected = (lfs.get("sha256") if isinstance(lfs, dict) else getattr(lfs, "sha256", None))
            if expected:
                print(f"校验 SHA256：{file.rfilename}", flush=True)
                with (Path(path) / file.rfilename).open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                if digest != expected:
                    raise RuntimeError(f"{file.rfilename} 校验失败；请勿使用此模型。")
        # A pinned snapshot is reproducible. Keep refs/main for the application's offline lookup.
        ref = CACHE / ("models--" + repo.replace("/", "--")) / "refs" / "main"
        ref.parent.mkdir(parents=True, exist_ok=True)
        ref.write_text(info.sha, encoding="utf-8")
        print(json.dumps({"model": model, "path": path, "revision": info.sha, "seconds": round(time.monotonic() - started, 1)}, ensure_ascii=False), flush=True)
    print("模型下载完成；之后可以断网进行识别、初译和语义选片。", flush=True)


if __name__ == "__main__":
    main()
