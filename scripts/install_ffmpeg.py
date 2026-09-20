#!/usr/bin/env python3
"""Install checksum-verified Apple Silicon FFmpeg into this project only."""
from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = {
    "ffmpeg": ("https://www.osxexperts.net/ffmpeg9arm.zip", "591260c945d0eef150e3bf82b0ef988bd36a9cecc18ff05d6679617159f0a95e"),
    "ffprobe": ("https://www.osxexperts.net/ffprobe9arm.zip", "e11c17e8200b3ee4c4c186d245e2b4053f01d56957336c1817fca0b997469106"),
}


def main() -> None:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise SystemExit("此安装脚本适用于 Apple Silicon Mac。其他平台请手动安装 FFmpeg 和 ffprobe。")
    bindir = ROOT / ".runtime" / "bin"
    downloads = ROOT / ".runtime" / "downloads"
    bindir.mkdir(parents=True, exist_ok=True)
    downloads.mkdir(parents=True, exist_ok=True)
    for name, (url, digest) in FILES.items():
        target = bindir / name
        if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == digest:
            print(f"{name} 已安装且 SHA256 正确。", flush=True)
            continue
        archive = downloads / url.rsplit("/", 1)[1]
        if not archive.exists():
            subprocess.run(["/usr/bin/curl", "--fail", "--location", "--retry", "3", "--connect-timeout", "20", "--output", str(archive) + ".part", url], check=True)
            Path(str(archive) + ".part").replace(archive)
        with zipfile.ZipFile(archive) as zf:
            candidates = [entry for entry in zf.infolist() if Path(entry.filename).name == name and not entry.is_dir()]
            if len(candidates) != 1:
                raise RuntimeError(f"{archive.name} 中没有唯一的 {name} 文件。")
            binary = zf.read(candidates[0])
        if hashlib.sha256(binary).hexdigest() != digest:
            raise RuntimeError(f"{name} SHA256 校验失败，停止安装。请检查来源或删除损坏的下载包重试。")
        temporary = target.with_suffix(".new")
        temporary.write_bytes(binary)
        temporary.chmod(0o755)
        temporary.replace(target)
        print(f"已校验并安装 {name}。", flush=True)
    filters = subprocess.run([str(bindir / "ffmpeg"), "-hide_banner", "-filters"], capture_output=True, text=True, check=True)
    if " subtitles " not in filters.stdout or " ass " not in filters.stdout:
        raise RuntimeError("FFmpeg 缺少 libass 字幕滤镜，不能进行字幕压制。")
    subprocess.run([str(bindir / "ffprobe"), "-version"], check=True, stdout=subprocess.DEVNULL)
    (ROOT / ".runtime" / "ffmpeg-sources.json").write_text(json.dumps({
        "publisher": "OSXExperts.NET", "source_page": "https://www.osxexperts.net/",
        "ffmpeg_source": "https://github.com/FFmpeg/FFmpeg", "license": "GPL; see publisher's build/source information",
        "binaries": {name: {"url": url, "binary_sha256": sha} for name, (url, sha) in FILES.items()},
    }, indent=2), encoding="utf-8")
    print("FFmpeg / ffprobe / libass 检查通过。", flush=True)


if __name__ == "__main__":
    main()
