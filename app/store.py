"""Small durable project store. Atomic writes and revisions protect manual edits."""
from __future__ import annotations

import copy
import json
import math
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from .subtitles import validate_ranges, validate_speakers

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("KOTORI_DATA_DIR", ROOT / "data")).resolve()
LOCK = threading.RLock()
DEFAULT_SETTINGS = {"mode": "mixed", "asr_model": "turbo", "llm_model": "qwen3-4b", "glossary": "ホロライブ=hololive\n兎田ぺこら=兔田佩克拉\n宝鐘マリン=宝钟玛琳\nさくらみこ=樱巫女\n星街すいせい=星街彗星", "clip_min": 30, "clip_max": 120}


def now():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return uuid.uuid4().hex[:16]


def initialize():
    for name in ("projects", "jobs", "exports", "models", "imports", "cache"):
        (DATA / name).mkdir(parents=True, exist_ok=True)


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp-" + uid())
    try:
        with temp.open("w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def project_path(project_id):
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", project_id):
        raise KeyError("项目不存在")
    return DATA / "projects" / project_id / "project.json"


def get_project(project_id):
    with LOCK:
        path = project_path(project_id)
        if not path.is_file():
            raise KeyError("项目不存在")
        return json.loads(path.read_text(encoding="utf-8"))


def list_projects():
    initialize()
    with LOCK:
        projects = []
        for path in (DATA / "projects").glob("*/project.json"):
            try:
                p = json.loads(path.read_text(encoding="utf-8"))
                projects.append({k: v for k, v in p.items() if k not in ("segments", "waveform", "highlights", "settings")})
                projects[-1].update(segment_count=len(p.get("segments", [])), highlight_count=len(p.get("highlights", [])))
            except (OSError, ValueError):
                continue
        return sorted(projects, key=lambda p: p.get("updated_at", ""), reverse=True)


def create_project(path, info, name=None, source_url="", source_title="", **extra):
    initialize()
    project = {"id": uid(), "name": name or Path(path).stem, "source_path": str(Path(path).resolve()),
               "source_url": source_url, "source_title": source_title or name or Path(path).stem,
               "duration": float(info["duration"]), "width": info.get("width", 0), "height": info.get("height", 0),
               "created_at": now(), "updated_at": now(), "segments": [], "highlights": [],
               "waveform": [], "waveform_step": 0.1, "exports": [], "settings": copy.deepcopy(DEFAULT_SETTINGS),
               "revision": 0, **extra}
    with LOCK:
        atomic_json(project_path(project["id"]), project)
    return project


class ConflictError(ValueError):
    pass


def validate_timing(items, duration, kind):
    if not isinstance(items, list) or len(items) > 100000:
        raise ValueError(f"{kind}数量或格式不正确")
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError(f"{kind}格式不正确")
        ident = str(item.get("id", ""))
        if not ident or ident in seen:
            raise ValueError(f"{kind} ID 缺失或重复")
        seen.add(ident)
        try:
            a, b = float(item["start"]), float(item["end"])
        except (KeyError, ValueError, TypeError):
            raise ValueError(f"{kind}时间格式不正确")
        if not math.isfinite(a) or not math.isfinite(b) or a < 0 or b <= a or b > duration + 0.001:
            raise ValueError(f"{kind}时间必须在视频范围内，且结束晚于开始")
        a, b = round(a, 3), min(round(b, 3), math.floor(duration * 1000) / 1000)
        if not 0 <= a < b <= duration:
            raise ValueError(f"{kind}在毫秒精度下必须有有效时长，且不能超过视频结尾")
        item["start"], item["end"] = a, b
        if "speaker_ids" in item:
            ids = item["speaker_ids"]
            if not isinstance(ids, list) or len(ids) > 8 or any(not isinstance(i, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", i) for i in ids) or len(set(ids)) != len(ids):
                raise ValueError("每句最多选择 8 位不同说话人")
        if "ranges" in item:
            item["ranges"] = validate_ranges(item["ranges"], duration)
            item["start"] = min(r["start"] for r in item["ranges"])
            item["end"] = max(r["end"] for r in item["ranges"])
        for field in ("ja", "zh", "speaker", "title", "reason"):
            if field in item and (not isinstance(item[field], str) or len(item[field]) > 20000):
                raise ValueError(f"{kind}文本过长或格式错误")


def patch_project(project_id, patch):
    with LOCK:
        project = get_project(project_id)
        if patch.get("revision") != project["revision"]:
            raise ConflictError("项目有新处理结果。请重新载入后再保存；当前输入仍保留在编辑器中。")
        for key in ("segments", "highlights"):
            if key in patch:
                value = copy.deepcopy(patch[key])
                validate_timing(value, project["duration"], "字幕" if key == "segments" else "片段")
                project[key] = sorted(value, key=lambda s: s["start"])
        for key in ("name", "source_url", "source_title"):
            if key in patch:
                if not isinstance(patch[key], str) or len(patch[key]) > 2000:
                    raise ValueError("项目信息格式错误")
                project[key] = patch[key]
        if "edit_ranges" in patch:
            project["edit_ranges"] = validate_ranges(patch["edit_ranges"], project["duration"], allow_empty=True)
        if "speakers" in patch:
            project["speakers"] = copy.deepcopy(validate_speakers(patch["speakers"]))
        if "settings" in patch:
            settings = patch["settings"]
            if not isinstance(settings, dict):
                raise ValueError("设置格式错误")
            # Credentials and endpoints deliberately never stored in a project.
            allowed = set(DEFAULT_SETTINGS) | {"context_before", "context_after", "max_candidates", "story_mode"}
            settings = {k: v for k, v in settings.items() if k in allowed}
            combined = {**project["settings"], **settings}
            if combined.get("story_mode", "story") not in {"story", "short", "continuous"}:
                raise ValueError("未知成片目标")
            if combined["mode"] not in ("mixed", "gaming", "chat", "music", "game", "talk"):
                raise ValueError("未知选片模式")
            if not 5 <= float(combined["clip_min"]) <= float(combined["clip_max"]) <= 600:
                raise ValueError("片段长度应为 5–600 秒，最短不能大于最长")
            if not isinstance(combined["glossary"], str) or len(combined["glossary"]) > 20000:
                raise ValueError("术语表过长")
            project["settings"] = combined
        project["revision"] += 1
        project["updated_at"] = now()
        atomic_json(project_path(project_id), project)
        return project


def merge_job_result(project_id, result, baseline):
    """Apply only fields owned by a job; never silently overwrite concurrent edits."""
    with LOCK:
        project = get_project(project_id)
        skipped = 0
        if "segments" in result:
            a, b = result.get("replace_range", [0, project["duration"]])
            old = {s["id"]: s for s in baseline.get("segments", []) if s["end"] > a and s["start"] < b}
            current = {s["id"]: s for s in project["segments"] if s["end"] > a and s["start"] < b}
            if old != current:
                raise ConflictError("识别期间该范围的字幕已被编辑。为保护修改，识别结果保存在任务结果文件中，未覆盖字幕；请在编辑完成后重新识别。")
            new = copy.deepcopy(result["segments"])
            validate_timing(new, project["duration"], "识别字幕")
            project["segments"] = sorted([s for s in project["segments"] if s["end"] <= a or s["start"] >= b] + new, key=lambda s: s["start"])
        if "translations" in result:
            previous = {s["id"]: s for s in baseline.get("segments", [])}
            translated = {s["id"]: s["zh"] for s in result["translations"]}
            for s in project["segments"]:
                if s["id"] not in translated:
                    continue
                old = previous.get(s["id"], {})
                if any(s.get(k) != old.get(k) for k in ("ja", "zh", "reviewed")):
                    skipped += 1
                    continue
                s["zh"] = translated[s["id"]]
                s["reviewed"] = False
        for field in ("waveform", "waveform_step", "proxy_path"):
            if field in result:
                project[field] = result[field]
        if "highlights" in result:
            new = copy.deepcopy(result["highlights"])
            validate_timing(new, project["duration"], "推荐片段")
            # Keep explicitly retained clips when scanning again.
            keep = [h for h in project["highlights"] if h.get("selected")]
            project["highlights"] = keep + [h for h in new if not any(abs(h["start"] - k["start"]) < 2 for k in keep)]
        if "exports" in result:
            project["exports"].extend(result["exports"])
        project["revision"] += 1
        project["updated_at"] = now()
        atomic_json(project_path(project_id), project)
        return {"skipped_translations": skipped, "revision": project["revision"]}
