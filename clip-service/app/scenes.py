"""셀프 고객 사진의 "장면" 나누기 — FE `src/lib/customer-scenes.ts`의 splitScenes와 같은 규칙.

촬영 시각 사이에 큰 공백이 생기는 지점에서 나눈다. 사진 내용(임베딩)으로 경계를 보정하지 않는다:
같은 홀에서 이어지는 장면은 사진이 비슷해 "비슷하면 합치기"가 장면을 잘못 합치고, 실제 촬영 데이터 없이
기준값을 정할 수 없어서다.
# ponytail: 시각 공백 휴리스틱. 실제 촬영 데이터가 모이면 임베딩 변화로 긴 장면을 나누는 보정을 더한다.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

# 3분: 행사 스냅(돌잔치 등)은 쉬지 않고 찍다가 순서가 바뀔 때만 3~10분 쉰다. 본식처럼 공백이 많으면
# MAX_SCENES 안에서 큰 공백부터 자르므로 기준이 낮아도 장면이 과하게 쪼개지지 않는다.
SCENE_GAP_SECONDS = 3 * 60
MIN_SCENE_PHOTOS = 10
MAX_SCENES = 8
MIN_PHOTOS_FOR_SCENES = 20
MIN_TIMED_RATIO = 0.8


def _time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", ""))
    except ValueError:
        return None


def split_scenes(photos: list[dict]) -> Optional[list[list[dict]]]:
    """photos: {"id", "order_index", "taken_at"}. 반환: 장면별 사진 목록(시간순, 촬영 시각 없는 사진은 맨 끝 장면).
    장면으로 나눌 근거가 부족하면(사진이 적거나 촬영 시각 대부분이 없으면) None."""
    timed = [photo for photo in photos if _time(photo.get("taken_at"))]
    if len(photos) < MIN_PHOTOS_FOR_SCENES or len(timed) < len(photos) * MIN_TIMED_RATIO:
        return None

    ordered = sorted(timed, key=lambda photo: (_time(photo["taken_at"]), photo["order_index"]))
    gaps = [(index, (_time(photo["taken_at"]) - _time(ordered[index - 1]["taken_at"])).total_seconds())
            for index, photo in enumerate(ordered) if index > 0]
    cuts = sorted(index for index, _ in sorted(
        [item for item in gaps if item[1] >= SCENE_GAP_SECONDS], key=lambda item: -item[1])[:MAX_SCENES - 1])

    ranges: list[list[dict]] = []
    start = 0
    for cut in [*cuts, len(ordered)]:
        part = ordered[start:cut]
        start = cut
        # 너무 작은 장면은 바로 앞 장면에 붙인다(첫 장면이면 아래에서 다음 장면과 합친다).
        if len(part) < MIN_SCENE_PHOTOS and ranges:
            ranges[-1].extend(part)
        else:
            ranges.append(part)
    if len(ranges) > 1 and len(ranges[0]) < MIN_SCENE_PHOTOS:
        ranges[0:2] = [ranges[0] + ranges[1]]

    untimed = sorted((photo for photo in photos if not _time(photo.get("taken_at"))), key=lambda photo: photo["order_index"])
    if untimed:
        ranges.append(untimed)
    return ranges
