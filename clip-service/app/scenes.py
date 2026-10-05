"""셀프 고객 사진의 "장면" 나누기 — FE `src/lib/customer-scenes.ts`의 splitScenes와 같은 규칙.

촬영 시각 사이에 큰 공백이 생기는 지점에서 나눈다. 사진 내용(임베딩)으로 경계를 보정하지 않는다:
같은 홀에서 이어지는 장면은 사진이 비슷해 "비슷하면 합치기"가 장면을 잘못 합치고, 실제 촬영 데이터 없이
기준값을 정할 수 없어서다.
# ponytail: 시각 공백 휴리스틱. 실제 촬영 데이터가 모이면 임베딩 변화로 긴 장면을 나누는 보정을 더한다.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Optional

# 3분: 행사 스냅(돌잔치 등)은 쉬지 않고 찍다가 순서가 바뀔 때만 3~10분 쉰다. 본식처럼 공백이 많으면
# 장면 상한(max_scenes) 안에서 큰 공백부터 자르므로 기준이 낮아도 장면이 과하게 쪼개지지 않는다.
SCENE_GAP_SECONDS = 3 * 60
MIN_SCENE_PHOTOS = 10
# 장면 상한: 사진 PHOTOS_PER_SCENE장당 1개(최소 MIN_MAX_SCENES, 최대 MAX_MAX_SCENES). 고정 8개였을 때 1,572장 홈스냅(장소 12곳)이
# 상한에 막혔다(2026-10-05). 상한은 화면 길이·장면 이름 호출 수 안전장치이고, 실제 경계는 공백 기준이 정한다.
PHOTOS_PER_SCENE = 40
MIN_MAX_SCENES = 8
MAX_MAX_SCENES = 30
# 골라낸 사진만 올리면(수십 장) 사진 간격이 몇 분씩이라 시간 공백으로 장소 경계를 못 찾는다. FE 같은 이름 상수와 같은 값.
# SCENE_MIN_PHOTOS(env)는 적은 샘플로 장면 분석을 시험할 때만 낮춘다(로컬 clip-service) — 운영에는 두지 않는다.
MIN_PHOTOS_FOR_SCENES = int(os.getenv("SCENE_MIN_PHOTOS", "100"))
MIN_TIMED_RATIO = 0.8
# 이보다 짧은 공백은 "이어진 촬영": 작은 장면은 공백이 더 짧은 이웃에 붙이되, 양쪽(첫·마지막 장면은 한쪽) 공백이
# 모두 이 이상이면 작아도 따로 둔다(입장·케이크 커팅처럼 짧은 장면). 같은 이름 장면 병합도 이 공백 미만일 때만(한쪽이 작은 장면이면 공백 무관).
CLOSE_GAP_SECONDS = 10 * 60
# 따로 떨어져 있어도 이보다 적으면(한두 장 튄 사진) 장면으로 두지 않고 가까운 쪽에 붙인다.
MIN_ISOLATED_PHOTOS = 3
# 장면 나누기 규칙 버전 — 규칙을 바꾸면 올린다. 실행 settings에 기준값과 함께 남아 검수 채점에서 설정끼리 비교한다.
SCENE_ALGORITHM_VERSION = "gap-v3"  # v2: 파일 수정 시각 제외, 작은 장면은 가까운 이웃에, 같은 이름은 짧은 공백일 때만. v3: 장면 상한을 사진 수에 비례
SCENE_SETTINGS = {
    "algorithm": SCENE_ALGORITHM_VERSION, "gapSeconds": SCENE_GAP_SECONDS, "minScenePhotos": MIN_SCENE_PHOTOS,
    "maxScenes": {"photosPerScene": PHOTOS_PER_SCENE, "min": MIN_MAX_SCENES, "max": MAX_MAX_SCENES}, "minPhotos": MIN_PHOTOS_FOR_SCENES, "minTimedRatio": MIN_TIMED_RATIO,
    "closeGapSeconds": CLOSE_GAP_SECONDS, "minIsolatedPhotos": MIN_ISOLATED_PHOTOS, "sameNameMerge": "close-gap-or-small",
}


def _time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", ""))
    except ValueError:
        return None


def scene_taken_at(photo: dict) -> Optional[str]:
    """장면 경계에 쓸 수 있는 촬영 시각. 파일 수정 시각("file")은 실제 촬영 시각이 아니라 뺀다(출처 기록 전 사진은 그대로 쓴다)."""
    return None if photo.get("taken_at_source") == "file" else photo.get("taken_at")


def scene_gap(before: list[dict], after: list[dict]) -> float:
    """시간순으로 붙은 두 장면 사이 공백(초)."""
    return (_time(scene_taken_at(after[0])) - _time(scene_taken_at(before[-1]))).total_seconds()


def max_scenes(photo_count: int) -> int:
    return min(MAX_MAX_SCENES, max(MIN_MAX_SCENES, photo_count // PHOTOS_PER_SCENE))


def _merge_small(ranges: list[list[dict]]) -> list[list[dict]]:
    """작은 장면을 공백이 더 짧은 이웃에 붙인다(같으면 앞). 양쪽 공백이 모두 CLOSE_GAP 이상이면 그대로 둔다."""
    while len(ranges) > 1:
        for i, part in enumerate(ranges):
            if len(part) >= MIN_SCENE_PHOTOS:
                continue
            before = scene_gap(ranges[i - 1], part) if i > 0 else None
            after = scene_gap(part, ranges[i + 1]) if i < len(ranges) - 1 else None
            if len(part) >= MIN_ISOLATED_PHOTOS and all(gap is None or gap >= CLOSE_GAP_SECONDS for gap in (before, after)):
                continue
            j = i - 1 if after is None or (before is not None and before <= after) else i + 1
            lo, hi = min(i, j), max(i, j)
            ranges[lo:hi + 1] = [ranges[lo] + ranges[hi]]
            break
        else:
            break
    return ranges


def split_scenes(photos: list[dict], gap_seconds: float = SCENE_GAP_SECONDS) -> Optional[list[list[dict]]]:
    """photos: {"id", "order_index", "taken_at", "taken_at_source"}. 반환: 장면별 사진 목록(시간순, 촬영 시각 없는 사진은 맨 끝 장면).
    gap_seconds: 이 이상 공백에서 나눈다 — 촬영 종류별 값(FE 카탈로그, 예: 홈스냅은 방을 옮겨도 1~2분만 쉰다).
    장면으로 나눌 근거가 부족하면(사진이 적거나 촬영 시각 대부분이 없으면) None."""
    timed = [photo for photo in photos if _time(scene_taken_at(photo))]
    if len(photos) < MIN_PHOTOS_FOR_SCENES or len(timed) < len(photos) * MIN_TIMED_RATIO:
        return None

    ordered = sorted(timed, key=lambda photo: (_time(scene_taken_at(photo)), photo["order_index"]))
    gaps = [(index, (_time(scene_taken_at(photo)) - _time(scene_taken_at(ordered[index - 1]))).total_seconds())
            for index, photo in enumerate(ordered) if index > 0]
    cuts = sorted(index for index, _ in sorted(
        [item for item in gaps if item[1] >= gap_seconds], key=lambda item: -item[1])[:max_scenes(len(photos)) - 1])

    bounds = [0, *cuts, len(ordered)]
    ranges = _merge_small([ordered[a:b] for a, b in zip(bounds, bounds[1:])])

    untimed = sorted((photo for photo in photos if not _time(scene_taken_at(photo))), key=lambda photo: photo["order_index"])
    if untimed:
        ranges.append(untimed)
    return ranges
