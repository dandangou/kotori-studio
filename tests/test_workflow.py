"""End-to-end local queue tests against real FFmpeg, without downloading models."""
import json
import time
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from app import main, store
from app.pipeline import ffmpeg_path
import subprocess


@pytest.fixture
def live(tmp_path, monkeypatch):
    if not ffmpeg_path():
        pytest.skip("FFmpeg is not installed")
    monkeypatch.setattr(store, "DATA", tmp_path / "data")
    source = tmp_path / "本地样例.mp4"
    subprocess.run([ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=navy:s=320x180:r=24:d=6",
                    "-f", "lavfi", "-i", "sine=frequency=480:sample_rate=16000:duration=6", "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", "-shortest", str(source)], check=True, timeout=30)
    with TestClient(main.app) as client:
        yield client, source


def finish(client, job_id, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = next(j for j in client.get("/api/jobs").json() if j["id"] == job_id)
        if job["status"] in {"completed", "failed", "cancelled"}:
            assert job["status"] == "completed", job
            return job
        time.sleep(.08)
    pytest.fail("worker did not complete")


def test_import_waveform_edit_export_range_and_download(live):
    client, source = live
    imported = client.post("/api/projects", json={"path": str(source), "source_url": "https://example.org/source", "name": "本地验证"})
    assert imported.status_code == 200, imported.text
    p = imported.json()
    waveform_job = client.get("/api/jobs").json()[0]
    finish(client, waveform_job["id"])
    p = client.get(f'/api/projects/{p["id"]}').json()
    assert 50 < len(p["waveform"]) <= 65
    assert p["revision"] == 1
    segment = {"id": "s1", "start": 1, "end": 4, "ja": "こんにちは", "zh": "你好", "reviewed": True}
    r = client.patch(f'/api/projects/{p["id"]}', json={"revision": p["revision"], "segments": [segment]})
    assert r.status_code == 200, r.text
    for format in ("srt", "burn"):
        r = client.post(f'/api/projects/{p["id"]}/jobs', json={"kind": "export", "options": {"format": format, "start": 2, "end": 5, "language": "bilingual"}})
        finish(client, r.json()["id"])
    p = client.get(f'/api/projects/{p["id"]}').json()
    assert {e["kind"] for e in p["exports"]} == {"srt", "burn", "info"}
    srt = next(e for e in p["exports"] if e["kind"] == "srt")
    download = client.get(f'/api/projects/{p["id"]}/exports/{srt["id"]}')
    assert download.status_code == 200
    assert "00:00:00,000 --> 00:00:02,000" in download.text
    assert "你好\nこんにちは" in download.text
    media = client.get(f'/api/projects/{p["id"]}/media', headers={"range": "bytes=0-99"})
    assert media.status_code == 206
    assert len(media.content) == 100
    assert source.exists()
    info = next(e for e in p["exports"] if e["kind"] == "info")
    assert "https://example.org/source" in Path(info["path"]).read_text()


def test_cancel_queued_job_never_starts_and_hides_credentials(live):
    client, source = live
    # Hold the consumer lock until cancellation marks the pending job.
    with main.manager.lock:
        job = main.manager.submit("translate", options={"api_key": "TEST_SECRET"})
        cancelled = main.manager.cancel(job["id"])
    assert cancelled["status"] == "cancelled"
    assert "TEST_SECRET" not in json.dumps(client.get("/api/jobs").json())
    assert "TEST_SECRET" not in (store.DATA / "jobs" / f'{job["id"]}.json').read_text()
    assert job["id"] not in main.manager.options


def test_youtube_url_rejects_arbitrary_host(live):
    client, _ = live
    for url in ("http://youtube.com/watch?v=x", "https://youtube.com.evil.test/watch?v=x", "file:///etc/hosts", "https://www.youtube.com/playlist?list=x"):
        assert client.post("/api/sources/youtube", json={"url": url}).status_code == 400


def test_upload_survives_full_analysis_queue(live, monkeypatch):
    client, source = live
    def full(*args, **kwargs):
        raise ValueError("队列已满")
    monkeypatch.setattr(main.manager, "submit", full)
    with source.open("rb") as f:
        response = client.post("/api/upload", files={"file": ("uploaded.mp4", f, "video/mp4")})
    assert response.status_code == 200, response.text
    p = response.json()
    assert p["import_notice"]
    assert Path(p["source_path"]).is_file()
    assert store.get_project(p["id"])["source_path"] == p["source_path"]


def test_queued_asr_preserves_edits_made_before_worker_starts(live):
    client, source = live
    p = store.create_project(source, {"duration": 6})
    with main.manager.lock:
        job = main.manager.submit("transcribe", p["id"], {"start": 0, "end": 6})
        store.patch_project(p["id"], {"revision": 0, "segments": [{"id": "manual", "start": 1, "end": 3, "ja": "手動", "zh": "人工修改"}]})
    for _ in range(100):
        status = next(j for j in client.get("/api/jobs").json() if j["id"] == job["id"])
        if status["status"] == "failed":
            break
        time.sleep(.02)
    assert status["status"] == "failed"
    assert "排队期间" in status["error"]
    assert store.get_project(p["id"])["segments"][0]["zh"] == "人工修改"
