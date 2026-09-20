import copy
import pytest
from app import store


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA", tmp_path)
    return store.create_project(tmp_path / "video.mp4", {"duration": 100, "width": 1280, "height": 720})


def segment(id="s1", start=10, end=15, zh=""):
    return {"id": id, "start": start, "end": end, "ja": "こんにちは", "zh": zh, "reviewed": False}


def test_optimistic_save_protects_newer_edits(project):
    p = store.patch_project(project["id"], {"revision": 0, "segments": [segment()]})
    with pytest.raises(store.ConflictError):
        store.patch_project(p["id"], {"revision": 0, "segments": []})
    assert store.get_project(p["id"])["segments"][0]["ja"] == "こんにちは"


@pytest.mark.parametrize("start,end", [(10, 9), (-1, 9), (0, 101), (float("nan"), 10), (0, float("inf")), (1.0001, 1.0002), (100.0001, 100.0002)])
def test_invalid_times_not_written(project, start, end):
    with pytest.raises(ValueError):
        store.patch_project(project["id"], {"revision": 0, "segments": [segment(start=start, end=end)]})
    assert store.get_project(project["id"])["revision"] == 0


def test_translation_preserves_manual_edits(project):
    baseline = store.patch_project(project["id"], {"revision": 0, "segments": [segment(), segment("s2", 20, 25)]})
    edited = copy.deepcopy(baseline["segments"])
    edited[0]["zh"] = "人工翻译"
    store.patch_project(project["id"], {"revision": 1, "segments": edited})
    result = store.merge_job_result(project["id"], {"translations": [{"id": "s1", "zh": "机器翻译"}, {"id": "s2", "zh": "你好"}]}, baseline)
    assert result["skipped_translations"] == 1
    p = store.get_project(project["id"])
    assert [s["zh"] for s in p["segments"]] == ["人工翻译", "你好"]


def test_scoped_transcription_preserves_outside(project):
    baseline = store.patch_project(project["id"], {"revision": 0, "segments": [segment(), segment("s2", 70, 75)]})
    store.merge_job_result(project["id"], {"segments": [segment("new", 9, 16)], "replace_range": [5, 30]}, baseline)
    assert [s["id"] for s in store.get_project(project["id"])["segments"]] == ["new", "s2"]


def test_transcription_does_not_overwrite_concurrent_edits(project):
    baseline = store.patch_project(project["id"], {"revision": 0, "segments": [segment()]})
    edited = copy.deepcopy(baseline["segments"])
    edited[0]["ja"] = "訂正しました"
    store.patch_project(project["id"], {"revision": 1, "segments": edited})
    with pytest.raises(store.ConflictError):
        store.merge_job_result(project["id"], {"segments": [], "replace_range": [0, 30]}, baseline)
    assert store.get_project(project["id"])["segments"][0]["ja"] == "訂正しました"


def test_credentials_cannot_be_persisted(project):
    p = store.patch_project(project["id"], {"revision": 0, "settings": {"api_key": "SECRET", "provider": "api"}})
    assert "api_key" not in p["settings"]
    assert "SECRET" not in store.project_path(p["id"]).read_text()


def test_path_traversal_rejected(project):
    with pytest.raises(KeyError):
        store.get_project("../../secret")


def test_edit_list_order_validation_and_revision(project):
    ranges = [{'start': 70, 'end': 80}, {'start': 10, 'end': 20}]
    saved = store.patch_project(project['id'], {'revision': 0, 'edit_ranges': ranges, 'settings': {'story_mode': 'story'}})
    assert saved['edit_ranges'] == ranges
    with pytest.raises(ValueError):
        store.patch_project(project['id'], {'revision': 1, 'edit_ranges': [{'start': 1, 'end': 101}]})
    assert store.get_project(project['id'])['edit_ranges'] == ranges
    assert store.get_project(project['id'])['revision'] == 1


def test_speaker_palette_and_chorus_validation(project):
    from app.subtitles import DEFAULT_SPEAKERS
    cue = {**segment(), 'speaker_ids': ['lamy', 'korone', 'azki']}
    saved = store.patch_project(project['id'], {'revision': 0, 'speakers': DEFAULT_SPEAKERS, 'segments': [cue]})
    assert saved['segments'][0]['speaker_ids'] == ['lamy', 'korone', 'azki']
    with pytest.raises(ValueError):
        store.patch_project(project['id'], {'revision': 1, 'speakers': [{'id':'x','name':'X','color':'red;attack'}]})
    with pytest.raises(ValueError):
        store.patch_project(project['id'], {'revision': 1, 'segments': [{**cue,'speaker_ids':['lamy','lamy']}]})


def test_saved_clips_survive_rescan_and_keep_ordered_ranges(project):
    clips = [dict(id='a', start=10, end=40, title='A', selected=True,
                  ranges=[dict(start=30, end=40), dict(start=10, end=20)]),
             dict(id='b', start=50, end=60, title='B', selected=True)]
    p = store.patch_project(project['id'], dict(revision=0, highlights=clips,
                                               segments=[segment(zh='已校对译文')]))
    store.merge_job_result(p['id'], {'highlights': [dict(id='new', start=70, end=80)]}, p)
    result = store.get_project(p['id'])
    assert [h['id'] for h in result['highlights']] == ['a', 'b', 'new']
    assert result['highlights'][0]['ranges'][0]['start'] == 30
    assert result['segments'][0]['zh'] == '已校对译文'
