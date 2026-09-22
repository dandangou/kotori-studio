"""Behavioral tests for time rebasing, candidate selection and actual media work."""
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

from app import pipeline
from app.subtitles import parse_srt, render_ass, render_srt


class SubtitleTests(unittest.TestCase):
    def test_clip_times_rebased_and_clamped(self):
        segments = [{"start": 8.1, "end": 11.5, "ja": "こんにちは", "zh": "你好"},
                    {"start": 13, "end": 18, "ja": "またね", "zh": "再见"},
                    {"start": 22, "end": 23, "ja": "outside"}]
        result = render_srt(segments, 10, 15)
        self.assertIn("00:00:00,000 --> 00:00:01,500", result)
        self.assertIn("00:00:03,000 --> 00:00:05,000", result)
        self.assertIn("你好\nこんにちは", result)
        self.assertNotIn("outside", result)
        parsed = parse_srt(result, "zh")
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[1]["end"], 5)

    def test_srt_bom_crlf_and_decimal_ms(self):
        parsed = parse_srt("\ufeff1\r\n00:00:01.2 --> 00:00:03,250\r\n<b>おはよう</b>\r\n")
        self.assertEqual(parsed[0]["start"], 1.2)
        self.assertEqual(parsed[0]["ja"], "おはよう")

    def test_ass_user_text_is_not_override_code(self):
        result = render_ass([{"start": 0, "end": 2, "ja": r"{\pos(0,0)}hello"}], 0, 2, "ja")
        self.assertNotIn(r"{\pos", result)
        self.assertIn("0:00:02.00", result)

    def test_speaker_colors_and_layered_chorus_survive_assembly(self):
        from app.subtitles import assemble_subtitles, DEFAULT_SPEAKERS
        cue = {"id":"chorus", "start":10, "end":12, "zh":"彼方！", "speaker_ids":["lamy","korone","azki"]}
        mapped = assemble_subtitles([cue], [{"start":10,"end":12}])
        ass = render_ass(mapped, 0, 2, "zh", DEFAULT_SPEAKERS)
        self.assertEqual(ass.count("Dialogue:"), 4)
        for color in ("&HFFDE94&", "&H4A99C9&", "&H765DE6&"):
            self.assertIn(color, ass)
        self.assertEqual(ass.count(r"\clip("), 3)
        self.assertIn("00:00:00,000 --> 00:00:02,000\n彼方！", render_srt(mapped, 0, 2, "zh"))

    def test_invalid_range_rejected(self):
        with self.assertRaises(ValueError):
            render_srt([], 3, 2)
        with self.assertRaises(ValueError):
            pipeline.validate_range({"duration": 10}, {"start": float("nan"), "end": 8})
        with self.assertRaises(ValueError):
            parse_srt("1\n00:00:03,000 --> 00:00:02,000\nwrong")


