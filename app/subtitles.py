"""SRT/ASS parsing and clip-relative subtitle rendering; no model dependencies."""
from __future__ import annotations

import html
import copy
import math
import re
import uuid
import unicodedata

DEFAULT_SPEAKERS = [
    {"id": "kanata", "name": "天音彼方", "color": "#4C88FF"},
    {"id": "lamy", "name": "雪花菈米", "color": "#94DEFF"},
    {"id": "korone", "name": "戌神沁音", "color": "#C9994A"},
    {"id": "azki", "name": "AZKi", "color": "#E65D76"},
]


def sentence_merges(segments, max_duration=9, max_chars=52, max_gap=0.45):
    """Conservative Japanese continuation suggestions; never infer speaker identity."""
    if not (2 <= max_duration <= 15 and 16 <= max_chars <= 100 and 0 <= max_gap <= 1):
        raise ValueError("断句上限需为 2–15 秒、16–100 字，停顿为 0–1 秒")
    terminal = re.compile(r"[。！？!?][」』）)]*$")
    continuation = re.compile(r"(?:けれども|けど|ので|のに|から|って|ても|たり|ながら|ため|より|なら|[がをのにとでてやもは、,])$")
    response = re.compile(r"^(?:うん|ううん|はい|ええ|そう|そうだね|そうなの|なるほど|ありがとう(?:ございます)?|えー?と|えっと|あのー?|あー?|えー?|おー?|へー?|やったー?|嬉しい|かわいい|ふふっ?|んふふっ?)[。！？!?、〜～ー…]*$")
    reply_start = re.compile(r"^(?:うん|ううん|はい|いや|え[、っ!?]|そう|おかえり|こんにちは|こんばんは|はじめまして|初めまして|やだ|ありがとう|ごめんなさい|嬉しい|かわいい)")
    groups = []
    for cue in sorted(segments, key=lambda s: s["start"]):
        group = groups[-1] if groups else []
        prev = group[-1] if group else None
        text = cue.get("ja", "").strip()
        if prev:
            before = prev.get("ja", "").strip()
            same_speaker = (set(prev.get("speaker_ids", [])) == set(cue.get("speaker_ids", []))
                            and prev.get("speaker", "") == cue.get("speaker", ""))
            # ponytail: grammar/pause heuristic; unknown speakers still need preview/listening.
            join = (text and before and same_speaker and not cue.get("reviewed") and not prev.get("reviewed")
                    and 0 <= cue["start"] - prev["end"] <= max_gap
                    and cue["end"] - group[0]["start"] <= max_duration
                    and sum(len(s.get("ja", "")) for s in group) + len(text) <= max_chars
                    and bool(cue.get("zh", "").strip()) == bool(prev.get("zh", "").strip())
                    and not terminal.search(before) and continuation.search(before)
                    and not before.endswith(("かも", "なの", "待って", "呼んで"))
                    and not response.fullmatch(before) and not response.fullmatch(text) and not reply_start.search(text)
                    and not {"possible_silence", "repetition", "boundary_review"}.intersection(prev.get("flags", []) + cue.get("flags", [])))
            if join:
                group.append(cue)
                continue
        groups.append([cue])
    changes = []
    for group in groups:
        if len(group) < 2:
            continue
        merged = copy.deepcopy(group[0])
        merged.update(end=group[-1]["end"], ja="".join(s["ja"].strip() for s in group),
                      zh=" ".join(s.get("zh", "").strip() for s in group).strip(), reviewed=False,
                      words=[copy.deepcopy(w) for s in group for w in s.get("words", [])],
                      flags=list(dict.fromkeys([f for s in group for f in s.get("flags", [])] + ["sentence_merge"])))
        if all("confidence" in s for s in group):
            merged["confidence"] = min(s["confidence"] for s in group)
        changes.append({"ids": [s["id"] for s in group], "segment": merged})
    return changes


def validate_speakers(speakers):
    if not isinstance(speakers, list) or len(speakers) > 24:
        raise ValueError("角色配色最多 24 人")
    seen = set()
    for row in speakers:
        if not isinstance(row, dict) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", str(row.get("id", ""))) or row["id"] in seen:
            raise ValueError("角色 ID 无效或重复")
        if not isinstance(row.get("name"), str) or not 1 <= len(row["name"].strip()) <= 40 or not re.fullmatch(r"#[0-9A-Fa-f]{6}", str(row.get("color", ""))):
            raise ValueError("请填写角色名及有效的六位十六进制颜色")
        seen.add(row["id"])
    return speakers


def speaker_colors(segment, speakers):
    palette = {s["id"]: s["color"] for s in validate_speakers(speakers)}
    ids = segment.get("speaker_ids", [])
    return [palette[i] for i in dict.fromkeys(ids) if i in palette]

_TIMING = re.compile(r"(\d{1,3}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,3}):(\d{2}):(\d{2})[,.](\d{1,3})")


def validate_ranges(ranges, duration, allow_empty=False):
    """An edit decision list, in playback order. Source media stays untouched."""
    if not isinstance(ranges, list) or len(ranges) > 32 or (not ranges and not allow_empty):
        raise ValueError("拼接清单需有 1–32 段")
    result = []
    for row in ranges:
        try:
            a, b = round(float(row["start"]), 3), round(float(row["end"]), 3)
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ValueError("拼接片段时间无效") from None
        if not all(math.isfinite(v) for v in (a, b)) or not 0 <= a < b <= duration + .001:
            raise ValueError("拼接片段必须位于原视频内，且结束晚于开始")
        b = min(b, math.floor(duration * 1000) / 1000)
        if b <= a:
            raise ValueError("拼接片段过短")
        result.append({"start": a, "end": b})
    return result


