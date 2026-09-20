"""Kotori Studio: local-only video clipping and bilingual subtitle workbench."""
from __future__ import annotations

import importlib.util
import math
import mimetypes
import os
import platform
import shutil
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import store
from .jobs import JobManager

manager: JobManager | None = None
disk_cache = {"at": 0, "bytes": 0}
demo_lock = threading.Lock()


@asynccontextmanager
async def lifespan(app):
    global manager
    store.initialize()
    manager = JobManager()
    yield
    manager.close()


app = FastAPI(title="烤肉工房 · Kotori Studio", version="0.1.0", lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"])


@app.middleware("http")
async def local_origin_guard(request: Request, call_next):
    origin = request.headers.get("origin")
    if request.method not in ("GET", "HEAD", "OPTIONS") and origin:
        parsed = urlparse(origin)
        if parsed.netloc != request.headers.get("host") or parsed.scheme not in ("http", "https"):
            return JSONResponse({"detail": "仅允许本地工作台页面发起操作"}, status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    if request.url.path.startswith("/api/") and "/media" not in request.url.path:
        response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(KeyError)
async def not_found(request, exc):
    return JSONResponse({"detail": str(exc).strip("'")}, status_code=404)


@app.exception_handler(store.ConflictError)
async def conflict(request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=409)


@app.exception_handler(ValueError)
async def invalid(request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.get("/api/health")
def health():
    return {"app": "kotori-studio", "version": "0.1.0", "status": "ok"}


@app.get("/api/system")
def system():
    from .pipeline import model_status, ffmpeg_path, ffprobe_path
    def binary(fn):
        try:
            path = fn()
            return path if path and Path(path).exists() else False
        except Exception:
            return False
    if time.monotonic() - disk_cache["at"] > 20:
        total = 0
        for base, dirs, files in os.walk(store.DATA):
            for name in files:
                try:
                    p = Path(base) / name
                    if not p.is_symlink():
                        total += p.stat().st_size
                except OSError:
                    pass
        disk_cache.update(at=time.monotonic(), bytes=total)
    return {"app_id": "yakuniku-workshop", "platform": f"{platform.system()} {platform.machine()}",
            "free_gb": round(shutil.disk_usage(store.DATA).free / 1024**3, 1), "data_gb": round(disk_cache["bytes"] / 1024**3, 2),
            "ffmpeg": binary(ffmpeg_path), "ffprobe": binary(ffprobe_path),
            "asr_available": importlib.util.find_spec("mlx_whisper") is not None,
            "llm_available": importlib.util.find_spec("mlx_lm") is not None,
            "models": model_status(), "jobs": manager.list(), "data_dir": str(store.DATA)}


@app.get("/api/projects")
def projects():
    return store.list_projects()


class ImportRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    name: str | None = Field(default=None, max_length=2000)
    source_url: str = Field(default="", max_length=2000)
    source_title: str = Field(default="", max_length=2000)


class YoutubeRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2000)
    max_height: int = 1080


@app.post("/api/sources/youtube")
def youtube(body: YoutubeRequest):
    from .acquire import validate_youtube_url
    validate_youtube_url(body.url)
    return manager.submit("download_video", options=body.model_dump())


def import_path(body: ImportRequest):
    from .pipeline import probe_media
    path = Path(body.path.strip().strip('"').strip("'")).expanduser().resolve()
    if not path.is_file():
        raise ValueError("找不到视频文件，请检查路径或使用选择文件")
    try:
        info = probe_media(str(path))
    except Exception as exc:
        raise ValueError(f"无法读取媒体文件：{exc}")
    if not math.isfinite(float(info.get("duration", 0))) or info.get("duration", 0) <= 0:
        raise ValueError("媒体文件没有可读取的时长")
    project = store.create_project(path, info, body.name, body.source_url, body.source_title)
    enqueue_waveform(project)
    return project


def enqueue_waveform(project):
    """Import is successful even when automatic analysis must wait for space."""
    try:
        manager.submit("waveform", project["id"])
    except Exception:
        project["import_notice"] = "视频已导入；自动分析暂未启动，请稍后点击「分析波形」。"


@app.post("/api/projects")
def import_project(body: ImportRequest):
    return import_path(body)


@app.post("/api/pick-file")
def pick_file():
    if platform.system() != "Darwin":
        raise ValueError("此系统请使用路径或上传导入")
    result = subprocess.run(["osascript", "-e", 'POSIX path of (choose file with prompt "选择要烤肉的视频（直接引用，不复制文件）")'], capture_output=True, text=True, timeout=300)
    if result.returncode:
        return {"path": ""}
    return {"path": result.stdout.strip()}


@app.post("/api/upload")
def upload(file: UploadFile = File(...)):
    suffix = Path(file.filename or "video.mp4").suffix.lower()
    if suffix not in {".mp4", ".mkv", ".mov", ".webm", ".m4v", ".avi", ".ts"}:
        raise ValueError("不支持此文件类型；请导入常见视频格式")
    target = store.DATA / "imports" / (store.uid() + suffix)
    try:
        with target.open("wb") as dest:
            count = 0
            while chunk := file.file.read(1024 * 1024):
                if count % (128 * 1024 * 1024) == 0 and shutil.disk_usage(store.DATA).free < 3 * 1024**3:
                    raise ValueError("剩余空间不足 3 GiB，已停止导入。请改用本地文件路径避免复制")
                dest.write(chunk)
                count += len(chunk)
        return import_path(ImportRequest(path=str(target), name=Path(file.filename or "导入视频").stem))
    except Exception:
        target.unlink(missing_ok=True)
        raise
    finally:
        file.file.close()


@app.get("/api/projects/{project_id}")
def project(project_id: str):
    return store.get_project(project_id)


@app.patch("/api/projects/{project_id}")
def update_project(project_id: str, body: dict):
    return store.patch_project(project_id, body)


@app.get("/api/projects/{project_id}/media")
def media(project_id: str):
    project = store.get_project(project_id)
    path = Path(project.get("proxy_path") or project["source_path"])
    if not path.is_file():
        raise HTTPException(404, "源文件已移动或删除，请重新导入")
    media_type = mimetypes.guess_type(str(path))[0] or "video/mp4"
    return FileResponse(path, media_type=media_type)


class JobRequest(BaseModel):
    kind: str
    options: dict = Field(default_factory=dict)


@app.post("/api/projects/{project_id}/jobs")
def start_job(project_id: str, body: JobRequest):
    project = store.get_project(project_id)
    if body.kind not in {"waveform", "scan", "transcribe", "translate", "semantic", "export", "proxy"}:
        raise ValueError("未知任务类型")
    opts = body.options
    if body.kind == "translate":
        provider = opts.get("provider", "local")
        if provider not in {"local", "openai-compatible"}:
            raise ValueError("请选择本地模型或自己的兼容 API")
        if provider == "openai-compatible":
            parsed = urlparse(str(opts.get("api_base", "")))
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("API 地址应为不含账号、密钥、查询参数的服务根地址，例如 https://example.com/v1")
            if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise ValueError("远程 API 请使用 HTTPS 地址")
            if not str(opts.get("api_model", "")).strip():
                raise ValueError("请填写 API 模型名称")
    if "start" in opts or "end" in opts:
        try:
            a, b = float(opts.get("start", 0)), float(opts.get("end", project["duration"]))
        except (ValueError, TypeError):
            raise ValueError("片段时间格式不正确")
        if not math.isfinite(a) or not math.isfinite(b) or a < 0 or b <= a or b > project["duration"] + 0.1:
            raise ValueError("片段时间必须在视频范围内，且结束晚于开始")
        opts.update(start=a, end=min(b, project["duration"]))
    if body.kind == "export":
        height = opts.get("output_height", 0)
        if type(height) is not int or height not in (0, 480, 720, 1080, 1440, 2160):
            raise ValueError("导出分辨率无效")
        if "ranges" in opts:
            opts["ranges"] = store.validate_ranges(opts["ranges"], project["duration"])
        if opts.get("format", "srt") not in {"srt", "ass", "video", "burn"}:
            raise ValueError("不支持的导出格式")
        if opts.get("language", "bilingual") not in {"bilingual", "zh", "ja"}:
            raise ValueError("不支持的字幕语言")
    if body.kind in {"export", "proxy", "transcribe"} and shutil.disk_usage(store.DATA).free < 2 * 1024**3:
        raise ValueError("剩余空间不足 2 GiB，请先清理缓存或移动源视频")
    return manager.submit(body.kind, project_id, opts)


@app.get("/api/jobs")
def jobs():
    return manager.list()


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    return manager.cancel(job_id)


class ModelDownloadRequest(BaseModel):
    endpoint: str = "https://huggingface.co"


@app.post("/api/models/{model_id}/download")
def download_model(model_id: str, body: ModelDownloadRequest | None = None):
    if model_id not in {"turbo", "large-v3", "qwen3-4b"}:
        raise ValueError("未知模型")
    if shutil.disk_usage(store.DATA).free < (10 if model_id == "large-v3" else 6) * 1024**3:
        raise ValueError("剩余空间不足，请先释放空间再下载模型")
    endpoint = body.endpoint if body else "https://huggingface.co"
    if endpoint not in {"https://huggingface.co", "https://hf-mirror.com"}:
        raise ValueError("请选择模型官方下载站或已提供的镜像站")
    return manager.submit("download", options={"model": model_id, "model_id": model_id, "endpoint": endpoint})


class SubtitleRequest(BaseModel):
    text: str = Field(max_length=20_000_000)
    language: str = "ja"
    revision: int | None = None


@app.post("/api/projects/{project_id}/subtitles")
def import_subtitles(project_id: str, body: SubtitleRequest):
    from .subtitles import parse_srt
    if body.language not in {"ja", "zh"}:
        raise ValueError("请选择日文或中文字幕")
    with store.LOCK:
        p = store.get_project(project_id)
        if body.revision is not None and body.revision != p["revision"]:
            raise store.ConflictError("项目已有更新，请刷新后重新导入字幕")
        cues = parse_srt(body.text, body.language)
        if not cues:
            raise ValueError("没有找到有效 SRT 字幕，请检查文件格式")
        valid = [c for c in cues if c["start"] < p["duration"]]
        if not valid:
            raise ValueError("字幕全部位于视频时长之外，未修改现有字幕。请确认字幕对应的源视频和时间基准。")
        for c in valid:
            c["end"] = min(c["end"], p["duration"])
        if body.language == "zh" and p["segments"]:
            segments = p["segments"]
            used = set()
            for cue in valid:
                matches = [s for s in segments if s["id"] not in used and abs(s["start"] - cue["start"]) < 0.2 and abs(s["end"] - cue["end"]) < 0.3]
                if matches:
                    matches[0]["zh"] = cue.get("zh", "")
                    matches[0]["reviewed"] = False
                    used.add(matches[0]["id"])
                else:
                    segments.append(cue)
        else:
            segments = valid
        return store.patch_project(project_id, {"revision": p["revision"], "segments": segments})


@app.get("/api/projects/{project_id}/exports/{export_id}")
def download_export(project_id: str, export_id: str):
    p = store.get_project(project_id)
    item = next((e for e in p["exports"] if e["id"] == export_id), None)
    if not item:
        raise HTTPException(404, "导出文件不存在")
    path = Path(item["path"]).resolve()
    if not path.is_relative_to(store.DATA) or not path.is_file():
        raise HTTPException(404, "导出文件已移动或删除")
    return FileResponse(path, filename=item.get("name") or path.name)


@app.post("/api/projects/{project_id}/cleanup")
def cleanup(project_id: str):
    # Require no active jobs: deleting a WAV that a worker is reading is not useful.
    if any(j["project_id"] == project_id and j["status"] in ("running", "queued") for j in manager.list()):
        raise ValueError("请等待该项目的任务结束或先取消任务，再清理缓存")
    with store.LOCK:
        p = store.get_project(project_id)
        protected = {Path(p["source_path"]).resolve()} | {Path(e["path"]).resolve() for e in p["exports"]}
        candidates = []
        for folder in (store.DATA / "projects" / project_id, store.DATA / "cache" / project_id):
            if folder.exists():
                candidates.extend(x for x in folder.rglob("*") if x.is_file() and x.suffix.lower() in (".wav", ".pcm", ".mp4", ".m4a"))
        removed = 0
        for path in candidates:
            if path.resolve() not in protected and path.resolve().is_relative_to(store.DATA):
                removed += path.stat().st_size
                path.unlink()
        if p.get("proxy_path") and not Path(p["proxy_path"]).exists():
            p.pop("proxy_path", None)
        p["revision"] += 1
        p["updated_at"] = store.now()
        store.atomic_json(store.project_path(project_id), p)
        return {"freed_mb": round(removed / 1024**2, 1), "project": p}


@app.get("/api/demo")
def demo():
    from .pipeline import ffmpeg_path, probe_media
    with demo_lock:
        for summary in store.list_projects():
            if summary.get("demo"):
                p = store.get_project(summary["id"])
                if Path(p["source_path"]).exists():
                    return p
        path = store.DATA / "demo.mp4"
        if not path.exists():
            result = subprocess.run([ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "testsrc2=size=960x540:rate=24:duration=32",
                "-f", "lavfi", "-i", "sine=frequency=330:sample_rate=24000:duration=32",
                "-af", "volume=0.035", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", "-shortest", str(path)], capture_output=True, text=True, timeout=120)
            if result.returncode:
                raise ValueError("演示视频生成失败：" + result.stderr[-1000:])
        p = store.create_project(path, probe_media(str(path)), name="演示项目 · 字幕与打轴练习", source_title="本机生成的测试画面（非直播素材）", demo=True)
        p["segments"] = [
            {"id": store.uid(), "start": 1.0, "end": 4.5, "ja": "これは字幕編集のデモです。", "zh": "这是字幕编辑演示。", "speaker": "演示", "reviewed": True, "confidence": 1, "words": [], "flags": []},
            {"id": store.uid(), "start": 5.2, "end": 9.0, "ja": "波形をクリックして、再生位置を動かせます。", "zh": "点击波形，即可移动播放位置。", "speaker": "演示", "reviewed": False, "confidence": 0.68, "words": [], "flags": ["演示用提示：请校对"]},
            {"id": store.uid(), "start": 10.0, "end": 14.0, "ja": "日本語と中国語を並べて確認しましょう。", "zh": "对照日文和中文，逐句确认吧。", "speaker": "演示", "reviewed": False, "confidence": 0.9, "words": [], "flags": []},
            {"id": store.uid(), "start": 16.0, "end": 20.0, "ja": "気になる部分は、繰り返し聞いてください。", "zh": "有疑问的地方，可以循环试听。", "speaker": "演示", "reviewed": False, "confidence": 0.9, "words": [], "flags": []},
            {"id": store.uid(), "start": 22.0, "end": 28.0, "ja": "この字幕は操作練習用で、音声認識の結果ではありません。", "zh": "这些字幕供操作练习使用，并非语音识别结果。", "speaker": "演示", "reviewed": True, "confidence": 1, "words": [], "flags": []},
        ]
        p["highlights"] = [{"id": store.uid(), "start": 0, "end": 15, "title": "字幕编辑练习", "reason": "演示片段，供预览和导出练习；不是自动识别结果。", "score": 0, "method": "audio", "selected": False}]
        store.atomic_json(store.project_path(p["id"]), p)
        enqueue_waveform(p)
        return p


app.mount("/static", StaticFiles(directory=store.ROOT / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(store.ROOT / "static" / "index.html", headers={"Cache-Control": "no-cache"})
