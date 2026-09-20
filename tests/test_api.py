"""Real API boundary checks; FFmpeg integration is exercised separately."""
import pytest
from fastapi.testclient import TestClient
from app import main, store


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA", tmp_path)
    with TestClient(main.app) as c:
        yield c


def make_project():
    return store.create_project(store.DATA / "sample.mp4", {"duration": 60})


def test_local_health_and_foreign_origin(client):
    assert client.get("/api/health").json()["app"] == "kotori-studio"
    r = client.post("/api/projects", json={"path": "/nonexistent"}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    assert client.get("/api/projects", headers={"host": "evil.example"}).status_code == 400


def test_invalid_file_does_not_create_project(client):
    r = client.post("/api/projects", json={"path": "/not-a-real-video.mp4"})
    assert r.status_code == 400
    assert client.get("/api/projects").json() == []


def test_autosave_conflict_returns_409(client):
    p = make_project()
    assert client.patch(f'/api/projects/{p["id"]}', json={"revision": 0, "name": "新的名称"}).status_code == 200
    assert client.patch(f'/api/projects/{p["id"]}', json={"revision": 0, "name": "过时名称"}).status_code == 409
    assert client.get(f'/api/projects/{p["id"]}').json()["name"] == "新的名称"


def test_invalid_export_range_never_queued(client):
    p = make_project()
    r = client.post(f'/api/projects/{p["id"]}/jobs', json={"kind": "export", "options": {"start": 20, "end": 19}})
    assert r.status_code == 400
    assert client.get("/api/jobs").json() == []


def test_srt_import_uses_source_timeline(client):
    p = make_project()
    text = "1\n00:00:02,500 --> 00:00:04,800\nこんにちは\n\n2\n00:00:08,000 --> 00:00:09,900\nありがとう\n"
    r = client.post(f'/api/projects/{p["id"]}/subtitles', json={"text": text, "language": "ja", "revision": 0})
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["segments"][0]["start"] == 2.5
    chinese = text.replace("こんにちは", "你好").replace("ありがとう", "谢谢")
    r = client.post(f'/api/projects/{p["id"]}/subtitles', json={"text": chinese, "language": "zh", "revision": p["revision"]})
    assert r.status_code == 200, r.text
    assert len(r.json()["segments"]) == 2
    assert r.json()["segments"][0]["ja"] == "こんにちは"
    assert r.json()["segments"][0]["zh"] == "你好"


def test_export_download_cannot_read_arbitrary_file(client, tmp_path):
    p = make_project()
    p["exports"] = [{"id": "bad", "path": "/etc/hosts", "name": "x.txt"}]
    store.atomic_json(store.project_path(p["id"]), p)
    assert client.get(f'/api/projects/{p["id"]}/exports/bad').status_code == 404


def test_out_of_range_srt_preserves_existing_work(client):
    p = make_project()
    cue = {"id": "existing", "start": 1, "end": 3, "ja": "元の字幕", "zh": "人工翻译"}
    store.patch_project(p["id"], {"revision": 0, "segments": [cue]})
    response = client.post(f'/api/projects/{p["id"]}/subtitles', json={"language": "ja", "text": "1\n00:10:00,000 --> 00:10:03,000\nwrong video\n"})
    assert response.status_code == 400
    assert store.get_project(p["id"])["segments"][0]["zh"] == "人工翻译"
