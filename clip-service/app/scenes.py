"""셀프 고객 사진의 "장면" 나누기 — FE `src/lib/customer-scenes.ts`의 splitScenes와 같은 규칙.

촬영 시각 사이에 큰 공백이 생기는 지점에서 나눈다. 사진 내용(임베딩)으로 경계를 보정하지 않는다:
같은 홀에서 이어지는 장면은 사진이 비슷해 "비슷하면 합치기"가 장면을 잘못 합치고, 실제 촬영 데이터 없이
기준값을 정할 수 없어서다.
# ponytail: 시각 공백 휴리스틱. 실제 촬영 데이터가 모이면 임베딩 변화로 긴 장면을 나누는 보정을 더한다.
"""
from __future__ import annotations

import os
import re
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


CONTENT_FILENAME_MIN_DIGITS = 4
# 앞자리 연번(`0001_원래이름.jpg`)은 이 자릿수까지만 연번으로 본다 — 휴대폰 이름 `20261008_123456.jpg`의 앞 8자리는 날짜다.
CONTENT_FILENAME_LEADING_MAX_DIGITS = 6
CONTENT_FILENAME_MAX_SPAN_FACTOR = 10
# 세트를 특정하지 못한 넓은 묘사만 사진 내용으로 보완한다. 구체적인 같은 세트명은 포즈가 달라도 유지한다.
CONTENT_GENERIC_PLACES = frozenset({"실내", "실내배경", "스튜디오", "야외", "야외배경", "야외정원", "정원", "알수없음"})


def _filename_sequence(names: list[str]) -> Optional[list[int]]:
    """파일명마다 순서 번호. 하나의 촘촘한 연번으로 읽히지 않으면 None.
    1) 숫자가 한 묶음뿐인 이름(`IMG_0001.jpg`) — 앞뒤 글자까지 모두 같은 체계여야 한다.
    2) 내보내기 프로그램이 앞에 붙인 연번(`0001_SJ7_3213.jpg`) — 뒤에 원래 카메라 이름이 섞여 있어도 앞 번호 자릿수가 같으면 한 체계로 본다
       (2026-10-14 본식 보정본 두 건: 여러 카메라 원본명 앞에 0001~ 연번)."""
    single = [re.fullmatch(r"([^\d]*)(\d{%d,})([^\d]*)" % CONTENT_FILENAME_MIN_DIGITS, name) for name in names]
    if all(single):
        signatures = {(m[1].casefold(), len(m[2]), m[3].casefold()) for m in single}
        numbers = [int(m[2]) for m in single]
    else:
        leading = [re.match(r"(\d{%d,%d})[^\d]" % (CONTENT_FILENAME_MIN_DIGITS, CONTENT_FILENAME_LEADING_MAX_DIGITS), name)
                   for name in names]
        if not all(leading):
            return None
        signatures = {len(m[1]) for m in leading}
        numbers = [int(m[1]) for m in leading]
    # ponytail: 단일 체계의 촘촘한 연번 추정. 여러 체계·중복·큰 번호 공백은 원래 순서를 유지한다.
    if (len(signatures) != 1 or len(set(numbers)) != len(numbers)
            or max(numbers) - min(numbers) > len(names) * CONTENT_FILENAME_MAX_SPAN_FACTOR):
        return None
    return numbers


def content_order(photos: list[dict]) -> list[dict]:
    """촬영 시각이 없고 하나의 촘촘한 연속 번호일 때만 순서를 복원한다(`_filename_sequence`). 입력 행은 변경하지 않는다."""
    if not photos or any(scene_taken_at(p) for p in photos):
        return photos
    numbers = _filename_sequence([p.get("filename") or p.get("original_filename") or "" for p in photos])
    if numbers is None:
        return photos
    return [{**photo, "order_index": i} for i, (_, photo) in enumerate(sorted(zip(numbers, photos), key=lambda pair: pair[0]))]


