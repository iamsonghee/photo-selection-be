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
