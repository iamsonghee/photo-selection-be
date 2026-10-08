import numpy as np

from app.grouping import SHOT_MAX, SHOT_MIN, group_shots, shot_threshold


def _unit(angle):
    return np.array([np.cos(angle), np.sin(angle), 0.0])


def test_studio_drift_does_not_chain_into_one_group():
    # 스튜디오: 이웃은 늘 0.96 넘게 비슷해 고정 0.94면 전부 한 묶음 — 포즈가 바뀌는 곳(0.966)에서 끊어야 한다.
    angles = [pose * 0.3 + i * 0.01 for pose in range(4) for i in range(5)]
    vectors = [_unit(angle) for angle in angles]
    assert shot_threshold(vectors) == SHOT_MAX
    assert [len(group) for group in group_shots(vectors, [None] * len(vectors))] == [5, 5, 5, 5]


def test_varied_shoot_keeps_base_threshold():
    rng = np.random.default_rng(0)
    vectors = [v / np.linalg.norm(v) for v in rng.normal(size=(40, 8))]
    assert shot_threshold(vectors) == SHOT_MIN


def test_time_gap_splits_same_framing():
    vectors = [_unit(0.0)] * 5
    assert group_shots(vectors, [0, 2, 4, 100, 102]) == [[0, 1, 2], [3, 4]]
    assert group_shots([], []) == []