def split_by_content(photos: list[dict], vectors: list) -> Optional[list[list[dict]]]:
    """photos: split_scenes와 같은 형식, vectors: 같은 순서의 임베딩(없으면 None). 사진이 적거나 임베딩이 없는 사진이 있으면 None.
    반환: 업로드 순서의 작은 구간 목록(큰 촬영 흐름을 정하고 크기를 나눌 때 쓸 경계 후보)."""
    if len(photos) < MIN_PHOTOS_FOR_SCENES or any(vector is None for vector in vectors):
        return None
    order = sorted(range(len(photos)), key=lambda i: photos[i]["order_index"])
    unit = np.asarray([vectors[i] for i in order], dtype=np.float64)
    unit /= np.linalg.norm(unit, axis=1, keepdims=True)
    cuts = _change_points(unit, CONTENT_WINDOW, CONTENT_CUT_SIMILARITY, CONTENT_MIN_SCENE_PHOTOS, CONTENT_MAX_SCENES - 1)
    bounds = [0, *cuts, len(order)]
    return [[photos[order[k]] for k in range(a, b)] for a, b in zip(bounds, bounds[1:])]


def _change_points(unit: np.ndarray, window: int, threshold: float, min_gap: int, max_cuts: Optional[int] = None) -> list[int]:
    """사진 순서로 놓인 정규화 임베딩에서 앞뒤 window장 평균의 코사인 유사도가 threshold 아래인 위치를 낮은 순으로 고른 경계
    (양 끝·서로 min_gap장 이상 떨어진 것만, 최대 max_cuts개). 정렬된 위치 목록."""
    scores = []
    for i in range(window, len(unit) - window + 1):
        before, after = unit[i - window:i].mean(0), unit[i:i + window].mean(0)
        scores.append((float(before @ after / np.linalg.norm(before) / np.linalg.norm(after)), i))
    cuts: list[int] = []
    for score, i in sorted(scores):
        if score >= threshold or (max_cuts is not None and len(cuts) >= max_cuts):
            break
        if all(abs(i - cut) >= min_gap for cut in (0, *cuts, len(unit))):
            cuts.append(i)
    return sorted(cuts)


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
                generic = before and after and all("".join(d["place"].split()) in CONTENT_GENERIC_PLACES for d in (before, after))
                # 구체적인 같은 세트명은 유지. 넓은 장소명·묘사 누락만 사진 변화로 보완한다.
                if not changed and (before and after and not generic or similarity >= CONTENT_CUT_SIMILARITY):
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
# 단계 기준 장면(웨딩 본식): 같은 구조로 사진마다 판정한 촬영 단계가 바뀌는 곳에서 나눈다. 보정본은 촬영 시각이 없어 업로드 순서
# (파일명 연번 복원 뒤, `content_order`)로 다듬는다. 신랑 입장처럼 20장 안팎인 짧은 단계가 29장 창에서는 지워질 수 있어 창을 줄였다
# — 창 21장이면 깨끗한 11장 구간이 남는다. 최소 구간도 그에 맞춰 10장. 판정 "알 수 없음"(하객 얼굴·손 클로즈업 등)은 앞 단계로
# 채우므로 판정 비율 기준을 장소(80%)보다 낮춘다.
# 임베딩이 없는 사진이 있을 때(유사컷 분석 실패 등)만 쓰는 대체 다듬기다 — 기본은 아래 임베딩 구간 다듬기.
STAGE_WINDOW = 21
STAGE_MIN_RUN = 10
STAGE_MIN_LABELED_RATIO = 0.5
# 임베딩 구간 다듬기(단계 기준 기본): 사진 내용이 바뀌는 곳(앞뒤 5장 평균 유사도 < 0.90, 구간 최소 4장)에서 잘게 자른 구간마다 판정 다수결로
# 라벨을 정하고, STAGE_SEGMENT_MIN_RUN장보다 짧은 조각은 원래 판정이 더 잘 맞는 위치에서 양쪽 이웃으로 나눠 붙인다(가장자리 조각은 이웃에 합침).
# 고정 창 다수결은 단계가 바뀌는 자리를 창 절반만큼 밀거나 짧은 단계를 지운다. 2026-10-15 실측(10장 묶음 단계 판정, 잠정 정답):
# 짱쭌 1,285장 사진 정확도 94.1 → 94.7%, 경계(±3장) 정밀도 73 → 90%. 해군 3,439장 79.7 → 81.0%, 정밀도 27 → 52%.
# 경계 후보(0.90)는 두 프로젝트 정답 경계를 ±5장 안에 10/11, 26/29 잡았다. 0.88로 낮추면 짱쭌이 87.9%로 떨어졌다.
STAGE_SEGMENT_WINDOW = 5
STAGE_SEGMENT_CUT_SIMILARITY = 0.90
STAGE_SEGMENT_MIN_PHOTOS = 4
STAGE_SEGMENT_MIN_RUN = 15
# 카메라별로 따로 내보낸 보정본: 카메라 A 전체 뒤에 카메라 B 전체를 이어 붙이면(해군 2026-10: OS1 0001-1865, OS2 1866-3439) 파일명 순서로
# 놓을 때 본식이 카메라 수만큼 반복된다. 촬영 시각이 없어 카메라 사이 순서는 모르므로 카메라마다 따로 다듬고 단계 순서를 맞춰 합친다.
# 카메라 이름 = 앞에 붙인 연번을 뗀 파일명에서 카메라 자체 번호(끝 4자리)를 뺀 부분. 카메라가 한 덩어리씩 이어져 있고 모두 이 장수 이상일 때만
# 카메라별로 본다 — 두 카메라가 시간순으로 섞여 있으면(짱쭌 SJ7·SJ8) 파일명 순서가 곧 촬영 순서다.
CAMERA_MIN_PHOTOS = 100
CAMERA_COUNTER_DIGITS = 4


