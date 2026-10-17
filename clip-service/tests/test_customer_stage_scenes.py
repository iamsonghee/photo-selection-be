"""웨딩 본식 단계 기준 장면 — 촬영 시각 없는 보정본을 파일명 연번 순서로 놓고, 흔들림 확인이 사진마다 판정한 단계로 나눈다.
설계: photo-selection-fe/docs/wedding-ceremony-scene-plan.md, PRD: wedding-ceremony-scene-prd.md."""
import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import numpy as np
import pytest

from app import customer_ai, gemini_quality_client
from app.gemini_quality_client import (STAGE_PROMPT_VERSION, UNKNOWN_PLACE, customer_schema, judge_stages, stage_prompt, stage_schema,
                                       stage_windows)
from app.scenes import (STAGE_MIN_LABELED_RATIO, STAGE_MIN_RUN, STAGE_WINDOW, _segment_runs, _settle_outside_main, camera_blocks,
                        camera_key, content_order, merge_camera_timelines, split_by_place)

STAGE_RULES = {"window": STAGE_WINDOW, "min_run": STAGE_MIN_RUN, "untimed_order": True, "min_labeled_ratio": STAGE_MIN_LABELED_RATIO}
UNLABELED = {UNKNOWN_PLACE}


def _untimed(blocks):
    """blocks: [(장수, 단계)] — 촬영 시각 없는(파일 수정 시각만 있는) 보정본. 반환: (사진, 사진 ID → 단계)."""
    photos, labels = [], {}
    for count, stage in blocks:
        for _ in range(count):
            n = len(photos)
            photos.append({"id": f"p{n}", "order_index": n, "filename": f"{n + 1:04}_SJ7_{3000 + n}.jpg",
                           "taken_at": "2026-10-12T08:14:31", "taken_at_source": "file"})
            labels[f"p{n}"] = stage
    return photos, labels


MAIN = {"화촉점화·혼주 입장", "신랑 입장", "신부 입장", "예식", "축가·축하공연", "행진·퇴장"}


def _vectors(photos, labels, seed=0):
    """단계마다 다른 방향 + 작은 잡음의 임베딩(사진 ID → 벡터). 판정과 상관없이 실제 단계 경계에서 내용이 바뀐다."""
    rng = np.random.default_rng(seed)
    bases: dict = {}
    out = {}
    for photo in photos:
        key = labels.get(f"truth:{photo['id']}", labels.get(photo["id"]))
        base = bases.setdefault(key, rng.normal(size=16))
        out[photo["id"]] = base + rng.normal(scale=0.05, size=16)
    return out


# --- 순서 복원 -------------------------------------------------------------------------------------------------

def test_content_order_restores_leading_export_sequence_even_with_mixed_camera_names():
    # 장준용 본식: 내보낼 때 붙인 0001~ 연번 뒤에 여러 카메라 원래 이름(SJ7·SJ8). 업로드는 역순·일부 섞임이었다.
    names = ["0003_SJ8_1404.jpg", "0001_SJ7_3213.jpg", "0004_SJ7_3216.jpg", "0002_SJ7_3214.jpg"]
    rows = [{"id": str(i), "filename": name, "order_index": i, "taken_at_source": "file"} for i, name in enumerate(names)]
    ordered = content_order(rows)
    assert [r["filename"] for r in ordered] == sorted(names)
    assert [r["order_index"] for r in ordered] == [0, 1, 2, 3]
    assert [r["order_index"] for r in rows] == [0, 1, 2, 3]  # 입력 행은 그대로


@pytest.mark.parametrize("filenames", [
    ["20261008_102030.jpg", "20261007_101010.jpg"],  # 휴대폰 날짜 이름 — 앞 8자리는 날짜
    ["0002_a.jpg", "0002_b.jpg"],  # 앞 번호 중복
    ["0002_a.jpg", "00001_b.jpg"],  # 앞 번호 자릿수가 다름(다른 체계)
    ["0001_a.jpg", "IMG_0002.jpg"],  # 체계 섞임
])
def test_content_order_keeps_ambiguous_leading_numbers(filenames):
    rows = [{"id": str(i), "filename": name, "order_index": i} for i, name in enumerate(filenames)]
    assert content_order(rows) is rows


# --- 단계 다듬기 ----------------------------------------------------------------------------------------------

