"""셀프 고객 사진의 "장면" 나누기 — FE `src/lib/customer-scenes.ts`의 splitScenes와 같은 규칙.

촬영 시각 사이에 큰 공백이 생기는 지점에서 나눈다. 사진 내용(임베딩)으로 경계를 보정하지 않는다:
같은 홀에서 이어지는 장면은 사진이 비슷해 "비슷하면 합치기"가 장면을 잘못 합치고, 실제 촬영 데이터 없이
기준값을 정할 수 없어서다.
# ponytail: 시각 공백 휴리스틱. 실제 촬영 데이터가 모이면 임베딩 변화로 긴 장면을 나누는 보정을 더한다.
"""
from __future__ import annotations

import os
from collections import Counter
from datetime import datetime
from typing import Optional

import numpy as np

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


# 내용 기준 장면: 촬영 시각이 없으면(포토샵 내보내기 보정본 등 — EXIF에 시각이 빠짐) 업로드 순서(= 파일명 순서)로 늘어놓고
# 앞뒤 CONTENT_WINDOW장 평균 임베딩의 코사인 유사도가 크게 떨어지는 곳마다 경계 후보를 찾는다. 이후 전체 대표 사진으로
# 큰 촬영 흐름을 잡고 size_content_sections에서 노출 카드 수에 따라 분리한다. 사진 수만으로 장면 수를 정하면 촬영마다 틀렸다:
# 세트 27곳을 15~40장씩 찍은 636장은 세트 절반을 놓쳤다. 잘게 자르면 636장에서 세트 경계 26개 중 24개를 잡는다.
# ponytail: 웨딩 스튜디오 두 건으로 정한 값. 정답 경계 데이터가 더 모이면 기준값을 다시 맞춘다.
CONTENT_WINDOW = 5
CONTENT_CUT_SIMILARITY = 0.92
CONTENT_MIN_SCENE_PHOTOS = 10
CONTENT_MAX_SCENES = 60  # 장면마다 AI 호출 1번 — 비용 상한
# 이웃 합치기: 장소·의상이 같아도 사진 평균이 이보다 다르면 따로(예: 같은 흰 벽에서 하트 풍선 소품), 이름이 달라도 이 이상 같으면 합친다.
DESCRIBED_MERGE_MIN_SIMILARITY = 0.85
CONTENT_MERGE_SIMILARITY = 0.95
# 20장짜리 오판 조각도 흡수하되, 실제 짧은 세트는 남긴다. 636장 비교 촬영의 16장 야외 세트는 이웃과 0.920이었다.
CONTENT_ABSORB_MAX_PHOTOS = 20
CONTENT_ABSORB_MIN_SIMILARITY = 0.925
# 유사컷을 접은 카드 수. 검증용 시작값이며 의미 있는 경계가 없으면 상한을 넘겨도 유지한다.
CONTENT_VISIBLE_TARGET = 60
CONTENT_VISIBLE_MAX = 120
CONTENT_VISIBLE_MIN = 20


def split_by_content(photos: list[dict], vectors: list) -> Optional[list[list[dict]]]:
    """photos: split_scenes와 같은 형식, vectors: 같은 순서의 임베딩(없으면 None). 사진이 적거나 임베딩이 없는 사진이 있으면 None.
    반환: 업로드 순서의 작은 구간 목록(큰 촬영 흐름을 정하고 크기를 나눌 때 쓸 경계 후보)."""
    if len(photos) < MIN_PHOTOS_FOR_SCENES or any(vector is None for vector in vectors):
        return None
    order = sorted(range(len(photos)), key=lambda i: photos[i]["order_index"])
    unit = np.asarray([vectors[i] for i in order], dtype=np.float64)
    unit /= np.linalg.norm(unit, axis=1, keepdims=True)
    window = CONTENT_WINDOW
    scores = []
    for i in range(window, len(order) - window + 1):
        before, after = unit[i - window:i].mean(0), unit[i:i + window].mean(0)
        scores.append((float(before @ after / np.linalg.norm(before) / np.linalg.norm(after)), i))
    cuts: list[int] = []
    for score, i in sorted(scores):
        if score >= CONTENT_CUT_SIMILARITY or len(cuts) >= CONTENT_MAX_SCENES - 1:
            break
        if all(abs(i - cut) >= CONTENT_MIN_SCENE_PHOTOS for cut in (0, *cuts, len(order))):
            cuts.append(i)
    bounds = [0, *sorted(cuts), len(order)]
    return [[photos[order[k]] for k in range(a, b)] for a, b in zip(bounds, bounds[1:])]


