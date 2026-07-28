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

## API (OpenCLIP)

- `GET /health` — 헬스체크
- `POST /analyze` (헤더 `X-Internal-Token` 필요) — body `{"project_id": "..."}`, 202 응답 후 백그라운드로 분석 실행
- `GET /analyze/{project_id}/status` (헤더 `X-Internal-Token` 필요) — 분석 상태 조회

## API (Gemini Embedding POC)

OpenCLIP 파이프라인과 완전히 독립된 실험적 기능. 결과 비교 목적이며 운영 그룹핑에는 영향을 주지 않는다.

- `POST /analyze/gemini` (헤더 `X-Internal-Token` 필요) — body `{"project_id": "...", "limit"?: number, "force"?: boolean}`. `limit`은 number 순 앞 N장만 분석(비용 통제용), `force`는 이미 저장된 임베딩도 재계산.
- `GET /analyze/gemini/{project_id}/status` (헤더 `X-Internal-Token` 필요) — 가장 최근 실행(run)의 상태/처리량/실패수/예상비용 조회
- `DELETE /analyze/gemini/{project_id}` (헤더 `X-Internal-Token` 필요) — 진행 중인 분석 취소
- `GET /analyze/gemini/{project_id}/groups?threshold=0.8` (헤더 `X-Internal-Token` 필요) — **Gemini API를 다시 호출하지 않고** 저장된 임베딩으로 그룹핑만 재계산(threshold 실험용)

## Railway 배포

1. 같은 GitHub repo(`photo-selection-be`)를 연결한 신규 Railway 서비스 생성
2. Settings → Root Directory를 `clip-service`로 지정 (이 폴더의 Dockerfile로 빌드됨)
3. 환경변수 설정: `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `CLIP_INTERNAL_TOKEN`(FE와 동일 값 공유), 그 외 `.env.example` 참고. Gemini POC를 쓰려면 `GEMINI_API_KEY` 추가 설정 필요(미설정 시 OpenCLIP 기능에는 영향 없이 Gemini 쪽만 실패).
4. DB 마이그레이션(`migration.sql`, `migration_002_quality_flags.sql`, `migration_003_gemini_poc.sql`)을 Supabase SQL 에디터에서 순서대로 실행해야 한다.

## 알고리즘

`photos.number` 순서로 정렬 후 인접한 두 사진의 코사인 유사도가 `CLIP_SIMILARITY_THRESHOLD`(기본 0.92) 이상이면 같은 그룹으로 묶는다. burst shot은 항상 연속된 number로 업로드되므로 전체 N×N 비교 없이 인접 비교만으로 충분하다. Gemini POC도 동일한 그룹핑 알고리즘(`grouping.py`)을 재사용하되 임베딩과 threshold(`GEMINI_SIMILARITY_THRESHOLD`, 기본 0.80)만 다르다 — 둘의 점수 분포가 다르므로 OpenCLIP의 threshold를 그대로 쓰지 않는다.