def test_stage_split_uses_upload_order_when_capture_times_are_missing():
    photos, labels = _untimed([(40, "신부대기실"), (60, "예식"), (30, "행진·퇴장")])
    scenes, names = split_by_place(photos, labels, UNLABELED, "기타 장면", **STAGE_RULES)
    assert names == ["신부대기실", "예식", "행진·퇴장"] and [len(scene) for scene in scenes] == [40, 60, 30]


def test_place_split_still_requires_capture_times():
    # 홈스냅(장소 기준)은 그대로 — 시각이 없으면 장소로 나누지 않는다.
    photos, labels = _untimed([(60, "거실"), (60, "침실·침대")])
    assert split_by_place(photos, labels, UNLABELED, "기타 장면") is None


def test_short_stage_survives_and_stray_or_unknown_labels_do_not_split():
    # 신랑 입장 11장은 따로 남고, 예식 중간의 오판정 3장·알 수 없음 6장은 예식을 쪼개지 않는다.
    photos, labels = _untimed([(40, "로비·하객 맞이"), (11, "신랑 입장"), (30, "예식"), (3, "축가·축하공연"),
                               (6, UNKNOWN_PLACE), (40, "예식")])
    scenes, names = split_by_place(photos, labels, UNLABELED, "기타 장면", **STAGE_RULES)
    assert names == ["로비·하객 맞이", "신랑 입장", "예식"]
    assert [len(scene) for scene in scenes] == [40, 11, 79]


def test_stage_split_needs_enough_labels():
    labeled, labels = _untimed([(60, "예식"), (40, UNKNOWN_PLACE)])
    assert split_by_place(labeled, labels, UNLABELED, "기타 장면", **STAGE_RULES) is not None  # 60% ≥ 50%
    sparse, labels = _untimed([(40, "예식"), (60, UNKNOWN_PLACE)])
    assert split_by_place(sparse, labels, UNLABELED, "기타 장면", **STAGE_RULES) is None


def test_stage_split_follows_restored_filename_order(monkeypatch):
    # 역순으로 올라간 사진도 파일명 연번으로 되돌린 뒤 나눈다(_place_scenes가 content_order를 거친다).
    photos, labels = _untimed([(50, "신부대기실"), (50, "예식")])
    reversed_rows = [dict(photo, order_index=len(photos) - 1 - photo["order_index"]) for photo in photos]
    monkeypatch.setattr(customer_ai, "_all_rows", lambda query, page_rows=None: [{"photo_id": k, "place": v} for k, v in labels.items()])
    scenes, names = customer_ai._place_scenes(MagicMock(), "project", ["신부대기실", "예식"], reversed_rows, "stage", {})
    assert names == ["신부대기실", "예식"]
    assert [photo["id"] for photo in scenes[0]] == [f"p{n}" for n in range(50)]


def test_separated_repeat_of_a_stage_is_numbered_but_never_split_by_size():
    # 같은 단계가 떨어져 다시 나오면 번호, 이어진 긴 단계는 크기로 쪼개지 않는다(번호 조각 장면 없음).
    photos, labels = _untimed([(200, "예식"), (30, "축가·축하공연"), (30, "예식")])
    scenes, names = split_by_place(photos, labels, UNLABELED, "기타 장면", **STAGE_RULES)
    assert customer_ai.number_repeated(names) == ["예식 1", "축가·축하공연", "예식 2"]
    assert [len(scene) for scene in scenes] == [200, 30, 30]


def test_embedding_segments_keep_stage_edges_where_window_vote_shifts_them():
    # 신부 입장 앞부분 6장을 예식으로 잘못 읽었다. 사진 내용은 실제 경계에서 바뀐다 → 구간 다수결이 경계를 실제 자리에 둔다.
    photos, truth = _untimed([(60, "신랑 입장"), (40, "신부 입장"), (80, "예식")])
    labels = dict(truth)
    for n in range(60, 66):
        labels[f"p{n}"] = "예식"
    vectors = _vectors(photos, truth)
    known = [labels[photo["id"]] for photo in photos]
    unit = np.asarray([vectors[photo["id"]] for photo in photos])
    unit /= np.linalg.norm(unit, axis=1, keepdims=True)
    assert _segment_runs(known, unit) == [[0, 60, "신랑 입장"], [60, 100, "신부 입장"], [100, 180, "예식"]]
    scenes, names = split_by_place(photos, labels, UNLABELED, "기타 장면", **STAGE_RULES, vectors=vectors)
    assert names == ["신랑 입장", "신부 입장", "예식"] and [len(scene) for scene in scenes] == [60, 40, 80]


