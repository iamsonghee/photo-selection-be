"""보정본 업로드 시 파일명 매칭(exact/fuzzy)에 실패한 잔여 항목을 CLIP 유사도로 매칭한다.

원본 사진은 photos.clip_embedding에 저장된 값을 우선 재사용하고, 없으면(분석을 한 번도
돌리지 않은 프로젝트) 그 자리에서 계산해 같은 컬럼에 저장(lazy backfill)한다.
"""
import asyncio
import logging
from typing import List, Optional, Tuple

import numpy as np

from app.clip_model import compute_embeddings
from app.downloader import download_all
from app.embeddings_store import persist_embeddings
from app.grouping import _cosine

logger = logging.getLogger(__name__)

AUTO_THRESHOLD = 0.85
LOW_THRESHOLD = 0.60


async def match_retouch(
    supabase,
    project_id: str,
    photo_ids: List[str],
    retouch_files: List[Tuple[str, bytes]],
) -> List[dict]:
    if not photo_ids or not retouch_files:
        return []

    loop = asyncio.get_event_loop()

    photos_r = (
        supabase.table("photos")
        .select("id, r2_thumb_url, clip_embedding")
        .eq("project_id", project_id)
        .in_("id", photo_ids)
        .execute()
    )
    rows = photos_r.data or []
    if not rows:
        return []

    embedding_by_id: dict[str, Optional[np.ndarray]] = {}
    missing_rows = []
    for row in rows:
        emb = row.get("clip_embedding")
        if emb:
            embedding_by_id[row["id"]] = np.array(emb, dtype=np.float32)
        else:
            missing_rows.append(row)

    if missing_rows:
        urls = [r["r2_thumb_url"] for r in missing_rows]
        images = await download_all(urls)
        computed = await loop.run_in_executor(
            None, compute_embeddings, [img for img in images if img is not None]
        )
        img_iter = iter(computed)
        full_computed = [next(img_iter) if img is not None else None for img in images]

        persist_embeddings(supabase, list(zip([r["id"] for r in missing_rows], full_computed)))
        for row, vec in zip(missing_rows, full_computed):
            embedding_by_id[row["id"]] = vec

    photo_meta = [
        {"photo_id": pid, "embedding": vec}
        for pid, vec in embedding_by_id.items()
        if vec is not None
    ]
    if not photo_meta:
        return []

    file_names = [name for name, _ in retouch_files]
    file_bytes = [data for _, data in retouch_files]
    file_embeddings = await loop.run_in_executor(None, compute_embeddings, file_bytes)

    file_meta = [
        {"filename": name, "embedding": vec}
        for name, vec in zip(file_names, file_embeddings)
        if vec is not None
    ]
    if not file_meta:
        return []

    similarity_matrix = [
        [float(_cosine(f["embedding"], p["embedding"])) for p in photo_meta]
        for f in file_meta
    ]

    return _greedy_assign(similarity_matrix, file_meta, photo_meta)


def _greedy_assign(
    similarity_matrix: List[List[float]],
    file_meta: List[dict],
    photo_meta: List[dict],
) -> List[dict]:
    pairs = []
    for fi, row in enumerate(similarity_matrix):
        for pi, sim in enumerate(row):
            pairs.append((sim, fi, pi))
    pairs.sort(key=lambda x: x[0], reverse=True)

    claimed_files: set[int] = set()
    claimed_photos: set[int] = set()
    results: List[dict] = []
    for sim, fi, pi in pairs:
        if sim < LOW_THRESHOLD:
            break  # 내림차순 정렬이므로 이후 전부 기준 미달
        if fi in claimed_files or pi in claimed_photos:
            continue
        claimed_files.add(fi)
        claimed_photos.add(pi)
        results.append(
            {
                "photo_id": photo_meta[pi]["photo_id"],
                "filename": file_meta[fi]["filename"],
                "similarity": round(sim, 4),
                "type": "clip" if sim >= AUTO_THRESHOLD else "clip_low",
            }
        )
    return results
