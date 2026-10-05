import asyncio

from app import customer_ai


class _Query:
    """range()로 요청한 구간만 돌려주는 가짜 PostgREST 조회. 요청 구간을 기록한다."""

    def __init__(self, rows, calls):
        self.rows, self.calls = rows, calls

    def __getattr__(self, _name):  # select/eq/order 등은 그대로 이어 붙인다
        return lambda *args, **kwargs: self

    def range(self, start, end):
        self.calls.append((start, end))
        self.data = self.rows[start:end + 1]
        return self

    def execute(self):
        return self


class _Db:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def table(self, _name):
        return _Query(self.rows, self.calls)


def test_stored_embeddings_are_read_in_small_pages():
    # 임베딩 한 행이 JSON 약 62KB라 1,000행 한 번에 읽으면 운영 DB가 멈췄다 — EMBEDDING_PAGE_ROWS씩 나눠 읽어야 한다.
    rows = [{"photo_id": f"p{i:04d}", "embedding": [0.0, 1.0]} for i in range(250)]
    db = _Db(rows)
    photos = [{"id": row["photo_id"]} for row in rows]
    vectors = asyncio.run(customer_ai._embeddings(db, "run", "project", photos))
    assert all(vector is not None for vector in vectors)
    assert db.calls == [(0, 99), (100, 199), (200, 299)]
    assert all(end - start + 1 <= customer_ai.EMBEDDING_PAGE_ROWS for start, end in db.calls)