def _label_runs(known: list[Optional[str]], window: int, min_run: int) -> list[list]:
    """사진 순서대로 놓인 라벨(None = 판정 없음·장소 없음)을 다듬어 [시작, 끝(미포함), 라벨] 구간으로 만든다.
    known에 라벨이 하나 이상 있어야 한다."""
    filled, last = [], next(label for label in known if label)
    for label in known:  # 판정 없음·장소 없음은 바로 앞 라벨을 따른다(맨 앞은 처음 나오는 라벨)
        last = label or last
        filled.append(last)
    half = window // 2
    smooth = [Counter(filled[max(0, i - half):i + half + 1]).most_common(1)[0][0] for i in range(len(filled))]
    runs: list[list] = []
    for i, label in enumerate(smooth):
        if runs and runs[-1][2] == label:
            runs[-1][1] = i + 1
        else:
            runs.append([i, i + 1, label])
    while len(runs) > 1 and min(end - start for start, end, _ in runs) < min_run:
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
    return runs


def _runs_of(labels: list) -> list[list]:
    """같은 라벨이 이어진 [시작, 끝(미포함), 라벨] 구간."""
    runs: list[list] = []
    for i, label in enumerate(labels):
        if runs and runs[-1][2] == label:
            runs[-1][1] = i + 1
        else:
            runs.append([i, i + 1, label])
    return runs


def _segment_runs(known: list[Optional[str]], unit: np.ndarray, min_run: int = STAGE_SEGMENT_MIN_RUN) -> list[list]:
    """임베딩 구간 다듬기(STAGE_SEGMENT_*). known: 사진 순서 라벨(None = 판정 없음, 하나 이상 있어야 한다), unit: 같은 순서의 정규화 임베딩.
    반환: `_label_runs`와 같은 [시작, 끝(미포함), 라벨] 구간."""
    bounds = [0, *_change_points(unit, STAGE_SEGMENT_WINDOW, STAGE_SEGMENT_CUT_SIMILARITY, STAGE_SEGMENT_MIN_PHOTOS), len(known)]
    voted: list[Optional[str]] = []
    for a, b in zip(bounds, bounds[1:]):
        counts = Counter(label for label in known[a:b] if label)
        voted += [counts.most_common(1)[0][0] if counts else None] * (b - a)
    filled, last = [], next(label for label in voted if label)
    for label in voted:  # 판정이 하나도 없는 구간은 앞 구간을 따른다(맨 앞은 처음 나오는 라벨)
        last = label or last
        filled.append(last)
    runs = _runs_of(filled)
    while len(runs) > 1 and min(end - start for start, end, _ in runs) < min_run:
        k = min(range(len(runs)), key=lambda i: runs[i][1] - runs[i][0])
        start, end, _ = runs[k]
        if 0 < k < len(runs) - 1:
            left, right = runs[k - 1][2], runs[k + 1][2]
            cut = max(range(start, end + 1), key=lambda c: sum(known[i] == left for i in range(start, c))
                      + sum(known[i] == right for i in range(c, end)))
            runs[k - 1][1] = runs[k + 1][0] = cut
            del runs[k]
        else:
            j = 1 if k == 0 else k - 1
            lo = min(k, j)
            runs[lo:lo + 2] = [[runs[lo][0], runs[lo + 1][1], runs[j][2]]]
        merged: list[list] = []
        for run in runs:
            if run[1] <= run[0]:
                continue
            if merged and merged[-1][2] == run[2]:
                merged[-1][1] = run[1]
            else:
                merged.append(run)
        runs = merged
    return runs


