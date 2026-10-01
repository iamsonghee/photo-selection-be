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


def test_splits_on_ten_minute_gaps_like_the_frontend():
    scenes = split_scenes(_photos([(11, 0, 30), (11, 40, 30), (12, 30, 30)]))
    assert [len(scene) for scene in scenes] == [30, 30, 30]
    assert scenes[1][0]["id"] == "p30"


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
