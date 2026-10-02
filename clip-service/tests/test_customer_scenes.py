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


def test_splits_on_long_gaps_like_the_frontend():
    scenes = split_scenes(_photos([(11, 0, 30), (11, 40, 30), (12, 30, 30)]))
    assert [len(scene) for scene in scenes] == [30, 30, 30]
    assert scenes[1][0]["id"] == "p30"


def test_event_snap_splits_on_short_breaks():
    # 돌잔치 스냅 실데이터 패턴: 쉬지 않고 찍다가 순서가 바뀔 때만 4~5분 쉰다(10분 공백 없음).
    scenes = split_scenes(_photos([(10, 0, 30), (10, 15, 30), (10, 30, 30)]))
    assert [len(scene) for scene in scenes] == [30, 30, 30]


def test_small_scenes_merge_into_neighbours_and_untimed_go_last():
    photos = _photos([(11, 0, 5), (11, 30, 25), (12, 30, 4)]) + [{"id": "x", "order_index": 99, "taken_at": None}]
    scenes = split_scenes(photos)
    # 5장(첫 장면) → 다음 장면과 합침, 4장(마지막) → 앞 장면에 붙음, 촬영 시각 없는 1장은 맨 끝 장면.
    assert [len(scene) for scene in scenes] == [34, 1]
    assert scenes[-1][0]["id"] == "x"


def test_too_few_or_mostly_untimed_photos_have_no_scenes():
    assert split_scenes(_photos([(11, 0, 19)])) is None
    untimed = [{"id": f"u{i}", "order_index": i, "taken_at": None} for i in range(30)]
    assert split_scenes(_photos([(11, 0, 20)]) + untimed) is None


def test_adjacent_same_named_scenes_merge():
    a, b, c, d, e = ([{"id": n}] for n in "abcde")
    scenes, names = merge_same_named([a, b, c, d, e], ["하객", "하객", "돌잡이", "하객", None])
    assert names == ["하객", "돌잡이", "하객", None]
    assert [[photo["id"] for photo in scene] for scene in scenes] == [["a", "b"], ["c"], ["d"], ["e"]]


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


def test_file_modified_times_are_not_used_for_scene_boundaries():
    # EXIF가 없어 파일 수정 시각으로 대신한 사진(HEIC·카카오톡)은 경계 계산에서 빠지고 "촬영 시각 없음" 장면으로 간다.
    photos = _photos([(11, 0, 30), (11, 40, 30)])
    fake = [{"id": f"f{i}", "order_index": 100 + i, "taken_at": "2026-10-03T11:20:00", "taken_at_source": "file"} for i in range(5)]
    scenes = split_scenes(photos + fake)
    assert [len(scene) for scene in scenes] == [30, 30, 5]
    assert scenes[-1][0]["id"] == "f0"


def test_file_modified_times_do_not_count_as_timed():
    exif = _photos([(11, 0, 20)])
    fake = [{"id": f"f{i}", "order_index": 100 + i, "taken_at": "2026-10-03T12:00:00", "taken_at_source": "file"} for i in range(10)]
    assert split_scenes(exif + fake) is None  # 20/30 = 67% < 80%


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