class ScoringTests(unittest.TestCase):
    def test_optional_replay_prior_finds_quiet_hotspot_and_ignores_invalid_metadata(self):
        from app.acquire import normalize_heatmap
        raw = [{"start_time": 180, "end_time": 220, "value": 1},
               {"start_time": 0, "end_time": 20, "value": float("nan")}, None,
               {"start_time": 600, "end_time": 601, "value": 1}]
        self.assertEqual(len(normalize_heatmap(raw, 600)), 1)
        args = ([.1] * 6000, .1, 600)
        self.assertEqual(pipeline.score_highlights(*args), [])
        result = pipeline.score_highlights(*args, heatmap=raw, count=1)
        self.assertEqual(result[0]["method"], "replay")
        self.assertLess(result[0]["start"], 220)
        self.assertGreater(result[0]["end"], 180)
        self.assertIn("不是精彩概率", result[0]["reason"])

    def test_silence_and_stationary_tone_are_not_highlights(self):
        self.assertEqual(pipeline.score_highlights([0] * 6000, .1, 600), [])
        self.assertEqual(pipeline.score_highlights([.1] * 6000, .1, 600), [])

    def test_peaks_have_context_and_no_overlap(self):
        import numpy as np
        audio = np.full(6000, .02)
        rng = np.random.default_rng(14)
        for where in [90, 220, 410]:
            audio[where * 10:(where + 10) * 10] = rng.uniform(.1, .7, 100)
        results = pipeline.score_highlights(audio, .1, 600, mode="gaming", clip_min=30, clip_max=80)
        self.assertGreaterEqual(len(results), 3)
        for item in results:
            self.assertGreaterEqual(item["start"], 0)
            self.assertLessEqual(item["end"], 600)
            self.assertGreaterEqual(item["end"] - item["start"], 30)
            self.assertLessEqual(item["end"] - item["start"], 80)
            self.assertEqual(item["method"], "audio")
        chronological = sorted(results, key=lambda h: h["start"])
        self.assertTrue(all(a["end"] <= b["start"] for a, b in zip(chronological, chronological[1:])))

    def test_chat_mode_prioritizes_story_terms(self):
        segments = [{"start": 100, "end": 130, "ja": "実は昔初めて秘密の話をした。本当の夢。"}]
        result = pipeline.score_highlights([.05] * 3000, .1, 300, segments, mode="chat", clip_min=40, clip_max=100)
        self.assertTrue(result)
        self.assertEqual(result[0]["method"], "transcript")
        self.assertLess(result[0]["start"], 130)
        self.assertGreater(result[0]["end"], 100)

    def test_asr_word_timestamp_offsets_and_flags(self):
        raw = [{"start": 0, "end": 3, "text": "テスト。", "avg_logprob": -1.1,
                "no_speech_prob": .6, "words": [{"word": "テスト。", "start": .2, "end": 2.5, "probability": .7}]}]
        result = pipeline._asr_segments(raw, 20, 20, 30)
        self.assertEqual(result[0]["start"], 20.2)
        self.assertEqual(result[0]["end"], 22.5)
        self.assertIn("low_confidence", result[0]["flags"])
        self.assertIn("possible_silence", result[0]["flags"])

    def test_semantic_context_finishes_sentence_and_respects_maximum(self):
        class FakeModel:
            def __init__(self, *args):
                pass
            def complete(self, *args, **kwargs):
                return '[{"start_id":"s1","end_id":"s2","title":"遇敌惊吓","reason":"出现敌人后的反应","score":90}]'
        project = {"id": "boundaries", "duration": 26, "settings": {"clip_min": 5, "clip_max": 25}, "segments": [
            {"id": "a", "start": 7.96, "end": 9.6, "ja": "ちょっと待って。"},
            {"id": "b", "start": 9.92, "end": 11.42, "ja": "後ろに敵がいます。"},
            {"id": "c", "start": 11.76, "end": 13.8, "ja": "嘘でしょう。びっくりした。"},
            {"id": "d", "start": 14.04, "end": 16.62, "ja": "今のは本当に危なかったですね。"}]}
        with patch.object(pipeline, "LocalLanguageModel", FakeModel):
            result = pipeline.semantic(project, {}, "/unused")
            self.assertAlmostEqual(result["highlights"][0]["start"], 7.92)
            self.assertEqual(result["highlights"][0]["end"], 16.62)
            self.assertEqual(pipeline.semantic(project, {"clip_max": 7}, "/unused")["highlights"], [])

    def test_cached_model_requires_all_weight_shards(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = Path(tmp)
            for name in ["config.json", "tokenizer.json", "tokenizer_config.json"]:
                (snapshot / name).write_text("{}")
            (snapshot / "model.safetensors.index.json").write_text(json.dumps({"weight_map": {"a": "one.safetensors", "b": "two.safetensors"}}))
            (snapshot / "one.safetensors").write_bytes(b"some")
            self.assertFalse(pipeline._complete_snapshot(snapshot, "qwen3-4b"))
            (snapshot / "two.safetensors").write_bytes(b"some")
            self.assertTrue(pipeline._complete_snapshot(snapshot, "qwen3-4b"))


class ResumeTests(unittest.TestCase):
    def test_asr_resume_reuses_complete_chunks_but_glossary_invalidates(self):
        import numpy as np
        calls = []
        def recognize(*args, **kwargs):
            calls.append(1)
            return {"segments": [{"start": 4, "end": 5, "text": "テスト", "words": [{"start": 4, "end": 5, "word": "テスト"}]}]}
        def extract(source, start, end, target):
            samples = (np.sin(np.arange(16000) * .2) * 10000).astype("<i2")
            with wave.open(str(target), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(16000)
                output.writeframes(samples.tobytes())
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "video.mp4"
            source.write_bytes(b"source stat")
            project = {"id": "resume", "source_path": str(source), "duration": 480, "settings": {}}
            with patch.dict(sys.modules, {"mlx_whisper": SimpleNamespace(transcribe=recognize)}), patch.object(pipeline, "_require_model", return_value="/cached/model"), patch.object(pipeline, "_extract_audio", side_effect=extract):
                first = pipeline.transcribe(project, {}, folder)
                second = pipeline.transcribe(project, {}, folder)
                self.assertEqual(len(calls), 2)
                self.assertEqual(first, second)
                self.assertEqual(len(first["segments"]), 2)
                project["settings"]["glossary"] = "兎田ぺこら=兔田佩克拉"
                pipeline.transcribe(project, {}, folder)
                self.assertEqual(len(calls), 4)

    def test_translation_resume_and_explicit_overwrite(self):
        calls = []
        class FakeModel:
            def __init__(self, *args):
                pass
            def complete(self, *args, **kwargs):
                calls.append(1)
                return '[{"id":"s1","zh":"你好"}]'
        project = {"id": "translation", "duration": 10, "segments": [{"id": "s1", "start": 1, "end": 2, "ja": "こんにちは"}]}
        with tempfile.TemporaryDirectory() as folder, patch.object(pipeline, "LocalLanguageModel", FakeModel):
            first = pipeline.translate(project, {}, folder)
            self.assertEqual(first, pipeline.translate(project, {}, folder))
            self.assertEqual(len(calls), 1)
            pipeline.translate(project, {"overwrite": True}, folder)
            self.assertEqual(len(calls), 2)

    def test_translation_retries_only_missing_ids_and_keeps_partial_checkpoint(self):
        calls = []
        outputs = iter([
            '[{"id":[],"zh":"invalid"},{"id":"s0","zh":"第一句"}]',
            '{"items":[{"id":"s0","zh":"不能覆盖"},{"id":"s2","zh":"第三句"}]}',
            'broken JSON',
            '{"id":"s1","zh":"第二句"}',
        ])
        class FakeModel:
            def __init__(self, *args):
                pass
            def complete(self, system, prompt, **kwargs):
                calls.append(json.loads(prompt.split("请翻译以下字幕：\n")[-1]))
                return next(outputs)
        project = {"id": "partial", "duration": 10, "segments": [
            {"id": f"cue{n}", "start": n, "end": n + 1, "ja": f"原文{n}"} for n in range(3)]}
        with tempfile.TemporaryDirectory() as folder, patch.object(pipeline, "LocalLanguageModel", FakeModel):
            first = pipeline.translate(project, {}, folder)
            self.assertIn("1 条字幕", first["warning"])
            self.assertEqual(first["translations"], [{"id": "cue0", "zh": "第一句"}, {"id": "cue2", "zh": "第三句"}])
            second = pipeline.translate(project, {}, folder)
            self.assertNotIn("warning", second)
            self.assertEqual([r["zh"] for r in second["translations"]], ["第一句", "第二句", "第三句"])
            self.assertEqual([[r["id"] for r in call] for call in calls], [["s0", "s1", "s2"], ["s1", "s2"], ["s1"], ["s1"]])

    def test_translation_subdivides_malformed_batches_without_reordering(self):
        calls = []
        class FakeModel:
            def __init__(self, *args):
                pass
            def complete(self, system, prompt, **kwargs):
                rows = json.loads(prompt.split("请翻译以下字幕：\n")[-1])
                calls.append(len(rows))
                if len(rows) > 1:
                    return 'not JSON'
                return json.dumps({"id": rows[0]["id"], "zh": rows[0]["ja"] + "译"})
        project = {"id": "split", "duration": 20, "segments": [
            {"id": f"cue{n}", "start": n, "end": n + 1, "ja": str(n)} for n in range(12)]}
        with tempfile.TemporaryDirectory() as folder, patch.object(pipeline, "LocalLanguageModel", FakeModel):
            result = pipeline.translate(project, {}, folder)
        self.assertNotIn("warning", result)
        self.assertEqual([r["id"] for r in result["translations"]], [s["id"] for s in project["segments"]])
        self.assertEqual(calls, [12] + [3] * 4 + [1] * 12)


@unittest.skipUnless(pipeline.ffmpeg_path(), "FFmpeg not installed")
class MediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="kotori-media-tests-")
        cls.folder = Path(cls.temp.name)
        cls.source = cls.folder / "source.mp4"
        subprocess.run([pipeline.ffmpeg_path(), "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=6", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=6", "-c:v", "libx264", "-threads", "2", "-c:a", "aac", "-shortest", str(cls.source)], check=True)
        cls.project = {"id": "test", "name": "测试", "source_path": str(cls.source), "duration": 6,
                       "source_url": "https://example.com/watch", "source_title": "测试视频",
                       "segments": [{"id": "s1", "start": 1, "end": 3, "ja": "こんにちは", "zh": "你好"}]}

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_probe_waveform_export_proxy_roundtrip(self):
        metadata = pipeline.probe_media(self.source)
        self.assertAlmostEqual(metadata["duration"], 6, delta=.1)
        result = pipeline.run_job("waveform", self.project, {}, self.folder)
        self.assertTrue(59 <= len(result["waveform"]) <= 62)
        self.assertTrue(all(0 <= v <= 1 and math.isfinite(v) for v in result["waveform"]))
        export = pipeline.export(self.project, {"start": 1.37, "end": 3.91, "format": "video"}, self.folder)
        video = next(e for e in export["exports"] if e["kind"] == "video")
        self.assertAlmostEqual(pipeline.probe_media(video["path"])["duration"], 2.54, delta=.1)
        info = next(e for e in export["exports"] if e["kind"] == "info")
        self.assertIn("https://example.com/watch", Path(info["path"]).read_text())
        proxy = pipeline.make_proxy(self.project, {}, self.folder)
        self.assertTrue(Path(proxy["proxy_path"]).is_file())

    def test_burn_unicode_path(self):
        odd_folder = self.folder / "中 文's:字幕,"
        odd_folder.mkdir(exist_ok=True)
        export = pipeline.export(self.project, {"start": 1.25, "end": 3.5, "format": "burn"}, odd_folder)
        video = next(e for e in export["exports"] if e["kind"] == "burn")
        self.assertAlmostEqual(pipeline.probe_media(video["path"])["duration"], 2.25, delta=.1)

    def test_export_resolution_caps_source_and_scales_discontinuous_video(self):
        result = pipeline.export(self.project, {"start": 1, "end": 1.2, "format": "video", "output_height": 1080}, self.folder)
        self.assertEqual(pipeline.probe_media(result["exports"][0]["path"])["height"], 180)
        source = self.folder / "hd.mp4"
        subprocess.run([pipeline.ffmpeg_path(), "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=s=1920x1080:r=25:d=1", "-c:v", "libx264", "-threads", "2", str(source)], check=True)
        project = {**self.project, "source_path": str(source), "duration": 1, "width": 1920, "height": 1080}
        result = pipeline.export(project, {"ranges": [{"start": .1, "end": .3}, {"start": .5, "end": .7}], "format": "video", "output_height": 720}, self.folder)
        info = pipeline.probe_media(result["exports"][0]["path"])
        self.assertEqual((info["width"], info["height"]), (1280, 720))

    def test_export_video_without_audio_track(self):
        source = self.folder / "mute.mp4"
        subprocess.run([pipeline.ffmpeg_path(), "-nostdin", "-loglevel", "error", "-y", "-i", str(self.source), "-c:v", "copy", "-an", str(source)], check=True)
        project = {**self.project, "source_path": str(source)}
        result = pipeline.export(project, {"start": .5, "end": 2, "format": "video"}, self.folder)
        output = next(e for e in result["exports"] if e["kind"] == "video")
        self.assertAlmostEqual(pipeline.probe_media(output["path"])["duration"], 1.5, delta=.1)

    def test_discontinuous_export_remaps_subtitles_and_video(self):
        from app.subtitles import validate_ranges, assemble_subtitles
        ranges = [{"start": 1.2, "end": 1.8}, {"start": 4, "end": 5.2}, {"start": 2.2, "end": 2.8}]
        mapped = assemble_subtitles(self.project["segments"], ranges)
        self.assertEqual([(s["start"], s["end"]) for s in mapped], [(0, .6), (1.8, 2.4)])
        for bad in ([], [{"start": float("nan"), "end": 3}], [{"start": 4, "end": 7}]):
            with self.assertRaises(ValueError):
                validate_ranges(bad, 6)
        result = pipeline.export(self.project, {"ranges": ranges, "format": "burn", "language": "zh"}, self.folder)
        output = next(e for e in result["exports"] if e["kind"] == "burn")
        self.assertAlmostEqual(pipeline.probe_media(output["path"])["duration"], 2.4, delta=.12)


    @unittest.skipUnless(Path("/System/Library/Fonts/STHeiti Medium.ttc").exists(), "macOS CJK font validation")
    def test_burn_font_covers_simplified_chinese_and_japanese(self):
        source = self.folder / "font.ass"
        source.write_text(render_ass([{"start": 0, "end": 3, "zh": "大家好，今天开始游戏直播。粉丝、谢谢、学习、歌声。",
                                      "ja": "兎田ぺこら、宝鐘マリン、さくらみこ、星街すいせい。"}], 0, 3), encoding="utf-8")
        result = subprocess.run([pipeline.ffmpeg_path(), "-nostdin", "-hide_banner", "-loglevel", "verbose", "-i", str(self.source),
                                 "-vf", "ass=font.ass", "-frames:v", "1", "-f", "null", "-"], cwd=self.folder, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("failed to find any fallback", result.stderr)
        self.assertNotIn("Error opening font", result.stderr)

    def test_worker_stdout_is_json_and_output_persisted(self):
        request = self.folder / "request.json"
        result_path = self.folder / "result.json"
        request.write_text(json.dumps({"job_id": "test", "kind": "waveform", "project": self.project, "options": {}, "data_dir": str(self.folder), "output_path": str(result_path)}))
        run = subprocess.run([sys.executable, "-m", "app.worker", str(request)], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        events = [json.loads(line) for line in run.stdout.splitlines()]
        self.assertTrue(events)
        self.assertEqual(events[-1]["progress"], 1)
        self.assertIn("waveform", json.loads(result_path.read_text()))



class StoryTests(unittest.TestCase):
    def test_story_removes_interruption_and_links_later_callback_without_duration_cap(self):
        class Model:
            def __init__(self, *args):
                self.calls = 0
            def complete(self, system, prompt, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return json.dumps([{"title": "找钥匙", "ranges": [{"start_id": "s0", "end_id": "s0"}, {"start_id": "s2", "end_id": "s2"}]}])
                if self.calls == 2:
                    return json.dumps([{"title": "找到钥匙", "ranges": [{"start_id": "s3", "end_id": "s3"}]}])
                return json.dumps([{"title": "钥匙失而复得", "ids": [0, 1]}])
        project = {"id": "story", "duration": 1300, "segments": [
            {"id": "a", "start": 10, "end": 100, "ja": "鍵をなくした"},
            {"id": "b", "start": 100, "end": 110, "ja": "スパチャありがとう"},
            {"id": "c", "start": 110, "end": 190, "ja": "鍵を探す"},
            {"id": "d", "start": 1000, "end": 1100, "ja": "さっきの鍵が見つかった"}]}
        with tempfile.TemporaryDirectory() as folder, patch.object(pipeline, "LocalLanguageModel", Model):
            result = pipeline.semantic(project, {"story_mode": "story", "clip_max": 120}, folder)
        ranges = result["highlights"][0]["ranges"]
        self.assertEqual(ranges, [{"start": 10, "end": 100}, {"start": 110, "end": 190}, {"start": 1000, "end": 1100}])
        self.assertGreater(sum(r["end"]-r["start"] for r in ranges), 120)


if __name__ == "__main__":
    unittest.main()
