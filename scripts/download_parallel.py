#!/usr/bin/env python3
"""Resume a public model weight with validated HTTP ranges, then verify SHA256.

Normally use download_models.py. This helper is useful on links that throttle
each connection. It consumes an HF model API manifest, reuses HF partial files,
and never accepts a full-file response in place of a requested range.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shutil
import threading
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "data" / "models" / "hub"
# Published SHA256 verified on each original Hugging Face file page.
KNOWN_WEIGHTS = {
    "mlx-community/whisper-large-v3-turbo": ("weights.safetensors", "951ed3fc1203e6a62467abb2144a96ce7eafca8fa77e3704fdb8635ff3e7f8a6"),
    "mlx-community/Qwen3-4B-Instruct-2507-4bit": ("model.safetensors", "2a73c6c248601ab904e035548abd8e6abb65ea27dcb5f342fb0a8910eb44173f"),
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--endpoint", default="https://huggingface.co")
    parser.add_argument("--connections", type=int, default=4, choices=range(1, 9))
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    repo = manifest["id"]
    filename, expected_sha = KNOWN_WEIGHTS[repo]
    entry = next(f for f in manifest["siblings"] if f["rfilename"] == filename)
    if entry["lfs"]["sha256"] != expected_sha:
        raise SystemExit("清单权重校验值与官方已确认值不符，停止下载。")
    size = entry["size"]
    model_dir = CACHE / ("models--" + repo.replace("/", "--"))
    blobs = model_dir / "blobs"
    blobs.mkdir(parents=True, exist_ok=True)
    target = blobs / expected_sha
    parts = blobs / (expected_sha + ".ranges")
    parts.mkdir(exist_ok=True)
    plan_path = parts / "plan.json"
    if plan_path.exists():
        plan = json.loads(plan_path.read_text())
        partial = blobs / plan["partial"]
        prefix = plan["prefix"]
    else:
        existing = list(blobs.glob(expected_sha + "*.incomplete"))
        partial = max(existing, key=lambda f: f.stat().st_size) if existing else blobs / (expected_sha + ".parallel.incomplete")
        partial.touch(exist_ok=True)
        prefix = partial.stat().st_size
        plan_path.write_text(json.dumps({"partial": partial.name, "prefix": prefix}))
    if target.exists():
        with target.open("rb") as stream:
            assert hashlib.file_digest(stream, "sha256").hexdigest() == expected_sha
        print("权重已存在且 SHA256 正确。", flush=True)
        return
    if prefix > size or shutil.disk_usage(ROOT).free < size - prefix + 1024**3:
        raise SystemExit("断点文件异常或剩余磁盘空间不足。")
    url = f"{args.endpoint.rstrip('/')}/{repo}/resolve/{manifest['sha']}/{filename}"
    chunk_size = 32 * 1024**2
    chunks = [(start, min(size - 1, start + chunk_size - 1)) for start in range(prefix, size, chunk_size)]
    progress_lock = threading.Lock()
    transferred = 0
    started = time.monotonic()
    last_report = started
    print(f"{repo}: 保留 {partial.stat().st_size / 1024**2:.1f} MiB 断点，{args.connections} 路分段下载。", flush=True)

    def download(bounds: tuple[int, int]) -> Path:
        nonlocal transferred, last_report
        start, end = bounds
        piece = parts / f"{start}-{end}.part"
        expected_size = end - start + 1
        # Already appended chunks need not be fetched after a restart.
        if partial.stat().st_size >= end + 1:
            return piece
        for attempt in range(6):
            done = piece.stat().st_size if piece.exists() else 0
            if done == expected_size:
                return piece
            if done > expected_size:
                raise RuntimeError("分段断点大小异常。")
            actual_start = start + done
            try:
                with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(90, connect=20)) as client:
                    with client.stream("GET", url, headers={"Range": f"bytes={actual_start}-{end}", "Accept-Encoding": "identity"}) as response:
                        wanted = f"bytes {actual_start}-{end}/{size}"
                        if response.status_code != 206 or response.headers.get("content-range") != wanted:
                            raise RuntimeError(f"拒绝非预期范围响应：{response.status_code} {response.headers.get('content-range')}, 预期 {wanted}")
                        with piece.open("ab") as stream:
                            for data in response.iter_bytes(1024**2):
                                if stream.tell() + len(data) > expected_size:
                                    raise RuntimeError("范围响应超出预期长度。")
                                stream.write(data)
                                with progress_lock:
                                    transferred += len(data)
                                    now = time.monotonic()
                                    if now - last_report >= 10:
                                        total = prefix + transferred
                                        print(f"{total / size:.1%} · {total / 1024**2:.0f}/{size / 1024**2:.0f} MiB · {transferred / (now - started) / 1024**2:.2f} MiB/s", flush=True)
                                        last_report = now
                if piece.stat().st_size != expected_size:
                    raise RuntimeError("分段响应提前结束。")
                return piece
            except (httpx.HTTPError, RuntimeError, OSError) as exc:
                if attempt == 5:
                    raise
                print(f"分段 {start} 重试 {attempt + 1}: {type(exc).__name__}", flush=True)
                time.sleep(min(2**attempt, 10))
        raise RuntimeError("下载失败。")

    with ThreadPoolExecutor(max_workers=args.connections) as pool:
        futures = [(bounds, pool.submit(download, bounds)) for bounds in chunks]
        for (start, end), future in futures:
            piece = future.result()
            cursor = partial.stat().st_size
            if cursor < end + 1:
                if not start <= cursor <= end:
                    raise RuntimeError("分段合并顺序异常。")
                with partial.open("ab") as output, piece.open("rb") as source:
                    source.seek(cursor - start)
                    shutil.copyfileobj(source, output, 1024**2)
            piece.unlink(missing_ok=True)
    with partial.open("rb") as stream:
        actual_sha = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual_sha != expected_sha or partial.stat().st_size != size:
        raise RuntimeError("最终权重大小或 SHA256 不符，已保留断点但不会安装此权重。")
    partial.replace(target)
    snapshot = model_dir / "snapshots" / manifest["sha"]
    snapshot.mkdir(parents=True, exist_ok=True)
    link = snapshot / filename
    if not link.exists():
        link.symlink_to(os.path.relpath(target, snapshot))
    refs = model_dir / "refs"
    refs.mkdir(exist_ok=True)
    (refs / "main").write_text(manifest["sha"])
    report = {"repo": repo, "revision": manifest["sha"], "file": filename, "size": size, "sha256": actual_sha, "endpoint": args.endpoint, "seconds": round(time.monotonic() - started, 1)}
    (model_dir / "download-verification.json").write_text(json.dumps(report, indent=2))
    shutil.rmtree(parts)
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