def assemble_subtitles(segments, ranges):
    result, offset = [], 0.0
    for index, part in enumerate(ranges):
        for cue in segments:
            a, b = max(cue["start"], part["start"]), min(cue["end"], part["end"])
            if b > a:
                result.append({**cue, "id": f"{index}-{cue.get('id', '')}",
                               "start": round(offset + a - part["start"], 3),
                               "end": round(offset + b - part["start"], 3)})
        offset += part["end"] - part["start"]
    return result


def _seconds(parts):
    h, m, s, ms = parts
    if int(m) >= 60 or int(s) >= 60:
        raise ValueError("字幕时间格式不正确：分和秒必须小于 60")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def parse_srt(text: str, language: str = "ja") -> list:
    if language not in {"ja", "zh"}:
        raise ValueError("字幕语言必须是 ja 或 zh")
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff").strip()
    segments = []
    for block in re.split(r"\n\s*\n", text):
        lines = block.splitlines()
        match = None
        for idx, line in enumerate(lines[:2]):
            match = _TIMING.search(line)
            if match:
                break
        if not match:
            if block.strip():
                raise ValueError("无法解析 SRT：每条字幕需包含序号、时间轴和文字")
            continue
        start, end = _seconds(match.groups()[:4]), _seconds(match.groups()[4:])
        if end <= start:
            raise ValueError("字幕结束时间必须晚于开始时间")
        content = "\n".join(lines[idx + 1:]).strip()
        content = html.unescape(re.sub(r"</?(?:b|i|u|font)(?:\s[^>]*)?>", "", content, flags=re.I))
        if not content:
            continue
        segments.append({"id": uuid.uuid4().hex[:12], "start": start, "end": end,
                         "ja": content if language == "ja" else "", "zh": content if language == "zh" else "",
                         "speaker": "", "reviewed": False, "confidence": 1.0,
                         "words": [], "flags": ["imported"]})
    return sorted(segments, key=lambda s: (s["start"], s["end"]))


def _cue_text(segment, language):
    if language not in {"ja", "zh", "bilingual"}:
        raise ValueError("字幕语言必须是 ja、zh 或 bilingual")
    if language == "bilingual":
        return "\n".join(str(segment.get(key, "")).strip() for key in ("zh", "ja") if str(segment.get(key, "")).strip())
    return str(segment.get(language, "")).strip()


def _clip_cues(segments, start, end, language):
    if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
        raise ValueError("导出时间范围无效")
    for seg in sorted(segments, key=lambda x: (float(x["start"]), float(x["end"]))):
        left, right = max(start, float(seg["start"])), min(end, float(seg["end"]))
        text = _cue_text(seg, language)
        if right > left and text:
            yield left - start, right - start, text


def _time(value: float, ass=False):
    units = 100 if ass else 1000
    total = max(0, round(value * units))
    seconds, fraction = divmod(total, units)
    hours, remain = divmod(seconds, 3600)
    minutes, seconds = divmod(remain, 60)
    return (f"{hours}:{minutes:02}:{seconds:02}.{fraction:02}" if ass else
            f"{hours:02}:{minutes:02}:{seconds:02},{fraction:03}")


def render_srt(segments, start, end, language="bilingual") -> str:
    cues = []
    for idx, (left, right, text) in enumerate(_clip_cues(segments, start, end, language), 1):
        cues.append(f"{idx}\n{_time(left)} --> {_time(right)}\n{text}\n")
    return "\n".join(cues)


def render_ass(segments, start, end, language="bilingual", speakers=None) -> str:
    header = """[Script Info]
Title: 烤肉工房
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Heiti SC,52,&H00FFFFFF,&H000000FF,&H00101018,&H78000000,-1,0,0,0,100,100,0,0,1,3,1,2,80,80,55,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    next(_clip_cues([], start, end, language), None)  # Validate even an empty export.
    lines = []
    for segment in sorted(segments, key=lambda s: (s['start'], s['end'])):
        for left, right, text in _clip_cues([segment], start, end, language):
            # Braces cannot be allowed to become user-supplied ASS override commands.
            text = text.replace("\\", "＼").replace("{", "｛").replace("}", "｝").replace("\r", "").replace("\n", r"\N")
            colors = speaker_colors(segment, speakers if speakers is not None else DEFAULT_SPEAKERS)
            def event(layer, content):
                return f"Dialogue: {layer},{_time(left, True)},{_time(right, True)},Default,,0,0,0,,{content}"
            def ass_color(color):
                return '&H' + color[5:7] + color[3:5] + color[1:3] + '&'
            if len(colors) <= 1:
                prefix = r'{\1c' + ass_color(colors[0]) + '}' if colors else ''
                lines.append(event(0, prefix + text))
                continue
            # Each glyph receives the same horizontal colour bands. A black base
            # preserves the outline at band boundaries; colour layers have no border.
            wrapped = []
            for raw in text.split(r'\N'):
                line, units = '', 0
                for char in raw:
                    width = 2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1
                    if units + width > 64:
                        wrapped.append(line); line, units = '', 0
                    line += char; units += width
                wrapped.append(line)
            for index, line in enumerate(wrapped):
                bottom = 1025 - (len(wrapped)-index-1)*64
                pos = rf'\an2\pos(960,{bottom})\q2'
                lines.append(event(0, '{' + pos + r'\1c&H101018&}' + line))
                for band, color in enumerate(colors):
                    top = 0 if band == 0 else round(bottom-52 + 52*band/len(colors))
                    edge = 1080 if band == len(colors)-1 else round(bottom-52 + 52*(band+1)/len(colors))
                    tags = pos + rf'\bord0\shad0\1c{ass_color(color)}\clip(0,{top},1920,{edge})'
                    lines.append(event(1, '{' + tags + '}' + line))
    return header + "\n".join(lines) + "\n"