def test_short_segment_is_split_between_neighbors_by_raw_labels():
    # 내용이 따로 바뀌는 10장 조각(최소 15장 미만)을 다수결로 메이크업이라 읽었다. 조각 안 판정은 앞 4장 대기실, 가운데 5장 메이크업,
    # 끝 1장 연출 → 조각을 없애고 원래 판정이 가장 잘 맞는 자리(대기실 4장 뒤)에서 양쪽 이웃에 나눠 붙인다.
    content = ["신부대기실"] * 40 + ["조각"] * 10 + ["신랑신부 연출촬영"] * 40
    known = ["신부대기실"] * 44 + ["메이크업·준비"] * 5 + ["신랑신부 연출촬영"] * 41
    rng = np.random.default_rng(1)
    bases = {label: rng.normal(size=16) for label in set(content)}
    unit = np.asarray([bases[label] + rng.normal(scale=0.05, size=16) for label in content])
    unit /= np.linalg.norm(unit, axis=1, keepdims=True)
    assert _segment_runs(known, unit) == [[0, 44, "신부대기실"], [44, 90, "신랑신부 연출촬영"]]


@pytest.mark.parametrize("name, key", [("0001_OS100353.jpg", "os10"), ("0002_SJ7_3214.jpg", "sj7_"), ("IMG_1234.JPG", "img_"),
                                       ("0003_a.jpg", None)])
def test_camera_key_drops_export_index_and_camera_counter(name, key):
    assert camera_key(name) == key


def _cameras(*blocks):
    """blocks: 카메라마다 [(장수, 단계)] — 카메라별로 내보낸 파일을 이어 붙인 보정본(앞 연번 + 카메라 원래 이름)."""
    photos, labels, n = [], {}, 0
    for camera, stages in enumerate(blocks, start=1):
        counter = 353
        for count, stage in stages:
            for _ in range(count):
                photos.append({"id": f"p{n}", "order_index": n, "filename": f"{n + 1:04}_OS{camera}0{counter:04}.jpg",
                               "taken_at": None, "taken_at_source": "file"})
                labels[f"p{n}"] = stage
                n, counter = n + 1, counter + 1
    return photos, labels


def test_camera_blocks_need_contiguous_cameras_with_enough_photos():
    photos, _ = _cameras([(120, "예식")], [(110, "예식")])
    assert [len(block) for block in camera_blocks(photos)] == [120, 110]
    small, _ = _cameras([(120, "예식")], [(40, "예식")])
    assert camera_blocks(small) is None  # 작은 카메라는 따로 보지 않는다
    interleaved = [dict(photo, filename=f"{i + 1:04}_SJ{7 + i % 2}_{3000 + i}.jpg") for i, photo in enumerate(photos)]
    assert camera_blocks(interleaved) is None  # 시간순으로 섞인 두 카메라(짱쭌)는 파일명 순서가 곧 촬영 순서


def test_stray_main_stage_outside_the_ceremony_takes_its_neighbor_stage():
    # 식 전 빈 홀 버진로드 리허설을 행진으로 읽었다 — 본식 범위 밖이라 더 긴 이웃(대기실)으로 돌린다. 본식 안 구간은 그대로.
    pieces = [[["x"] * 80, "신부대기실", 0], [["x"] * 30, "행진·퇴장", 0], [["x"] * 50, "신랑신부 연출촬영", 0],
              [["x"] * 40, "신랑 입장", 0], [["x"] * 200, "예식", 0], [["x"] * 60, "행진·퇴장", 0], [["x"] * 70, "원판·단체 기념촬영", 0]]
    settled = _settle_outside_main(pieces, MAIN)
    assert [(len(photos), label) for photos, label, _ in settled] == [
        (110, "신부대기실"), (50, "신랑신부 연출촬영"), (40, "신랑 입장"), (200, "예식"), (60, "행진·퇴장"), (70, "원판·단체 기념촬영")]


