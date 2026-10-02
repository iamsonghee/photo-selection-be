import json
from pathlib import Path

import pytest

from app.customer_ai import merge_same_named
from app.scenes import split_scenes


def _photos(blocks):
    """blocks: [(시, 분, 장수)] — 블록 안에서는 20초 간격."""
    photos, n = [], 0
    for hour, minute, count in blocks:
        for i in range(count):
            seconds = hour * 3600 + minute * 60 + i * 20
            photos.append({"id": f"p{n}", "order_index": n,
                           "taken_at": f"2026-10-03T{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}"})
            n += 1
    return photos


# 공용 골든 케이스 — FE tests/customer-scenes.test.mjs 도 같은 파일(복사본)을 읽는다(경계 규칙 드리프트 방지).
FIXTURE = Path(__file__).parent / "fixtures" / "scene-cases.json"
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_golden_scene_cases(case):
    photos = []
    for block in case["blocks"]:
        start, count, source = block[0], block[1], (block[2] if len(block) > 2 else "exif")
        h, m, sec = map(int, start.split(":"))
        for i in range(count):
            t = h * 3600 + m * 60 + sec + i * 20
            photos.append({"id": f"c{len(photos)}", "order_index": len(photos), "taken_at_source": source,
                           "taken_at": f"2026-10-03T{t // 3600:02}:{t // 60 % 60:02}:{t % 60:02}"})
    photos += [{"id": f"c{len(photos) + i}", "order_index": len(photos) + i, "taken_at": None} for i in range(case.get("untimed", 0))]
    scenes = split_scenes(photos)
    assert (None if scenes is None else [len(scene) for scene in scenes]) == case["expected"]


def test_untimed_photos_go_last_in_upload_order():
    photos = _photos([(11, 0, 30)]) + [{"id": f"x{i}", "order_index": 99 - i, "taken_at": None} for i in range(2)]
    assert [photo["id"] for photo in split_scenes(photos)[-1]] == ["x1", "x0"]


def test_same_named_scenes_merge_only_when_close_in_time():
    # 하객(11:00) ─5분─ 하객 → 합침 / ─30분─ 하객 → 다른 시점이라 따로. 이름 없음·기타 장면은 합치지 않는다.
    a, b, c, d = (_photos([(11, minute, 10)]) for minute in (0, 8, 45, 50))
    for scene, prefix in zip((a, b, c, d), "abcd"):
        for photo in scene:
            photo["id"] = prefix + photo["id"]
    scenes, names = merge_same_named([a, b, c, d], ["하객", "하객", "하객", "기타 장면"])
    assert names == ["하객", "하객", "기타 장면"]
    assert [len(scene) for scene in scenes] == [20, 10, 10]
    scenes, names = merge_same_named([c, d], ["기타 장면", "기타 장면"])
    assert names == ["기타 장면", "기타 장면"]
    scenes, names = merge_same_named([a, b], [None, None])
    assert names == [None, None]


def test_same_named_small_scene_merges_even_after_long_gap():
    # 실데이터(돌잔치): 하객 4장 ─13분─ 하객 11장 → 작은 쪽이 있으면 공백이 길어도 합친다.
    a, b = _photos([(17, 15, 4)]), _photos([(17, 29, 11)])
    for photo in b:
        photo["id"] = "b" + photo["id"]
    scenes, names = merge_same_named([a, b], ["하객", "하객"])
    assert names == ["하객"] and [len(scene) for scene in scenes] == [15]


def _fake_scene_run(monkeypatch, rows, status="processing", name="하객", flagged=()):
    """run_scene을 DB·Gemini 없이 돌린다. 반환: 일어난 일 순서(events)와 _done 호출 인자."""
    import asyncio
    from unittest.mock import MagicMock
    from app import customer_ai

    events, done = [], {}

    async def fake_client():
        return None

    async def fake_download(urls):
        return [b"x" for _ in urls]

    async def fake_name(client, images, names, usages=None):
        events.append("name")
        usages.append({"prompt_token_count": 100, "candidates_token_count": 10, "thoughts_token_count": 50, "total_token_count": 160})
        return name

    db = MagicMock()
    db.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [{"status": status}]
    db.table.return_value.delete.side_effect = lambda: events.append("delete") or MagicMock()
    monkeypatch.setattr(customer_ai, "get_supabase", lambda: db)
    monkeypatch.setattr(customer_ai, "_all_rows", lambda query: rows)
    monkeypatch.setattr(customer_ai, "_flagged_photos", lambda db, project_id: set(flagged))
    monkeypatch.setattr(customer_ai, "get_client", fake_client)
    monkeypatch.setattr(customer_ai, "download_all", fake_download)
    monkeypatch.setattr(customer_ai, "_name_scene", fake_name)
    monkeypatch.setattr(customer_ai, "_progress", lambda *args, **kwargs: (lambda step=1: events.append("tick")))
    monkeypatch.setattr(customer_ai, "_done", lambda db, run_id, total, processed, failed, error=None, usage=None: done.update(
        total=total, processed=processed, failed=failed, error=error, usage=usage))
    asyncio.run(customer_ai.run_scene("run", "p", ["하객", "돌잡이"]))
    return events, done


def test_scenes_are_replaced_only_after_naming_and_only_by_the_current_run(monkeypatch):
    # 기존 장면은 이름 붙이기(오래 걸림)가 끝난 뒤에 지운다. 멈춘 것으로 닫힌 실행은 장면을 건드리지 않는다.
    rows = [dict(photo, preview_url="u") for photo in _photos([(11, 0, 30), (11, 40, 30)])]
    events, done = _fake_scene_run(monkeypatch, rows)
    assert events.index("delete") > max(i for i, event in enumerate(events) if event == "name")
    assert events.count("tick") == 2  # 진행 수 = 이름 붙일 장면 수
    assert done == {"total": 2, "processed": 2, "failed": 0, "error": None, "usage": {
        "calls": 2, "prompt_tokens": 200, "output_tokens": 20, "thinking_tokens": 100, "total_tokens": 320}}
    events, done = _fake_scene_run(monkeypatch, rows, status="failed")
    assert "delete" not in events and not done


def test_failed_naming_is_counted_and_saved_as_other(monkeypatch):
    rows = [dict(photo, preview_url="u") for photo in _photos([(11, 0, 30), (11, 40, 30)])]
    _, done = _fake_scene_run(monkeypatch, rows, name=None)
    assert {key: done[key] for key in ("total", "processed", "failed", "error")} == {"total": 2, "processed": 0, "failed": 2, "error": None}


def test_samples_skip_flagged_photos_and_similar_duplicates():
    from app.customer_ai import pick_samples
    scene = [{"id": f"p{i}", "similarity_group_id": "g1" if i < 4 else None} for i in range(10)]
    ids = [photo["id"] for photo in pick_samples(scene, flagged={"p5"})]
    assert len(ids) == 3 and "p5" not in ids
    assert sum(photo_id in {"p0", "p1", "p2", "p3"} for photo_id in ids) <= 1  # 유사컷 묶음은 한 장만
    # 전부 흔들림이면 그대로 쓴다(이름을 못 붙이는 것보다 낫다).
    assert len(pick_samples(scene[:3], flagged={"p0", "p1", "p2"})) >= 1


def test_repeated_names_get_numbers_in_order():
    from app.customer_ai import number_repeated
    assert number_repeated(["야외", "실내·카페", "야외", "기타 장면", "기타 장면", None]) == \
        ["야외 1", "실내·카페", "야외 2", "기타 장면", "기타 장면", None]
