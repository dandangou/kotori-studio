"""Serial subprocess queue: responsive UI, cancellable FFmpeg, released GPU memory."""
from __future__ import annotations

import copy
import json
import os
import queue
import signal
import subprocess
import sys
import threading
from pathlib import Path

from . import store

KINDS = {"waveform", "scan", "transcribe", "translate", "semantic", "export", "proxy", "download", "download_video"}


class JobManager:
    def __init__(self):
        self.lock = threading.RLock()
        self.queue = queue.Queue()
        self.jobs = {}
        self.options = {}
        self.submitted_asr = {}
        self.processes = {}
        self.closed = False
        store.initialize()
        for path in (store.DATA / "jobs").glob("*.json"):
            if path.name.endswith((".request.json", ".result.json")):
                continue
            try:
                job = json.loads(path.read_text())
                if job.get("status") in ("running", "queued"):
                    job.update(status="failed", error="应用已退出，任务中断。可重新运行。", message="上次运行已中断")
                    store.atomic_json(path, job)
                self.jobs[job["id"]] = job
            except (OSError, ValueError, KeyError):
                pass
        self.thread = threading.Thread(target=self._loop, daemon=True, name="kotori-job-queue")
        self.thread.start()

    def _persist(self, job):
        store.atomic_json(store.DATA / "jobs" / f'{job["id"]}.json', job)

    def list(self):
        with self.lock:
            return copy.deepcopy(sorted(self.jobs.values(), key=lambda j: j["created_at"], reverse=True)[:100])

    def submit(self, kind, project_id=None, options=None):
        if kind not in KINDS:
            raise ValueError("未知处理任务")
        with self.lock:
            if self.closed:
                raise ValueError("应用正在退出")
            if sum(j["status"] in ("queued", "running") for j in self.jobs.values()) >= 30:
                raise ValueError("队列已满，请等待现有任务完成")
            job = {"id": store.uid(), "project_id": project_id, "kind": kind, "status": "queued", "progress": 0,
                   "message": "等待处理（为控制内存占用，任务依次执行）", "created_at": store.now(), "result": None, "error": None}
            if kind == "download":
                job["model_id"] = (options or {}).get("model_id") or (options or {}).get("model")
            self.jobs[job["id"]] = job
            self.options[job["id"]] = copy.deepcopy(options or {})
            if kind == "transcribe" and project_id:
                project = store.get_project(project_id)
                a, b = float((options or {}).get("start", 0)), float((options or {}).get("end", project["duration"]))
                self.submitted_asr[job["id"]] = (a, b, self._range_segments(project, a, b))
            self._persist(job)
            self.queue.put(job["id"])
            return copy.deepcopy(job)

    def cancel(self, job_id):
        with self.lock:
            if job_id not in self.jobs:
                raise KeyError("任务不存在")
            job = self.jobs[job_id]
            if job["status"] in ("queued", "running"):
                job.update(status="cancelled", message="已取消", finished_at=store.now())
                self.options.pop(job_id, None)
                self.submitted_asr.pop(job_id, None)
                proc = self.processes.get(job_id)
                if proc and proc.poll() is None:
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                        threading.Thread(target=self._kill_later, args=(proc,), daemon=True).start()
                    except ProcessLookupError:
                        pass
                self._persist(job)
            return copy.deepcopy(job)

    @staticmethod
    def _range_segments(project, start, end):
        return copy.deepcopy([s for s in project["segments"] if s["end"] > start and s["start"] < end])

    @staticmethod
    def _kill_later(proc):
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def close(self):
        with self.lock:
            self.closed = True
            for job_id in list(self.jobs):
                if self.jobs[job_id]["status"] in ("running", "queued"):
                    self.cancel(job_id)
        self.queue.put(None)

    def _loop(self):
        while True:
            job_id = self.queue.get()
            if job_id is None:
                return
            with self.lock:
                if self.jobs[job_id]["status"] != "queued":
                    continue
            self._run(job_id)

    def _run(self, job_id):
        request_path = store.DATA / "jobs" / f"{job_id}.request.json"
        output_path = store.DATA / "jobs" / f"{job_id}.result.json"
        log_path = store.DATA / "jobs" / f"{job_id}.log"
        try:
            with self.lock:
                job = self.jobs[job_id]
                if job["status"] == "cancelled":
                    return
                options = self.options.pop(job_id, {})
                project = store.get_project(job["project_id"]) if job["project_id"] else None
                submitted = self.submitted_asr.pop(job_id, None)
                if submitted and self._range_segments(project, submitted[0], submitted[1]) != submitted[2]:
                    raise store.ConflictError("识别任务排队期间，该范围的字幕已被修改。已保留人工修改；如确需替换，请重新提交识别。")
                # API key only lives in process memory/environment; never write it to JSON/logs.
                api_key = options.pop("api_key", "")
                request = {"job_id": job_id, "kind": job["kind"], "project": project, "options": options,
                           "data_dir": str(store.DATA), "output_path": str(output_path)}
                store.atomic_json(request_path, request)
                request_path.chmod(0o600)
                env = dict(os.environ, PYTHONUNBUFFERED="1", HF_HOME=str(store.DATA / "models"), HF_HUB_CACHE=str(store.DATA / "models" / "hub"), TOKENIZERS_PARALLELISM="false")
                env["PATH"] = str(store.ROOT / ".runtime" / "bin") + os.pathsep + env.get("PATH", "")
                if api_key:
                    env["KOTORI_API_KEY"] = api_key
                log = log_path.open("w", encoding="utf-8")
                proc = subprocess.Popen([sys.executable, "-m", "app.worker", str(request_path)], cwd=store.ROOT,
                                        stdout=subprocess.PIPE, stderr=log, text=True, env=env, start_new_session=True)
                self.processes[job_id] = proc
                job.update(status="running", message="正在启动处理", started_at=store.now())
                self._persist(job)
            try:
                for line in proc.stdout:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    with self.lock:
                        if job["status"] == "cancelled":
                            continue
                        if isinstance(event, dict) and "progress" in event:
                            job["progress"] = max(0, min(1, float(event["progress"])))
                            job["message"] = str(event.get("message", "处理中"))[:2000]
                            self._persist(job)
                code = proc.wait()
            finally:
                log.close()
            with self.lock:
                if job["status"] == "cancelled":
                    return
                if code != 0 or not output_path.is_file():
                    details = log_path.read_text(encoding="utf-8", errors="replace")[-6000:]
                    if api_key:
                        details = details.replace(api_key, "[redacted]")
                    raise RuntimeError(details.strip() or f"处理程序退出（{code}）")
                result = json.loads(output_path.read_text(encoding="utf-8"))
                summary = {}
                if project:
                    summary = store.merge_job_result(project["id"], result, project)
                elif result.get("imported_path"):
                    from .pipeline import probe_media
                    downloaded = store.create_project(result["imported_path"], probe_media(result["imported_path"]),
                        name=result.get("source_title"), source_title=result.get("source_title", ""), source_url=result.get("source_url", ""),
                        source_heatmap=result.get("source_heatmap", []))
                    summary["project_id"] = downloaded["id"]
                    try:
                        self.submit("waveform", downloaded["id"])
                    except Exception:
                        result["warning"] = "视频已导入；自动分析暂未启动，可稍后手动分析波形"
                job.update(status="completed", progress=1, message="处理完成", finished_at=store.now(), result=summary)
                if summary.get("skipped_translations"):
                    job["message"] = f'完成；保留了 {summary["skipped_translations"]} 条处理期间手动修改的字幕'
                if result.get("warning"):
                    job["message"] += " · " + str(result["warning"])
                self._persist(job)
        except Exception as exc:
            with self.lock:
                job = self.jobs[job_id]
                if job["status"] != "cancelled":
                    job.update(status="failed", message="处理失败", error=str(exc)[-6000:], finished_at=store.now())
                    self._persist(job)
        finally:
            request_path.unlink(missing_ok=True)
            with self.lock:
                self.processes.pop(job_id, None)
