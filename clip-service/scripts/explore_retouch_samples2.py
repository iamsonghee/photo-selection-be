"""후보 프로젝트들의 원본 썸네일 URL을 실제로 눈으로 확인하기 위해 목록만 출력 (다운로드 없음)."""
import os
import sys
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv()
from supabase import create_client

supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])

CANDIDATE_PROJECT_IDS = [
    "d0df95ba-6a0d-4f74-8ac8-21f4e563cd81",  # '테'
    "ed19283b-c49a-43f1-bcf2-4b1df5b226cb",  # '한아름 프리랜서'
    "23f28c4d-1449-432a-87db-0b0bca911521",  # '그시절 로모그래피'
    "2d9bb58e-f311-4d7d-b1bd-6d7186612cdd",  # '민정홈스냅'
]

for pid in CANDIDATE_PROJECT_IDS:
    proj = supabase.table("projects").select("id, name, photo_count, status").eq("id", pid).maybe_single().execute()
    photos = supabase.table("photos").select("id, original_filename, r2_thumb_url").eq("project_id", pid).execute()
    versions = (
        supabase.table("photo_versions")
        .select("photo_id, version, filename, r2_thumb_url")
        .in_("photo_id", [p["id"] for p in photos.data])
        .execute()
    )
    print(f"\n=== {proj.data['name']!r} ({pid}) photo_count={proj.data['photo_count']} status={proj.data['status']} ===")
    print(f"  photos in project: {len(photos.data)}, versions: {len(versions.data)}")
    for p in photos.data[:3]:
        print(f"  original: {p['original_filename']} -> {p['r2_thumb_url']}")
    for v in versions.data[:3]:
        print(f"  version:  {v['filename']} (v{v['version']}) -> {v['r2_thumb_url']}")