def _centroid(scene: list[dict], vectors: dict) -> np.ndarray:
    mean = np.asarray([vectors[photo["id"]] for photo in scene], dtype=np.float64).mean(0)
    return mean / np.linalg.norm(mean)


def merge_described(scenes: list[list[dict]], descriptions: list[Optional[dict]],
                    vectors: dict) -> tuple[list[list[dict]], list[Optional[dict]]]:
    """잘게 자른 내용 기준 장면에서 이웃을 합친다. descriptions[i]: {"place": str, "outfits": [str]}(AI가 못 봤으면 None),
    vectors: 사진 ID → 임베딩. 장소가 같고 의상이 한쪽에 포함되면(단독 컷 ⊂ 커플 컷) 사진 평균이 DESCRIBED_MERGE_MIN_SIMILARITY 이상일 때,
    또는 이름과 상관없이 CONTENT_MERGE_SIMILARITY 이상이면 합친다. 합친 장면의 의상은 순서를 지켜 모은다."""
    merged: list[list[dict]] = []
    merged_descriptions: list[Optional[dict]] = []
    for scene, description in zip(scenes, descriptions):
        if merged:
            previous = merged_descriptions[-1]
            similarity = float(_centroid(merged[-1], vectors) @ _centroid(scene, vectors))
            alike = (previous and description and previous["place"] == description["place"]
                     and (set(previous["outfits"]) <= set(description["outfits"]) or set(description["outfits"]) <= set(previous["outfits"])))
            if (alike and similarity >= DESCRIBED_MERGE_MIN_SIMILARITY) or similarity >= CONTENT_MERGE_SIMILARITY:
                merged[-1] = merged[-1] + scene
                if previous and description:
                    previous["outfits"] = [*previous["outfits"], *(o for o in description["outfits"] if o not in previous["outfits"])]
                merged_descriptions[-1] = previous or description
                continue
        merged.append(scene)
        merged_descriptions.append(dict(description) if description else None)
    while len(merged) > 1:
        candidates = []
        for i, scene in enumerate(merged):
            if len(scene) > CONTENT_ABSORB_MAX_PHOTOS:
                continue
            for j in (i - 1, i + 1):
                if 0 <= j < len(merged):
                    candidates.append((float(_centroid(scene, vectors) @ _centroid(merged[j], vectors)), i, j))
        eligible = [item for item in candidates if item[0] >= CONTENT_ABSORB_MIN_SIMILARITY
                    or not (merged_descriptions[item[1]] or {}).get("outfits")]
        if not eligible:
            break
        _, i, j = max(eligible)
        lo = min(i, j)
        description = merged_descriptions[j] if len(merged[j]) >= len(merged[i]) else merged_descriptions[i]
        merged[lo:lo + 2] = [merged[lo] + merged[lo + 1]]
        merged_descriptions[lo:lo + 2] = [description]
    return merged, merged_descriptions


def described_name(description: Optional[dict]) -> Optional[str]:
    """"주황 나무 계단 · 흰 드레스, 검정 턱시도". 인물이 없으면 장소만."""
    if not description:
        return None
    return " · ".join(part for part in (description["place"], ", ".join(description["outfits"])) if part)