def camera_key(filename: str) -> Optional[str]:
    """파일명의 카메라 이름 부분(`0001_OS100353.jpg` → `os10`, `IMG_1234.JPG` → `img_`). 끝에 카메라 번호가 없으면 None."""
    stem = re.sub(r"\.[^.]*$", "", filename)
    stem = re.sub(r"^\d{%d,%d}[^\d]" % (CONTENT_FILENAME_MIN_DIGITS, CONTENT_FILENAME_LEADING_MAX_DIGITS), "", stem)
    match = re.fullmatch(r"(.*?)(\d{%d})" % CAMERA_COUNTER_DIGITS, stem)
    return match[1].casefold() if match else None


def camera_blocks(ordered: list[dict]) -> Optional[list[list[dict]]]:
    """순서대로 놓인 사진이 카메라별로 한 덩어리씩 이어져 있으면(카메라 2대 이상, 모두 CAMERA_MIN_PHOTOS장 이상) 카메라별 사진 목록, 아니면 None."""
    blocks: list[list] = []
    for photo in ordered:
        key = camera_key(photo.get("filename") or photo.get("original_filename") or "")
        if key is None:
            return None
        if blocks and blocks[-1][0] == key:
            blocks[-1][1].append(photo)
        else:
            blocks.append([key, [photo]])
    if len(blocks) < 2 or len({key for key, _ in blocks}) != len(blocks) or min(len(block) for _, block in blocks) < CAMERA_MIN_PHOTOS:
        return None
    return [block for _, block in blocks]


def _main_span(pieces: list, main: set[str]) -> Optional[tuple[int, int]]:
    """본식 범위: 본식 단계 사진 수에서 그 밖 사진 수를 뺀 값이 가장 큰 연속 구간 [처음, 끝](구간 번호, 끝 포함). 본식 단계가 없으면 None.
    pieces: [(사진, 라벨, ...)] 순서대로."""
    best, total, start = None, 0, 0
    for k, piece in enumerate(pieces):
        weight = len(piece[0]) if piece[1] in main else -len(piece[0])
        total, start = (weight, k) if total <= 0 else (total + weight, start)
        if best is None or total > best[0]:
            best = (total, start, k)
    return (best[1], best[2]) if best and best[0] > 0 else None


def _settle_outside_main(pieces: list[list], main: set[str]) -> list[list]:
    """본식 범위(`_main_span`) 밖에 떨어진 본식 단계 구간을 같은 쪽 이웃 중 더 긴 비본식 단계로 돌리고, 이웃한 같은 단계는 합친다.
    pieces: [사진, 라벨, 덩어리] — 덩어리(시간 공백·카메라)가 다른 구간은 합치지 않는다. 행진·입장은 본식 밖에서 일어나지 않는다:
    식 전 빈 홀 버진로드 리허설이 행진으로, 대기실 앞 인사가 신랑 입장으로 읽힌다(2026-10-15 해군 카메라 2, 사진 정확도 81.0 → 84.0%,
    짱쭌은 바뀐 사진 없음)."""
    span = _main_span(pieces, main)
    if span:
        outside = lambda j: not span[0] <= j <= span[1] and pieces[j][1] not in main  # noqa: E731
        for k, piece in enumerate(pieces):
            if piece[1] in main and not span[0] <= k <= span[1]:
                neighbors = [pieces[j] for j in (k - 1, k + 1) if 0 <= j < len(pieces) and outside(j)]
                if neighbors:
                    piece[1] = max(neighbors, key=lambda other: len(other[0]))[1]
    merged: list[list] = []
    for piece in pieces:
        if merged and merged[-1][1] == piece[1] and merged[-1][2] == piece[2]:
            merged[-1] = [merged[-1][0] + piece[0], piece[1], piece[2]]
        else:
            merged.append(list(piece))
    return merged


def _positions(timeline: list[tuple[list[dict], Optional[str]]]) -> list[float]:
    """구간마다 타임라인 안 상대 위치(가운데 사진 기준 0~1)."""
    total, done, out = sum(len(photos) for photos, _ in timeline), 0, []
    for photos, _ in timeline:
        out.append((done + len(photos) / 2) / total)
        done += len(photos)
    return out


def _join_same(pieces: list[tuple[list[dict], Optional[str]]]) -> list[tuple[list[dict], Optional[str]]]:
    merged: list[tuple[list[dict], Optional[str]]] = []
    for photos, label in pieces:
        if merged and merged[-1][1] == label:
            merged[-1] = (merged[-1][0] + photos, label)
        else:
            merged.append((photos, label))
    return merged