def test_camera_timelines_merge_by_ceremony_phase():
    # 카메라 1: 대기실 → 입장 → 예식 → 행진 → 연출. 카메라 2: 메이크업 → 대기실 → 예식 → 축가 → 예식 → 행진 → 피로연.
    # 같은 단계는 한 장면으로, 식 전 대기실과 식 후 연출은 서로 짝짓지 않는다.
    photos, labels = _cameras([(150, "신부대기실"), (40, "신랑 입장"), (200, "예식"), (50, "행진·퇴장"), (60, "신랑신부 연출촬영")],
                              [(30, "메이크업·준비"), (100, "신부대기실"), (150, "예식"), (30, "축가·축하공연"), (60, "예식"),
                               (40, "행진·퇴장"), (120, "피로연·연회장")])
    scenes, names = split_by_place(photos, labels, UNLABELED, "기타 장면", **STAGE_RULES, vectors=_vectors(photos, labels),
                                   main_stages=MAIN)
    assert names == ["메이크업·준비", "신부대기실", "신랑 입장", "예식", "축가·축하공연", "예식", "행진·퇴장",
                     "신랑신부 연출촬영", "피로연·연회장"] or names == ["메이크업·준비", "신부대기실", "신랑 입장", "예식", "축가·축하공연",
                                                                  "예식", "행진·퇴장", "피로연·연회장", "신랑신부 연출촬영"]
    by_name = {name: len(scene) for scene, name in zip(scenes, names) if name != "예식"}
    assert by_name["신부대기실"] == 250 and by_name["행진·퇴장"] == 90
    assert [len(scene) for scene, name in zip(scenes, names) if name == "예식"] == [350, 60]
    assert sum(len(scene) for scene in scenes) == len(photos)


# --- 판정 버전·문구 ---------------------------------------------------------------------------------------------

def test_place_prompt_version_is_unchanged_for_home_snap():
    places = ["거실", "침실·침대"]
    digest = hashlib.sha1(json.dumps(places, ensure_ascii=False).encode()).hexdigest()[:8]
    assert customer_ai.quality_prompt_version(places) == f"{customer_ai.CUSTOMER_QUALITY_PROMPT_VERSION}-place-{digest}"


def test_stage_prompt_version_changes_with_list_or_hints():
    names = ["신부대기실", "예식"]
    base = customer_ai.quality_prompt_version(names, "stage", {"예식": "단상 앞"})
    assert "-stage-" in base
    assert customer_ai.quality_prompt_version(names, "stage", {"예식": "단상 앞 서약"}) != base
    assert customer_ai.quality_prompt_version([*names, "폐백"], "stage", {"예식": "단상 앞"}) != base
    # 목록에 없는 이름의 판단 기준은 버리므로 버전에 영향이 없다.
    assert customer_ai.quality_prompt_version(names, "stage", {"예식": "단상 앞", "없는 단계": "x"}) == base


def test_stage_version_includes_batch_judgment_prompt():
    # 묶음 판정으로 바꾼 버전 — 이전 한 장 판정(stage-v1) 캐시를 쓰지 않고 다시 판정한다.
    assert STAGE_PROMPT_VERSION != "stage-v1"
    payload = {"names": ["예식"], "hints": {}, "prompt": STAGE_PROMPT_VERSION}
    digest = hashlib.sha1(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:8]
    assert customer_ai.quality_prompt_version(["예식"], "stage", {}) == f"{customer_ai.CUSTOMER_QUALITY_PROMPT_VERSION}-stage-{digest}"


def test_stage_prompt_lists_stages_with_hints_and_asks_for_every_photo():
    prompt = stage_prompt(10, ["신부대기실", "예식"], {"예식": "단상 앞에서 서약"})
    assert "시간 순서대로 이어 찍은 사진" in prompt and "앞뒤 사진의 흐름" in prompt
    assert "- 예식: 단상 앞에서 서약" in prompt and "- 신부대기실\n" in prompt and "정확히 10개" in prompt
    schema = stage_schema(10, ["신부대기실", "예식"])
    stages = schema.properties["stages"]
    assert stages.items.enum == ["신부대기실", "예식", UNKNOWN_PLACE] and stages.min_items == stages.max_items == 10
    assert stage_schema(21, ["예식"]).properties["stages"].max_items is None  # 큰 개수 제한은 Gemini가 거절한다


def test_quality_schema_no_longer_asks_stage():
    schema, prompt = customer_schema()
    assert "place" not in schema.model_fields and "단계" not in prompt
    _, place_prompt = customer_schema(["거실"])
    assert "이 사진을 찍은 장소를 배경을 보고" in place_prompt and "단계" not in place_prompt