def visible_photo_count(photos: list[dict]) -> int:
    """FE의 모두 보기·유사컷 접기와 같은 수: 그룹마다 표지 하나, 나머지는 사진마다 하나."""
    return len({("group", p["similarity_group_id"]) if p.get("similarity_group_id") else ("photo", p["id"])
                for p in photos})


def size_content_sections(scenes: list[list[dict]], descriptions: list[Optional[dict]],
                          sections: list[dict], vectors: dict, outfit_based: bool = False) -> tuple[list[list[dict]], list[str]]:
    """큰 촬영 흐름은 유지하고, 노출 카드가 너무 많을 때만 기존 내용 경계에서 나눈다."""
    starts = [section["start"] for section in sections]
    if not starts or starts[0] != 0 or starts != sorted(set(starts)) or starts[-1] >= len(scenes):
        raise ValueError("invalid content section boundaries")
    sections = list(sections)
    if outfit_based:
        continuous = []
        for section in sections:
            outfits = set(section.get("outfits", []))
            previous = set(continuous[-1].get("outfits", [])) if continuous else set()
            if outfits and previous and (outfits <= previous or previous <= outfits):
                if previous < outfits:
                    continuous[-1] = {**section, "start": continuous[-1]["start"]}
            else:
                continuous.append(section)
        sections = continuous
        # 의상 구간에 사람이 없는 짧은 디테일이 독립 구간으로 오판된 경우만 흡수한다. 활동 중심 촬영의 빈 무대 등은 보존.
        for i in range(len(sections) - 1, -1, -1):
            lo = sections[i]["start"]
            hi = sections[i + 1]["start"] if i + 1 < len(sections) else len(scenes)
            photos = [p for scene in scenes[lo:hi] for p in scene]
            if (len(sections) > 1 and visible_photo_count(photos) < CONTENT_VISIBLE_MIN
                    and all(d is not None and not d["outfits"] for d in descriptions[lo:hi])):
                if i == 0:
                    sections[1] = {**sections[1], "start": 0}
                del sections[i]
        starts = [section["start"] for section in sections]
    result, names = [], []

    def emit(lo, hi, name):
        photos = [photo for scene in scenes[lo:hi] for photo in scene]
        count = visible_photo_count(photos)
        cuts = []
        if count > CONTENT_VISIBLE_MAX:
            for cut in range(lo + 1, hi):
                before, after = descriptions[cut - 1], descriptions[cut]
                similarity = float(_centroid(scenes[cut - 1], vectors) @ _centroid(scenes[cut], vectors))
                if similarity >= CONTENT_MERGE_SIMILARITY:
                    continue  # 이름 오판만으로 같은 세트를 자르지 않는다.
                changed = before and after and before["place"] != after["place"]
                if before and after and not changed:
                    continue
                if not (before and after) and similarity >= CONTENT_CUT_SIMILARITY:
                    continue
                left = visible_photo_count([p for scene in scenes[lo:cut] for p in scene])
                right = visible_photo_count([p for scene in scenes[cut:hi] for p in scene])
                if min(left, right) >= CONTENT_VISIBLE_MIN:
                    cuts.append((abs(left - CONTENT_VISIBLE_TARGET), similarity, cut))
        if cuts:
            cut = min(cuts)[2]
            emit(lo, cut, name)
            emit(cut, hi, name)
        else:
            result.append(photos)
            names.append(name)

    for section, end in zip(sections, [*starts[1:], len(scenes)]):
        name = (" · ".join(section.get("outfits", [])) if outfit_based and section.get("outfits")
                else section["name"]).strip()
        if not name:
            raise ValueError("empty content section name")
        emit(section["start"], end, name)
    return result, names


# 장소 기준 장면(홈스냅): 사진마다 판정한 장소가 바뀌는 곳에서 나눈다 — 방을 쉬지 않고 옮기면 시간 공백으로는 못 잡는다.
# 라벨은 앞뒤 PLACE_WINDOW장 다수결로 고르고, PLACE_MIN_RUN장보다 짧은 구간은 긴 이웃에 붙인다(한두 장 오판정이 장면을 쪼개지 않게).
# 2026-10-05 실촬영 두 건(459장·1,572장)을 4장마다 판정한 실험(창 7·최소 3 → 사진 기준 약 29장·12장)에서 정한 값이다.
PLACE_WINDOW = 29
PLACE_MIN_RUN = 12


