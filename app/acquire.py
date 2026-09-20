"""Optional direct source download using yt-dlp; no hosted processing service."""
from __future__ import annotations
import math
import shutil
import time
from pathlib import Path
from urllib.parse import urlparse


def normalize_heatmap(values, duration):
    """Keep optional public replay hints bounded; malformed metadata is ignored."""
    result = []
    for row in values[:10000] if isinstance(values, list) else []:
        try:
            a, b, value = (float(row[k]) for k in ("start_time", "end_time", "value"))
            if all(math.isfinite(x) for x in (a, b, value)) and 0 <= a < min(b, duration) and b <= duration + 1:
                result.append({"start_time": a, "end_time": min(b, duration), "value": max(0, min(1, value))})
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(result, key=lambda row: row["start_time"])


def validate_youtube_url(url):
    try:
        parsed = urlparse(url)
    except ValueError:
        raise ValueError("请输入有效的 YouTube 视频链接")
    if parsed.scheme != "https" or parsed.hostname not in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"} or parsed.username or parsed.password:
        raise ValueError("请使用 https://www.youtube.com/watch?v=… 或 https://youtu.be/… 链接")
    if parsed.port not in (None, 443):
        raise ValueError("视频链接不能包含自定义端口")
    if parsed.path in ("/playlist", "/", ""):
        raise ValueError("请输入单个视频链接，不支持频道或播放列表")
    return url


def run_download(request, progress):
    from yt_dlp import YoutubeDL
    from .pipeline import ffmpeg_path

    options = request.get("options", {})
    url = validate_youtube_url(str(options.get("url", "")))
    data_dir = Path(request["data_dir"])
    target = data_dir / "imports" / request["job_id"]
    target.mkdir(parents=True, exist_ok=True)
    height = int(options.get("max_height", 1080))
    if height not in (480, 720, 1080, 1440, 2160):
        height = 1080
    if shutil.disk_usage(data_dir).free < 10 * 1024**3:
        raise ValueError("下载视频需要预留至少 10 GiB 空间；也可以导入已下载的视频路径")

    last_emit = 0.0
    def hook(event):
        nonlocal last_emit
        now = time.monotonic()
        if event.get("status") == "downloading" and now - last_emit < 0.8:
            return
        last_emit = now
        if shutil.disk_usage(data_dir).free < 3 * 1024**3:
            raise RuntimeError("剩余空间低于 3 GiB，下载已停止。未完成文件保留在 data/imports 中")
        total = event.get("total_bytes") or event.get("total_bytes_estimate") or 0
        downloaded = event.get("downloaded_bytes", 0)
        fraction = downloaded / total if total else 0
        if event.get("status") == "downloading":
            progress(min(.88, .08 + fraction * .8), f"下载视频 · {downloaded / 1024**2:.0f} MiB" + (f" / {total / 1024**2:.0f} MiB" if total else ""))
        elif event.get("status") == "finished":
            progress(.90, "视频轨下载完成，正在准备合并")

    config = {"noplaylist": True, "quiet": True, "no_warnings": True,
              "format": f"bestvideo[height<={height}][vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/best[height<={height}][ext=mp4]/best[height<={height}]",
              "merge_output_format": "mp4", "outtmpl": str(target / "%(id)s.%(ext)s"),
              "ffmpeg_location": str(Path(ffmpeg_path()).parent), "progress_hooks": [hook],
              "socket_timeout": 30, "retries": 3, "fragment_retries": 3,
              "js_runtimes": {"node": {"path": shutil.which("node") or "/opt/homebrew/bin/node"}},
              "cachedir": str(data_dir / "cache" / "yt-dlp"), "restrictfilenames": True, "noprogress": True}
    progress(.02, "读取 YouTube 视频信息")
    with YoutubeDL(config) as downloader:
        info = downloader.extract_info(url, download=False)
        if not info or info.get("_type") in ("playlist", "multi_video"):
            raise ValueError("仅支持单个公开回放视频")
        if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming", "post_live"):
            raise ValueError("请等待直播结束并生成公开回放后再导入")
        duration = float(info.get("duration") or 0)
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("无法确定视频时长，请先下载到本地后导入")
        estimate = info.get("filesize") or info.get("filesize_approx")
        if estimate and estimate * 2 + 3 * 1024**3 > shutil.disk_usage(data_dir).free:
            raise ValueError("磁盘空间不足以下载并合并这个视频，请降低分辨率或使用外置盘")
        progress(.06, "正在下载：" + str(info.get("title", "YouTube 视频")))
        info = downloader.process_ie_result(info, download=True)
        paths = list(target.glob("*.mp4")) or [p for p in target.iterdir() if p.suffix in {".mkv", ".webm", ".mov"}]
        if not paths:
            expected = Path(downloader.prepare_filename(info))
            if expected.is_file():
                paths = [expected]
        if not paths:
            raise RuntimeError("下载完成但未找到合并后的媒体文件，请查看 data/imports")
        path = max(paths, key=lambda p: p.stat().st_size)
        progress(1, "视频已下载，准备导入")
        return {"imported_path": str(path.resolve()), "source_title": str(info.get("title") or path.stem),
                "source_url": str(info.get("webpage_url") or url),
                "source_heatmap": normalize_heatmap(info.get("heatmap"), duration)}