def test_stage_windows_overlap_and_skip_context():
    assert stage_windows(26) == [(0, 10, 0), (8, 18, 2), (16, 26, 2)]
    assert stage_windows(12, context=2) == [(0, 10, 2), (8, 12, 2)]  # 앞 배치 끝 2장은 맥락으로만
    assert stage_windows(2, context=2) == [] and stage_windows(5) == [(0, 5, 0)]


class _FakeModels:
    """judge_stages용 가짜 Gemini: 사진 바이트(b"사진번호")를 읽어 그 번호의 단계를 돌려준다. fail: 실패시킬 첫 사진 번호."""

    def __init__(self, truth, fail=(), short_once=()):
        self.truth, self.fail, self.short_once, self.calls = truth, set(fail), set(short_once), []

    async def generate_content(self, model, contents, config):
        numbers = [int(part.inline_data.data) for part in contents if hasattr(part, "inline_data") and part.inline_data]
        self.calls.append((numbers, config.media_resolution))
        if numbers[0] in self.fail:
            raise RuntimeError("boom")
        stages = [self.truth[n] for n in numbers]
        if numbers[0] in self.short_once:
            self.short_once.discard(numbers[0])
            stages = stages[:-1]
        from types import SimpleNamespace
        return SimpleNamespace(text=json.dumps({"stages": stages}, ensure_ascii=False), usage_metadata=None)


def _judge(monkeypatch, count, context=0, **fake):
    truth = {n: ("예식" if n % 2 else "신부 입장") for n in range(count)}
    models = _FakeModels(truth, **fake)
    from types import SimpleNamespace

    async def client():
        return SimpleNamespace(aio=SimpleNamespace(models=models))

    async def no_sleep(seconds):
        return None

    monkeypatch.setattr(gemini_quality_client, "get_client", client)
    monkeypatch.setattr(gemini_quality_client.asyncio, "sleep", no_sleep)
    labels, _ = asyncio.run(judge_stages([str(n).encode() for n in range(count)], ["신부 입장", "예식"], {}, context))
    return labels, truth, models


def test_judge_stages_labels_each_photo_once_with_neighbors_in_view(monkeypatch):
    labels, truth, models = _judge(monkeypatch, 26)
    assert labels == [truth[n] for n in range(26)]
    assert sorted(numbers for numbers, _ in models.calls) == [list(range(0, 10)), list(range(8, 18)), list(range(16, 26))]
    assert {resolution for _, resolution in models.calls} == {gemini_quality_client.STAGE_MEDIA_RESOLUTION}


def test_judge_stages_returns_only_photos_after_context(monkeypatch):
    labels, truth, _ = _judge(monkeypatch, 12, context=2)
    assert labels == [truth[n] for n in range(2, 12)]


def test_judge_stages_leaves_failed_window_unlabeled_and_retries_wrong_count(monkeypatch):
    labels, truth, models = _judge(monkeypatch, 26, fail={8}, short_once={16})
    assert labels[:10] == [truth[n] for n in range(10)]
    assert labels[10:18] == [None] * 8  # 실패한 묶음(8~17)이 맡은 사진(앞 2장은 앞 묶음 것) — 다듬기에서 앞 단계를 따른다
    assert labels[18:] == [truth[n] for n in range(18, 26)]  # 개수가 틀린 응답은 한 번 다시 물어 받는다
    assert [numbers[0] for numbers, _ in models.calls].count(16) == 2


# --- 장면 실행: 흔들림 확인을 기다려 단계로 저장 --------------------------------------------------------------------

