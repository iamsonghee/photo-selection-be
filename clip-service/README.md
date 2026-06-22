# clip-service

CLIP 임베딩 기반 유사컷(burst shot) 그룹핑 분석 서비스. `photo-selection-be`의 기존 `app/` 패키지와 완전히 독립적이며, Railway에 별도 서비스로 배포한다.

## 로컬 실행

```bash
cd clip-service
python -m venv .venv && source .venv/bin/activate
pip install --index-url https://download.pytorch.org/whl/cpu torch
pip install -r requirements.txt
cp .env.example .env  # 값 채우기
uvicorn app.main:app --reload --port 8001
```

## API

- `GET /health` — 헬스체크
- `POST /analyze` (헤더 `X-Internal-Token` 필요) — body `{"project_id": "..."}`, 202 응답 후 백그라운드로 분석 실행
- `GET /analyze/{project_id}/status` (헤더 `X-Internal-Token` 필요) — 분석 상태 조회

## Railway 배포

1. 같은 GitHub repo(`photo-selection-be`)를 연결한 신규 Railway 서비스 생성
2. Settings → Root Directory를 `clip-service`로 지정 (이 폴더의 Dockerfile로 빌드됨)
3. 환경변수 설정: `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `CLIP_INTERNAL_TOKEN`(FE와 동일 값 공유), 그 외 `.env.example` 참고
4. DB 마이그레이션(`../supabase/migrations/2026xxxx_clip_similarity_grouping.sql`)을 Supabase SQL 에디터에서 먼저 실행해야 한다.

## 알고리즘

`photos.number` 순서로 정렬 후 인접한 두 사진의 코사인 유사도가 `CLIP_SIMILARITY_THRESHOLD`(기본 0.92) 이상이면 같은 그룹으로 묶는다. burst shot은 항상 연속된 number로 업로드되므로 전체 N×N 비교 없이 인접 비교만으로 충분하다.
