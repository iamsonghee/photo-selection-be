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


def test_scene_progress_fills_second_half(monkeypatch):
    # 다시 정리(임베딩 재사용)할 때도 장면 이름을 붙이는 동안 진행률이 올라 끝에서 정확히 전체가 된다.
    import asyncio
    from unittest.mock import MagicMock
    from app import customer_ai

    async def fake_client():
        return None

    async def fake_download(urls):
        return [b"x" for _ in urls]

    async def fake_name(client, images, names):
        return names[0]

    monkeypatch.setattr(customer_ai, "get_client", fake_client)
    monkeypatch.setattr(customer_ai, "download_all", fake_download)
    monkeypatch.setattr(customer_ai, "_name_scene", fake_name)
    rows = [dict(photo, preview_url="u") for photo in _photos([(11, 0, 30), (11, 40, 30)])]
    steps = []
    asyncio.run(customer_ai._save_scenes(MagicMock(), "run", "p", rows, ["하객"], lambda step=1: steps.append(step)))
    assert sum(steps) == len(rows)


def test_scenes_are_replaced_only_after_naming_and_only_by_the_current_run(monkeypatch):
    # 기존 장면은 이름 붙이기(오래 걸림)가 끝난 뒤에 지운다. 멈춘 것으로 닫힌 실행은 장면을 건드리지 않는다.
    import asyncio
    from unittest.mock import MagicMock
    from app import customer_ai

    events = []

    async def fake_client():
        return None

    async def fake_download(urls):
        return [b"x" for _ in urls]

    async def fake_name(client, images, names):
        events.append("name")
        return names[0]

    monkeypatch.setattr(customer_ai, "get_client", fake_client)
    monkeypatch.setattr(customer_ai, "download_all", fake_download)
    monkeypatch.setattr(customer_ai, "_name_scene", fake_name)
    rows = [dict(photo, preview_url="u") for photo in _photos([(11, 0, 30), (11, 40, 30)])]

    for status, expect_delete in (("processing", True), ("failed", False)):
        events.clear()
        db = MagicMock()
        db.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [{"status": status}]
        db.table.return_value.delete.side_effect = lambda: events.append("delete") or MagicMock()
        asyncio.run(customer_ai._save_scenes(db, "run", "p", rows, ["하객"]))
        assert ("delete" in events) == expect_delete
        if expect_delete:
            assert events.index("delete") > max(i for i, e in enumerate(events) if e == "name")
