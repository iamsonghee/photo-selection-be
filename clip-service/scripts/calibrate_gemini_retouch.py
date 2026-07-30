"""1단계 실측 스크립트 — 보정본↔원본 Gemini 매칭 임계값 산정을 위한 데이터 수집.

읽기 전용: 운영 DB에서 SELECT만 하고 아무것도 쓰지 않는다. main.py의 /match-retouch
운영 경로는 건드리지 않는다(OpenCLIP 그대로). Gemini API에 이미지 임베딩 요청만 보낸다
(소액 비용 발생, 쓰기 없음).

측정 항목:
A) 실제 원본-보정본 쌍(운영 데이터, photo_versions의 기존 매칭 = ground truth)의
   positive 점수 + 같은 프로젝트 내 다른 원본들과의 순위(오매칭 위험, margin 분석용).
B) 같은 촬영(버스트샷 유사) 내 서로 다른 원본들 간 교차 유사도 — 연속컷 오매칭 위험 직접 측정.
C) 합성 편집(강한 색보정/흑백/크롭/블러=피부보정 근사)을 실제 원본 사진에 적용해
   편집 유형별 매칭 안정성을 측정(운영에 실제로 존재하는 편집 다양성이 부족할 수 있어 보강).
"""
import asyncio
import io
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from dotenv import load_dotenv
from PIL import Image, ImageOps, ImageFilter, ImageEnhance

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv()

from supabase import create_client  # noqa: E402
from app.downloader import download_all  # noqa: E402
from app.gemini_matcher import compute_retouch_embeddings, get_or_compute_original_embeddings  # noqa: E402
from app.grouping import _cosine  # noqa: E402

supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])

PROJECTS = {
    "wedding": "d0df95ba-6a0d-4f74-8ac8-21f4e563cd81",   # 실제 웨딩, 원본 77장 / 보정본 13건
    "lomography": "23f28c4d-1449-432a-87db-0b0bca911521",  # 실제 로모그래피 스타일, 원본 8장(서로 매우 유사) / 보정본 6건
}


@dataclass
class ProjectSample:
    label: str
    photo_ids: list
    photo_filenames: dict
    photo_embeddings: dict = field(default_factory=dict)  # photo_id -> np.ndarray
    version_pairs: list = field(default_factory=list)  # (true_photo_id, version_filename, embedding)


async def load_project(label: str, project_id: str, max_originals: int = 20) -> ProjectSample:
    photos = (
        supabase.table("photos")
        .select("id, original_filename, r2_thumb_url, number")
        .eq("project_id", project_id)
        .order("number")
        .limit(max_originals)
        .execute()
        .data
    )
    photo_ids = [p["id"] for p in photos]
    photo_filenames = {p["id"]: p["original_filename"] for p in photos}

    versions = (
        supabase.table("photo_versions")
        .select("photo_id, version, filename, r2_thumb_url")
        .in_("photo_id", photo_ids)
        .execute()
        .data
    )

    print(f"[{label}] embedding {len(photos)} originals via gemini_matcher (cache-aware)...")
    photo_embeddings = await get_or_compute_original_embeddings(supabase, project_id, photo_ids)

    version_rows = [v for v in versions if v.get("r2_thumb_url")]
    print(f"[{label}] embedding {len(version_rows)} retouched versions...")
    version_images = await download_all([v["r2_thumb_url"] for v in version_rows])
    version_vecs = await compute_retouch_embeddings([img for img in version_images if img is not None])
    # download_all과 순서를 맞추기 위해 None 제외분을 재정렬
    vec_iter = iter(version_vecs)
    version_pairs = []
    for v, img in zip(version_rows, version_images):
        if img is None:
            continue
        vec = next(vec_iter)
        if vec is not None:
            version_pairs.append((v["photo_id"], v.get("filename") or f"v{v['version']}", vec))

    return ProjectSample(label, photo_ids, photo_filenames, photo_embeddings, version_pairs)


def rank_against_pool(query_vec: np.ndarray, pool: dict) -> list[tuple[str, float]]:
    """pool: {photo_id: vec} -> [(photo_id, sim), ...] 유사도 내림차순."""
    scored = [(pid, float(_cosine(query_vec, vec))) for pid, vec in pool.items()]
    scored.sort(key=lambda x: -x[1])
    return scored


def analyze_positive_pairs(sample: ProjectSample) -> list[dict]:
    results = []
    for true_pid, filename, vec in sample.version_pairs:
        ranked = rank_against_pool(vec, sample.photo_embeddings)
        if not ranked:
            continue
        true_rank = next((i for i, (pid, _) in enumerate(ranked) if pid == true_pid), None)
        true_sim = next((s for pid, s in ranked if pid == true_pid), None)
        top1_pid, top1_sim = ranked[0]
        top2_sim = ranked[1][1] if len(ranked) > 1 else None
        results.append(
            {
                "project": sample.label,
                "version_filename": filename,
                "true_photo_filename": sample.photo_filenames.get(true_pid),
                "true_sim": round(true_sim, 4) if true_sim is not None else None,
                "true_rank": true_rank,  # 0이면 top1이 정답(=매칭 성공)
                "top1_photo_filename": sample.photo_filenames.get(top1_pid),
                "top1_sim": round(top1_sim, 4),
                "top1_is_correct": top1_pid == true_pid,
                "top1_top2_margin": round(top1_sim - top2_sim, 4) if top2_sim is not None else None,
                "pool_size": len(ranked),
            }
        )
    return results