def _stage_scene_run(monkeypatch, photo_count, quality_status="completed", placed=None, run_status="processing", embedded=None,
                     main_stages=("예식", "없는 단계"), quality_failed=0):
    events, done, seen_since = [], {}, {}
    embedded = photo_count if embedded is None else embedded
    rows = [{"id": f"p{i}", "order_index": i, "filename": f"{i + 1:04}_A.jpg", "preview_url": "u", "thumb_url": "t",
             "taken_at": None, "taken_at_source": None} for i in range(photo_count)]

    def ensure(db, run_id):
        if run_status != "processing":
            raise customer_ai._Superseded()

    async def wait(db, run_id, project_id, kind, since=None):  # 실제 대기도 매번 현재 실행인지부터 확인한다
        ensure(db, run_id)
        events.append(f"wait:{kind}")
        assert since is not None and since == seen_since.setdefault("at", since)  # 두 대기 모두 장면 실행 시작 시각 기준
        return quality_status if kind == "quality" else "completed"

    def forbidden(*args, **kwargs):
        raise AssertionError("단계 기준 장면은 Gemini 묘사·이름·임베딩을 부르지 않는다")

    db = MagicMock()
    monkeypatch.setattr(customer_ai, "get_supabase", lambda: db)
    monkeypatch.setattr(customer_ai, "_all_rows", lambda query, page_rows=None: rows)
    monkeypatch.setattr(customer_ai, "_ensure_running", ensure)
    monkeypatch.setattr(customer_ai, "_wait_for_run", wait)
    monkeypatch.setattr(customer_ai, "_latest_run", lambda db, project_id, kind: {"status": quality_status, "failed_count": quality_failed})
    monkeypatch.setattr(customer_ai, "_stored_embeddings", lambda db, project_id: {row["id"]: np.ones(4) for row in rows[:embedded]})
    monkeypatch.setattr(customer_ai, "_place_scenes", lambda *args, **kwargs: events.append(
        ("place", args[4], len(args[6]), sorted(args[7] or []))) or placed)
    monkeypatch.setattr(customer_ai, "_replace_scenes", lambda db, project_id, scenes, names: events.append(("replace", names)))
    for name in ("_describe_scene", "_name_scene", "_plan_content_sections", "_embeddings", "get_client"):
        monkeypatch.setattr(customer_ai, name, forbidden)
    monkeypatch.setattr(customer_ai, "_progress", lambda *args, **kwargs: events.append(
        ("smoothing", kwargs["settings"].get("smoothing"))) and (lambda step=1: None) if kwargs.get("settings", {}).get("smoothing")
        else (lambda step=1: None))
    monkeypatch.setattr(customer_ai, "_done", lambda db, run_id, total, processed, failed, error=None, usage=None: done.update(
        error=error, calls=(usage or {}).get("calls")))
    asyncio.run(customer_ai.run_scene("run", "project", ["신부대기실", "예식"], None,
                                      ["신부대기실", "예식"], "stage", {"예식": "단상 앞"}, list(main_stages)))
    return events, done


def test_stage_scene_waits_for_quality_and_similarity_then_saves_stage_scenes(monkeypatch):
    placed = ([["a"], ["b"]], ["신부대기실", "예식"])
    events, done = _stage_scene_run(monkeypatch, 120, placed=placed)
    # 임베딩 120장 모두 넘기고, 본식 단계는 단계 목록에 있는 것만 넘긴다.
    assert events == ["wait:quality", "wait:similarity", ("smoothing", "embedding-segments"), ("place", "stage", 120, ["예식"]),
                      ("replace", ["신부대기실", "예식"])]
    assert done == {"error": None, "calls": 0}


def test_stage_scene_without_all_embeddings_records_window_smoothing(monkeypatch):
    events, _ = _stage_scene_run(monkeypatch, 120, placed=([["a"]], ["예식"]), embedded=119)
    assert ("smoothing", "window") in events and ("place", "stage", 119, ["예식"]) in events


def test_stage_scene_fails_and_keeps_old_scenes_when_quality_fails(monkeypatch):
    events, done = _stage_scene_run(monkeypatch, 120, quality_status="failed")
    assert events == ["wait:quality"]  # 기존 장면을 지우지 않는다
    assert done["error"] and "흔들림 확인" in done["error"]


def test_stage_scene_without_enough_labels_clears_scenes(monkeypatch):
    events, done = _stage_scene_run(monkeypatch, 120, placed=None)
    assert events[-1] == ("replace", []) and done["error"] is None


def test_stage_scene_keeps_old_scenes_when_failed_judgments_leave_too_few_stages(monkeypatch):
    # 흔들림 확인이 완료로 끝났지만 2,268장이 실패해(크레딧 소진) 단계가 부족하다 — 기존 장면을 지우지 않고 실패로 닫는다.
    events, done = _stage_scene_run(monkeypatch, 120, placed=None, quality_failed=2268)
    assert not any(event[0] == "replace" for event in events if isinstance(event, tuple))
    assert done["error"] and "2268장" in done["error"]


def test_stage_scene_with_some_failed_judgments_still_saves_scenes(monkeypatch):
    # 일부만 실패해 단계가 충분하면 판정된 사진으로 장면을 만든다(화면은 "흔들림·눈 감음 확인 N장 못 함 · 다시 시도").
    events, done = _stage_scene_run(monkeypatch, 120, placed=([["a"]], ["예식"]), quality_failed=30)
    assert events[-1] == ("replace", ["예식"]) and done["error"] is None


