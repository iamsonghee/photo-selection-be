# photo-selection-be

FastAPI 백엔드 (사진 업로드, Supabase·R2).

## 문서

**모노레포 루트의 [README.md](../README.md)**에 다음이 정리되어 있습니다.

- 업로드 API 동작·EXIF 보정
- 동시 처리·스레드 풀 환경 변수 (`UPLOAD_PHOTOS_CONCURRENCY`, `VERSION_UPLOAD_CONCURRENCY`, `IMAGE_EXECUTOR_MAX_WORKERS`)

## 로컬 실행

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --reload
```

- `GET /health` — 헬스체크

`.env`에 Supabase·JWT·R2 등을 설정하세요.
