from app.customer_ai import capture_order


def test_capture_order_uses_taken_at_then_upload_order():
    rows = [
        {"id": "late", "order_index": 0, "taken_at": "2026-10-03T12:00:00"},
        {"id": "untimed", "order_index": 1, "taken_at": None},
        {"id": "early", "order_index": 2, "taken_at": "2026-10-03T11:00:00"},
        {"id": "same_time_first_upload", "order_index": 3, "taken_at": "2026-10-03T11:30:00"},
        {"id": "same_time_second_upload", "order_index": 4, "taken_at": "2026-10-03T11:30:00"},
    ]
    assert [row["id"] for row in capture_order(rows)] == [
        "early", "same_time_first_upload", "same_time_second_upload", "late", "untimed",
    ]


def test_capture_order_ignores_file_times():
    # 보정본을 한꺼번에 내보내면 파일 시각이 섞인다 — 파일 시각은 무시하고 업로드(파일명) 순서로.
    rows = [{"id": "b", "order_index": 1, "taken_at": "2026-06-08T23:48:58", "taken_at_source": "file"},
            {"id": "a", "order_index": 0, "taken_at": "2026-06-08T23:48:59", "taken_at_source": "file"}]
    assert [row["id"] for row in capture_order(rows)] == ["a", "b"]