def test_stage_scene_with_few_photos_finishes_without_waiting(monkeypatch):
    events, done = _stage_scene_run(monkeypatch, 40)
    assert events == [("replace", [])] and done["error"] is None


def test_superseded_stage_scene_stops_quietly(monkeypatch):
    events, done = _stage_scene_run(monkeypatch, 120, run_status="failed")
    assert events == [] and done == {}


def _quality_run(monkeypatch, photos, batch_photos=40, done_ids=(), fail_ids=()):
    """done_ids: 이미 지금 버전으로 판정된 사진, fail_ids: 이번 흔들림 판정이 실패하는 사진."""
    from types import SimpleNamespace
    calls = {"assess": [], "judge": [], "saved": {}}
    queries = iter([photos, [{"photo_id": photo_id} for photo_id in done_ids]])
    fail_urls = {photo["preview_url"] for photo in photos if photo["id"] in fail_ids}

    async def download(urls):
        return [url.encode() for url in urls]

    async def assess(images, on_each=None, customer=False, stats=None, place_names=None):
        calls["assess"].append(place_names)
        value = SimpleNamespace(eyes_closed=SimpleNamespace(value="ok"), blur_or_shake=SimpleNamespace(value="ok"),
                                focus_issue=SimpleNamespace(value="ok"), face_occluded=SimpleNamespace(value="ok"),
                                primary_subject_detected=True, notes=None, model_dump=lambda mode: {"people": "solo"})
        return [None if image.decode() in fail_urls else value for image in images], []

    async def judge(images, names, hints, context, stats):
        calls["judge"].append(([image.decode() for image in images], context, names, hints))
        return [f"단계:{image.decode()}" for image in images[context:]], [{"prompt_token_count": 1}]

    db = MagicMock()
    db.table.return_value.upsert.side_effect = lambda payload, on_conflict: calls["saved"].update(
        {row["photo_id"]: row["raw_response"] for row in payload}) or MagicMock()
    monkeypatch.setattr(customer_ai, "_all_rows", lambda query, page_rows=None: next(queries))
    monkeypatch.setattr(customer_ai, "_ensure_running", lambda db, run_id: None)
    monkeypatch.setattr(customer_ai, "download_all", download)
    monkeypatch.setattr(customer_ai, "assess_images", assess)
    monkeypatch.setattr(customer_ai, "judge_stages", judge)
    monkeypatch.setattr(customer_ai, "BATCH_PHOTOS", batch_photos)
    monkeypatch.setattr(customer_ai, "_progress", lambda *args, **kwargs: calls.update(settings=kwargs.get("settings")) or (lambda step=1: None))
    monkeypatch.setattr(customer_ai, "_done", lambda *args, **kwargs: calls.update(done=kwargs.get("usage")))
    monkeypatch.setattr(customer_ai, "_place_scenes", lambda *args, **kwargs: pytest.fail("흔들림 실행이 단계 장면을 저장하면 안 된다"))
    asyncio.run(customer_ai._run_quality(db, "run", "project", ["신부대기실", "예식"], "stage", {"예식": "단상 앞", "없는 단계": "x"}))
    delete = db.table.return_value.delete.return_value.eq.return_value
    calls["cleanup"] = [(call.args, neq.args) for call, neq in zip(delete.in_.call_args_list, delete.in_.return_value.neq.call_args_list)]
    return calls


def test_quality_cleanup_keeps_previous_judgment_of_photos_that_failed(monkeypatch):
    # p4는 이미 지금 버전으로 판정됨, p1은 이번 판정 실패 — p1의 이전 판정(흔들림 표시)은 지우지 않는다.
    photos = [{"id": f"p{n}", "order_index": n, "preview_url": f"u{n}", "filename": f"{n + 1:04}_A.jpg",
               "taken_at": None, "taken_at_source": "file"} for n in range(5)]
    calls = _quality_run(monkeypatch, photos, done_ids=["p4"], fail_ids={"p1"})
    assert set(calls["saved"]) == {"p0", "p2", "p3"}
    chunk = ("photo_id", ["p0", "p2", "p3", "p4"])
    assert [args for args, _ in calls["cleanup"]] == [chunk, chunk]
    assert [neq[0] for _, neq in calls["cleanup"]] == ["prompt_version", "model"]