def _align_timelines(a: list[tuple[list[dict], Optional[str]]], b: list[tuple[list[dict], Optional[str]]]):
    """두 카메라 타임라인([(사진, 라벨)])을 단계로 맞춰 합친다. 같은 라벨 구간끼리 짝지어(순서를 지키는 짝 중 짝지은 사진 수 — 두 구간 중 작은 쪽 —
    합이 가장 큰 것, 같으면 상대 위치가 가까운 것) 한 장면으로 합치고, 짝 사이에 남은 구간은 상대 위치 순서로 끼운다. 카메라 안 순서는 바뀌지 않는다.
    짝 수만 세면 오판정으로 생긴 작은 구간끼리 짝지어져 큰 예식이 화촉점화 앞으로 갔다(2026-10-15 해군).
    짝 없는 작은 구간을 이웃에 붙이면 목록은 짧아지지만 진짜 축가까지 사라지고 정확도도 오르지 않아 붙이지 않는다
    (2026-10-17 해군 측정: 30장 미만 흡수 시 본식 14 → 5개, 축가 0장, 사진 84.9 → 84.2%)."""
    if not a or not b:
        return _join_same(a or b)
    pa, pb = _positions(a), _positions(b)
    n, m = len(a), len(b)
    best = [[(0, 0.0)] * (m + 1) for _ in range(n + 1)]  # a[i:]·b[j:]에서 (짝지은 사진 수, -위치 차이 합)
    step = [[0] * (m + 1) for _ in range(n + 1)]  # 0: a 건너뜀, 1: b 건너뜀, 2: 짝
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            options = [best[i + 1][j], best[i][j + 1]]
            if a[i][1] == b[j][1]:
                matched, cost = best[i + 1][j + 1]
                options.append((matched + min(len(a[i][0]), len(b[j][0])), cost - abs(pa[i] - pb[j])))
            step[i][j] = max(range(len(options)), key=lambda k: options[k])
            best[i][j] = options[step[i][j]]
    out: list[tuple[list[dict], Optional[str]]] = []
    loose: list[tuple[float, int, tuple]] = []

    def flush():
        out.extend(piece for _, _, piece in sorted(loose, key=lambda item: item[:2]))
        loose.clear()

    i = j = 0
    while i < n and j < m:
        if step[i][j] == 2:
            flush()
            out.append((a[i][0] + b[j][0], a[i][1]))
            i, j = i + 1, j + 1
        elif step[i][j] == 0:
            loose.append((pa[i], 0, a[i]))
            i += 1
        else:
            loose.append((pb[j], 1, b[j]))
            j += 1
    loose += [(pa[k], 0, a[k]) for k in range(i, n)] + [(pb[k], 1, b[k]) for k in range(j, m)]
    flush()
    return _join_same(out)


def merge_camera_timelines(timelines: list[list[tuple[list[dict], Optional[str]]]],
                           main: Optional[set[str]] = None) -> list[tuple[list[dict], Optional[str]]]:
    """카메라별 타임라인을 앞에서부터 차례로 합친다. 본식 단계(main)가 있고 두 쪽 모두 본식 범위가 있으면 식 전·본식·식 후를 따로 맞춰
    이어 붙인다(식 전 대기실과 식 후 연출이 서로 짝지어지지 않게). 아니면 전체를 `_align_timelines`로 맞춘다."""
    def phases(timeline):
        span = _main_span(timeline, main) if main else None
        return span and (timeline[:span[0]], timeline[span[0]:span[1] + 1], timeline[span[1] + 1:])

    merged = timelines[0]
    for timeline in timelines[1:]:
        left, right = phases(merged), phases(timeline)
        if left and right:
            merged = _join_same([piece for x, y in zip(left, right) for piece in _align_timelines(x, y)])
        else:
            merged = _align_timelines(merged, timeline)
    return merged


