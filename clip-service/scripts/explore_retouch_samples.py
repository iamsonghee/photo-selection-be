"""일회성 read-only 조사 — 보정본 매칭 Gemini 임계값 실측을 위한 샘플 후보 탐색.
운영 DB에 어떤 값도 쓰지 않는다."""
import os
import sys
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv()
from supabase import create_client

supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])

# photo_versions이 5건 이상 있는 프로젝트를 찾는다 (다양성 확보를 위해 사진 수 많은 프로젝트 우선)
versions = (
    supabase.table("photo_versions")
    .select("id, photo_id, version, r2_url, r2_thumb_url, filename, created_at")
    .order("created_at", desc=True)
    .limit(500)
    .execute()
)
print(f"total photo_versions rows fetched: {len(versions.data)}")

photo_ids = list({v["photo_id"] for v in versions.data})
photos = (
    supabase.table("photos")
    .select("id, project_id, r2_thumb_url, original_filename, clip_embedding")
    .in_("id", photo_ids)
    .execute()
)
photo_by_id = {p["id"]: p for p in photos.data}

by_project: dict[str, list] = {}
for v in versions.data:
    p = photo_by_id.get(v["photo_id"])
    if not p:
        continue
    by_project.setdefault(p["project_id"], []).append((v, p))

# 사진 수 많은 프로젝트 상위 10개 출력
ranked = sorted(by_project.items(), key=lambda kv: -len(kv[1]))
for pid, pairs in ranked[:10]:
    proj = supabase.table("projects").select("id, name, photo_count").eq("id", pid).maybe_single().execute()
    proj_name = proj.data["name"] if proj.data else "?"
    print(f"project={pid} name={proj_name!r} version_count={len(pairs)}")

# 상위 프로젝트 하나 상세 출력 (원본/보정본 URL 존재 여부 확인용)
if ranked:
    top_pid, top_pairs = ranked[0]
    print("\n--- sample pairs from top project ---")
    for v, p in top_pairs[:5]:
        print(
            {
                "photo_id": p["id"],
                "original_thumb": p["r2_thumb_url"],
                "has_clip_embedding": p["clip_embedding"] is not None,
                "version_url": v["r2_url"],
                "version_filename": v["filename"],
            }
        )