def test_quality_with_stage_basis_judges_stages_in_capture_order_and_leaves_scenes_to_scene_run(monkeypatch):
    # 업로드는 역순 — 파일명 연번(촬영 순서)으로 놓고 묶음 판정한다. 흔들림 호출은 단계를 묻지 않는다.
    photos = [{"id": f"p{n}", "order_index": 4 - n, "preview_url": f"u{n}", "filename": f"{n + 1:04}_A.jpg",
               "taken_at": "2026-10-12T08:14:31", "taken_at_source": "file"} for n in range(5)]
    calls = _quality_run(monkeypatch, photos, batch_photos=3)
    assert calls["assess"] == [None, None]
    # 둘째 배치는 첫 배치 끝 2장을 맥락으로 함께 보낸다(촬영 순서로 바로 이어짐).
    assert [(images, context) for images, context, _, _ in calls["judge"]] == [(["u0", "u1", "u2"], 0), (["u1", "u2", "u3", "u4"], 2)]
    assert calls["judge"][0][2:] == (["신부대기실", "예식"], {"예식": "단상 앞"})
    assert calls["saved"]["p3"] == {"people": "solo", "place": "단계:u3"}
    assert calls["settings"]["stageJudge"]["batchPhotos"] == 10 and calls["settings"]["stageJudge"]["mediaResolution"] == "MEDIA_RESOLUTION_LOW"
    assert calls["done"]["calls"] == 2  # 단계 판정 usage도 실행 비용에 들어간다


# --- 흔들림 확인 기다리기 ----------------------------------------------------------------------------------------

def _wait(monkeypatch, runs, grace=None, kind="quality", since=None):
    """runs: _latest_run이 차례로 돌려줄 값(마지막 값은 계속 반복)."""
    sequence = list(runs)
    seen = {"polls": 0}

    def latest(db, project_id, kind):
        seen["polls"] += 1
        return sequence.pop(0) if len(sequence) > 1 else sequence[0]

    async def no_sleep(seconds):
        return None

    monkeypatch.setattr(customer_ai, "_latest_run", latest)
    monkeypatch.setattr(customer_ai, "_ensure_running", lambda db, run_id: None)
    monkeypatch.setattr(customer_ai.asyncio, "sleep", no_sleep)
    if grace is not None:
        monkeypatch.setattr(customer_ai, "QUALITY_START_GRACE_SECONDS", grace)
    return asyncio.run(customer_ai._wait_for_run(MagicMock(), "run", "project", kind, since)), seen["polls"]


def _at(seconds_ago=0):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


def test_wait_returns_when_the_same_tidy_quality_run_finishes(monkeypatch):
    running = {"status": "processing", "created_at": _at(), "updated_at": _at()}
    status, polls = _wait(monkeypatch, [running, running, {**running, "status": "completed"}])
    assert status == "completed" and polls == 3


def test_wait_treats_stale_quality_run_as_failed(monkeypatch):
    stale = {"status": "processing", "created_at": _at(3600), "updated_at": _at(3600)}
    assert _wait(monkeypatch, [stale])[0] == "failed"


def test_wait_for_similarity_after_quality_counts_from_the_tidy_start(monkeypatch):
    # 흔들림 확인에 10분 걸린 뒤 유사컷을 기다릴 때: 같은 정리에서 10분 전에 끝난 유사컷을 바로 쓴다(유예 시간을 기다리지 않음).
    tidy_start = datetime.now(timezone.utc) - timedelta(minutes=10)
    finished = {"status": "completed", "created_at": _at(600), "updated_at": _at(580)}
    status, polls = _wait(monkeypatch, [finished], kind="similarity", since=tidy_start)
    assert status == "completed" and polls == 1
    # 같은 정리의 실행이 없으면(옛 실행만) 유예 시간이 이미 지났으므로 기다리지 않고 최근 결과를 쓴다.
    older = {"status": "completed", "created_at": _at(3600), "updated_at": _at(3600)}
    assert _wait(monkeypatch, [older], kind="similarity", since=tidy_start) == ("completed", 1)


def test_wait_uses_older_quality_result_only_after_grace(monkeypatch):
    old = {"status": "completed", "created_at": _at(3600), "updated_at": _at(3600)}
    assert _wait(monkeypatch, [old], grace=0)[0] == "completed"
    assert _wait(monkeypatch, [None], grace=0)[0] is None