def analyze_burst_cross_similarity(sample: ProjectSample) -> dict:
    """같은 프로젝트 내 서로 다른 원본들 간 교차 유사도 분포 — 연속컷 오매칭 위험."""
    ids = list(sample.photo_embeddings.keys())
    sims = []
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            sims.append(float(_cosine(sample.photo_embeddings[ids[i]], sample.photo_embeddings[ids[j]])))
    if not sims:
        return {"project": sample.label, "pair_count": 0}
    arr = np.array(sims)
    return {
        "project": sample.label,
        "pair_count": len(sims),
        "mean": round(float(arr.mean()), 4),
        "p50": round(float(np.percentile(arr, 50)), 4),
        "p90": round(float(np.percentile(arr, 90)), 4),
        "p99": round(float(np.percentile(arr, 99)), 4),
        "max": round(float(arr.max()), 4),
    }


# ── 합성 편집 (실 운영 데이터의 편집 다양성 보강) ──────────────────────────────

def apply_strong_color_grade(img: Image.Image) -> Image.Image:
    img = ImageEnhance.Color(img).enhance(1.8)
    img = ImageEnhance.Contrast(img).enhance(1.3)
    r, g, b = img.convert("RGB").split()
    r = r.point(lambda x: min(255, int(x * 1.15)))
    b = b.point(lambda x: max(0, int(x * 0.85)))
    return Image.merge("RGB", (r, g, b))


def apply_grayscale(img: Image.Image) -> Image.Image:
    return ImageOps.grayscale(img).convert("RGB")


def apply_crop(img: Image.Image) -> Image.Image:
    w, h = img.size
    return img.crop((int(w * 0.15), int(h * 0.15), int(w * 0.85), int(h * 0.85)))


def apply_skin_smooth_approx(img: Image.Image) -> Image.Image:
    # 실제 피부보정 알고리즘은 아니지만 "부드러운 블러 + 밝기up"으로 근사
    img = img.filter(ImageFilter.GaussianBlur(radius=3))
    return ImageEnhance.Brightness(img).enhance(1.08)


SYNTHETIC_EDITS = {
    "strong_color_grade": apply_strong_color_grade,
    "grayscale": apply_grayscale,
    "crop": apply_crop,
    "skin_smooth_approx": apply_skin_smooth_approx,
}


async def analyze_synthetic_edits(sample: ProjectSample, n_photos: int = 5) -> list[dict]:
    ids = list(sample.photo_embeddings.keys())[:n_photos]
    urls = []
    for pid in ids:
        row = supabase.table("photos").select("r2_thumb_url").eq("id", pid).maybe_single().execute()
        urls.append(row.data["r2_thumb_url"])
    raw_images = await download_all(urls)

    edit_bytes: list[tuple[str, str, bytes]] = []  # (photo_id, edit_name, jpeg_bytes)
    for pid, raw in zip(ids, raw_images):
        if raw is None:
            continue
        base = Image.open(io.BytesIO(raw)).convert("RGB")
        for edit_name, fn in SYNTHETIC_EDITS.items():
            edited = fn(base)
            buf = io.BytesIO()
            edited.save(buf, format="JPEG", quality=90)
            edit_bytes.append((pid, edit_name, buf.getvalue()))

    print(f"[{sample.label}] embedding {len(edit_bytes)} synthetic-edit images...")
    vecs = await compute_retouch_embeddings([b for _, _, b in edit_bytes])

    results = []
    for (true_pid, edit_name, _), vec in zip(edit_bytes, vecs):
        if vec is None:
            continue
        ranked = rank_against_pool(vec, sample.photo_embeddings)
        true_rank = next((i for i, (pid, _) in enumerate(ranked) if pid == true_pid), None)
        true_sim = next((s for pid, s in ranked if pid == true_pid), None)
        top1_pid, top1_sim = ranked[0]
        top2_sim = ranked[1][1] if len(ranked) > 1 else None
        results.append(
            {
                "project": sample.label,
                "edit_type": edit_name,
                "true_photo_filename": sample.photo_filenames.get(true_pid),
                "true_sim": round(true_sim, 4) if true_sim is not None else None,
                "true_rank": true_rank,
                "top1_is_correct": top1_pid == true_pid,
                "top1_sim": round(top1_sim, 4),
                "top1_top2_margin": round(top1_sim - top2_sim, 4) if top2_sim is not None else None,
            }
        )
    return results


async def main():
    samples = {}
    for label, pid in PROJECTS.items():
        samples[label] = await load_project(label, pid)

    report = {"positive_pairs": [], "burst_cross_similarity": [], "synthetic_edits": []}

    for sample in samples.values():
        report["positive_pairs"].extend(analyze_positive_pairs(sample))
        report["burst_cross_similarity"].append(analyze_burst_cross_similarity(sample))

    # 합성 편집은 wedding 프로젝트(사진 다양성 큼)에서만 수행
    report["synthetic_edits"] = await analyze_synthetic_edits(samples["wedding"], n_photos=5)

    out_path = os.path.join(os.path.dirname(__file__), "calibration_report.json")
    with open(out_path, "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