def split_by_place(photos: list[dict], places: dict[str, Optional[str]], placeless: set[str],
                   fallback_name: str) -> Optional[tuple[list[list[dict]], list[Optional[str]]]]:
    """photos: split_scenes와 같은 형식. places: 사진 ID → 판정한 장소(없으면 판정 안 됨). placeless: 장소를 알 수 없다는 라벨.
    CLOSE_GAP 이상 시간 공백은 장소와 상관없이 항상 나눈다(저녁 식당처럼 짧아도 따로 이동한 경우).
    반환: (장면별 사진, 장면 이름 = 장소). 장면 근거가 부족하거나(split_scenes와 같은 기준) 장소 판정이 촬영 시각 있는 사진의
    MIN_TIMED_RATIO보다 적으면 None — 시간 기준으로 나눈다."""
    timed = [photo for photo in photos if _time(scene_taken_at(photo))]
    if len(photos) < MIN_PHOTOS_FOR_SCENES or len(timed) < len(photos) * MIN_TIMED_RATIO:
        return None
    if sum(bool(places.get(photo["id"])) and places[photo["id"]] not in placeless for photo in timed) < len(timed) * MIN_TIMED_RATIO:
        return None
    ordered = sorted(timed, key=lambda photo: (_time(scene_taken_at(photo)), photo["order_index"]))
    cuts = [i for i in range(1, len(ordered)) if scene_gap(ordered[i - 1:i], ordered[i:i + 1]) >= CLOSE_GAP_SECONDS]
    scenes: list[list[dict]] = []
    names: list[Optional[str]] = []
    # 공백 사이에 한두 장만 남은 덩어리는 시간 기준과 같은 규칙으로 가까운 쪽에 붙인다.
    for block in _merge_small([ordered[a:b] for a, b in zip([0, *cuts], [*cuts, len(ordered)])]):
        known = [places.get(photo["id"]) if places.get(photo["id"]) not in placeless else None for photo in block]
        if not any(known):
            scenes.append(block)
            names.append(fallback_name)
            continue
        filled, last = [], next(label for label in known if label)
        for label in known:  # 판정 없음·장소 없음은 바로 앞 장소를 따른다(맨 앞은 처음 나오는 장소)
            last = label or last
            filled.append(last)
        half = PLACE_WINDOW // 2
        smooth = [Counter(filled[max(0, i - half):i + half + 1]).most_common(1)[0][0] for i in range(len(filled))]
        runs: list[list] = []  # [시작, 끝(미포함), 장소]
        for i, label in enumerate(smooth):
            if runs and runs[-1][2] == label:
                runs[-1][1] = i + 1
            else:
                runs.append([i, i + 1, label])
        while len(runs) > 1 and min(end - start for start, end, _ in runs) < PLACE_MIN_RUN:
            k = min(range(len(runs)), key=lambda i: runs[i][1] - runs[i][0])
            j = max((i for i in (k - 1, k + 1) if 0 <= i < len(runs)), key=lambda i: runs[i][1] - runs[i][0])
            lo, hi = min(k, j), max(k, j)
            runs[lo:hi + 1] = [[runs[lo][0], runs[hi][1], runs[j][2]]]
            merged: list[list] = []
            for run in runs:
                if merged and merged[-1][2] == run[2]:
                    merged[-1][1] = run[1]
                else:
                    merged.append(run)
            runs = merged
        for start, end, label in runs:
            scenes.append(block[start:end])
            names.append(label)
    untimed = sorted((photo for photo in photos if not _time(scene_taken_at(photo))), key=lambda photo: photo["order_index"])
    if untimed:
        scenes.append(untimed)
        names.append(None)
    return scenes, names