def split_by_place(photos: list[dict], places: dict[str, Optional[str]], placeless: set[str], fallback_name: str, *,
                   window: int = PLACE_WINDOW, min_run: int = PLACE_MIN_RUN, untimed_order: bool = False,
                   min_labeled_ratio: float = MIN_TIMED_RATIO, vectors: Optional[dict] = None,
                   main_stages: Optional[set[str]] = None) -> Optional[tuple[list[list[dict]], list[Optional[str]]]]:
    """photos: split_scenes와 같은 형식. places: 사진 ID → 판정한 장소(없으면 판정 안 됨). placeless: 장소를 알 수 없다는 라벨.
    촬영 시각이 있으면(MIN_TIMED_RATIO 이상) 시각순으로 놓고, CLOSE_GAP 이상 시간 공백은 장소와 상관없이 항상 나눈다
    (저녁 식당처럼 짧아도 따로 이동한 경우). 시각이 부족하면 untimed_order일 때만 order_index 순서로 다듬는다(단계 기준, 보정본) —
    카메라별로 따로 내보낸 파일이 이어 붙어 있으면(`camera_blocks`) 카메라마다 다듬고 단계 순서를 맞춰 합친다(`merge_camera_timelines`).
    window·min_run: 라벨 다수결 창·최소 구간(`_label_runs`). min_labeled_ratio: 다듬을 사진 중 라벨이 있어야 하는 비율.
    vectors: 사진 ID → 임베딩. 덩어리의 모든 사진에 있으면 임베딩 구간 다듬기(`_segment_runs`)를 쓴다(단계 기준만 넘긴다).
    main_stages: 본식 단계(단계 기준만). 있으면 타임라인(카메라)마다 본식 범위 밖 본식 단계 구간을 이웃 단계로 돌리고(`_settle_outside_main`),
    카메라를 합칠 때 식 전·본식·식 후를 따로 맞춘다.
    반환: (장면별 사진, 장면 이름 = 라벨). 장면 근거가 부족하면 None — 다른 기준으로 나눈다."""
    if len(photos) < MIN_PHOTOS_FOR_SCENES:
        return None
    timed = [photo for photo in photos if _time(scene_taken_at(photo))]
    cameras = None
    if len(timed) >= len(photos) * MIN_TIMED_RATIO:
        ordered = sorted(timed, key=lambda photo: (_time(scene_taken_at(photo)), photo["order_index"]))
        cuts = [i for i in range(1, len(ordered)) if scene_gap(ordered[i - 1:i], ordered[i:i + 1]) >= CLOSE_GAP_SECONDS]
        # 공백 사이에 한두 장만 남은 덩어리는 시간 기준과 같은 규칙으로 가까운 쪽에 붙인다.
        blocks = _merge_small([ordered[a:b] for a, b in zip([0, *cuts], [*cuts, len(ordered)])])
        untimed = sorted((photo for photo in photos if not _time(scene_taken_at(photo))), key=lambda photo: photo["order_index"])
    elif untimed_order:
        ordered = sorted(photos, key=lambda photo: photo["order_index"])
        cameras = camera_blocks(ordered)
        blocks, untimed = cameras or [ordered], []
    else:
        return None
    if sum(bool(places.get(photo["id"])) and places[photo["id"]] not in placeless for photo in ordered) < len(ordered) * min_labeled_ratio:
        return None

    def block_runs(block: list[dict]) -> list[tuple[list[dict], Optional[str]]]:
        known = [places.get(photo["id"]) if places.get(photo["id"]) not in placeless else None for photo in block]
        if not any(known):
            return [(block, fallback_name)]
        if vectors and len(block) > 2 * STAGE_SEGMENT_WINDOW and all(vectors.get(photo["id"]) is not None for photo in block):
            unit = np.asarray([vectors[photo["id"]] for photo in block], dtype=np.float64)
            unit /= np.linalg.norm(unit, axis=1, keepdims=True)
            runs = _segment_runs(known, unit)
        else:
            runs = _label_runs(known, window, min_run)
        return [(block[start:end], label) for start, end, label in runs]

    timelines = [block_runs(block) for block in blocks]
    if cameras:
        if main_stages:
            timelines = [[(photos, label) for photos, label, _ in _settle_outside_main([[p, n, 0] for p, n in timeline], main_stages)]
                         for timeline in timelines]
        pieces = merge_camera_timelines(timelines, main_stages)
    else:
        tagged = [[photos, label, k] for k, timeline in enumerate(timelines) for photos, label in timeline]
        if main_stages:
            tagged = _settle_outside_main(tagged, main_stages)
        pieces = [(photos, label) for photos, label, _ in tagged]
    scenes = [photos for photos, _ in pieces]
    names: list[Optional[str]] = [name for _, name in pieces]
    if untimed:
        scenes.append(untimed)
        names.append(None)
    return scenes, names
