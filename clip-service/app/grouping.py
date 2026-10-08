"""number 순서 기준 인접 코사인 유사도 + union-find 그룹핑.

burst shot(거의 동일한 연속 촬영본)은 항상 연속된 number로 업로드되므로
전체 N x N 비교 없이 인접한 사진끼리만 비교하면 충분하다.
"""
from typing import List, Optional

import numpy as np


class _UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b))


def group_by_similarity(
    embeddings: List[Optional[np.ndarray]], threshold: float
) -> List[List[int]]:
    """embeddings[i]는 정렬된 순서(number 순)의 i번째 사진 임베딩 (정규화됨, None 가능).
    반환: 2장 이상인 그룹들의 인덱스 리스트 목록."""
    n = len(embeddings)
    uf = _UnionFind(n)

    for i in range(1, n):
        a, b = embeddings[i - 1], embeddings[i]
        if a is None or b is None:
            continue
        if _cosine(a, b) >= threshold:
            uf.union(i - 1, i)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        root = uf.find(i)
        groups.setdefault(root, []).append(i)

    return [members for members in groups.values() if len(members) >= 2]


# 셀프 고객 유사컷. 고정 기준(0.94)으로 이웃끼리만 이으면 배경·조명이 같은 스튜디오 촬영은 이웃 유사도가 대부분 0.98이라
# 포즈가 바뀌어도(0.96~0.97) 계속 이어져 세트 하나(111장)가 한 묶음이 됐다(2026-10-07 웨딩 스튜디오 2,249장).
# 그래서 기준을 프로젝트 이웃 유사도의 하위 SHOT_PERCENTILE%로 올리되 SHOT_MIN~SHOT_MAX 안에 둔다 — 스튜디오는 0.97 근처,
# 홈스냅·돌잔치처럼 장면이 다양한 촬영은 그대로 0.94. 실사진으로 본 같은 포즈 경계 8개를 0.972에서 모두 맞혔다.
# ponytail: 5개 프로젝트로 정한 값. 사람이 표시한 유사컷 정답이 생기면 다시 맞춘다.
SHOT_PERCENTILE = 25
SHOT_MIN = 0.94
SHOT_MAX = 0.972
# 조금씩 달라지며 이어지는 사슬을 끊는다: 묶음 첫 사진과도 이만큼 이내로 비슷해야 한다.
SHOT_ANCHOR_MARGIN = 0.03
# 촬영 시각이 둘 다 있으면 이보다 떨어진 사진은 묶지 않는다 — 스튜디오에서 같은 포즈를 다듬는 데 길게 30초쯤 걸렸다(99%).
SHOT_MAX_GAP_SECONDS = 30


def shot_threshold(embeddings: List[Optional[np.ndarray]]) -> float:
    sims = [_cosine(a, b) for a, b in zip(embeddings, embeddings[1:]) if a is not None and b is not None]
    return min(SHOT_MAX, max(SHOT_MIN, float(np.percentile(sims, SHOT_PERCENTILE)))) if sims else SHOT_MIN


def group_shots(embeddings: List[Optional[np.ndarray]], times: List[Optional[float]]) -> List[List[int]]:
    """embeddings[i]: 촬영 순서 i번째 사진 임베딩(정규화됨, None 가능). times[i]: 촬영 시각(초, 모르면 None).
    반환: 2장 이상인 묶음들의 인덱스 목록(연속 구간)."""
    threshold = shot_threshold(embeddings)
    groups: list[list[int]] = []
    current: list[int] = [0] if embeddings else []
    for i in range(1, len(embeddings)):
        a, b, first = embeddings[i - 1], embeddings[i], embeddings[current[0]]
        same = (a is not None and b is not None and first is not None and _cosine(a, b) >= threshold
                and _cosine(first, b) >= threshold - SHOT_ANCHOR_MARGIN
                and (times[i] is None or times[i - 1] is None or times[i] - times[i - 1] <= SHOT_MAX_GAP_SECONDS))
        if same:
            current.append(i)
        else:
            groups.append(current)
            current = [i]
    groups.append(current)
    return [members for members in groups if len(members) >= 2]
