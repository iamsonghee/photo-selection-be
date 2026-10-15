# clip-service

CLIP 임베딩 기반 유사컷(burst shot) 그룹핑 분석 서비스. `photo-selection-be`의 기존 `app/` 패키지와 완전히 독립적이며, Railway에 별도 서비스로 배포한다.

## 로컬 실행

로컬 FE는 로컬 clip-service를 본다(FE `.env.local`의 `CLIP_SERVICE_URL=http://localhost:8001`, FE `.env.example` 참고).
배포된 clip-service를 보면 아직 배포하지 않은 clip-service 변경이 테스트되지 않는다 — 오류 없이 예전 동작이 나온다.
로컬 clip-service가 꺼져 있으면 FE의 AI 정리 시작·작가 Gemini 분석·보정본 매칭이 실패하고, 사진 삭제 뒤 유사컷 묶음 동기화를 건너뛴다.

```bash
cd clip-service
python3.11 -m venv .venv
.venv/bin/pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision
.venv/bin/pip install -r requirements.txt
cp .env.example .env  # 값 채우기 — 아래 표
.venv/bin/uvicorn app.main:app --port 8001
```

- Python 3.11(배포 Dockerfile과 같음). 시작할 때 `app.main`이 torch·opencv를 불러오므로 AI 정리만 쓸 때도 전체 설치가 필요하다(약 1.6GB, macOS arm64 휠 있음).
- **`--reload`를 쓰지 않는다.** AI 정리 실행은 이 프로세스 안에서 백그라운드로 수십 분 돈다. 재시작되면 끊기고 10분 뒤 실패로 처리된다.
  대신 clip-service 코드를 바꾸거나 브랜치를 옮기면 직접 재시작해야 그 코드가 돈다.
- 시작할 때 도는 작업은 없다. FE에서 요청이 올 때만 일한다. 다만 로컬 `.env`는 운영 Supabase를 가리키고 Gemini 키는 유료이므로,
  로컬 실행도 운영 DB에 쓰고 비용이 든다. 테스트는 전용 테스트 프로젝트에서 하고, 같은 프로젝트를 다른 판정 버전(배포본·다른 브랜치)으로
  번갈아 돌리지 않는다 — 판정 버전이 바뀔 때마다 전체 사진을 다시 판정한다.

| `.env` 키 | 필수 | 값 |
|---|---|---|
| `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` | 예 | BE `.env`와 같은 값 |
| `CLIP_INTERNAL_TOKEN` | 예 | 그 사람 FE `.env.local`의 `CLIP_INTERNAL_TOKEN`과 같은 값(운영 값이 아니어도 됨) |
| `GEMINI_API_KEY` | AI 기능 쓸 때 | Gemini 키 |
| `GEMINI_CUSTOMER_QUALITY_SERVICE_TIER` | 아니오 | 셀프 고객 흔들림 판정 티어. 없으면 `standard`. `flex`는 절반 가격·지연 가변 |

확인: `curl http://localhost:8001/health` → `{"status":"ok"}`. 포트 8001은 로컬 clip-service용이다(BE는 8000, FE는 3001).

## API (OpenCLIP)

- `GET /health` — 헬스체크
- `POST /analyze` (헤더 `X-Internal-Token` 필요) — body `{"project_id": "..."}`, 202 응답 후 백그라운드로 분석 실행
- `GET /analyze/{project_id}/status` (헤더 `X-Internal-Token` 필요) — 분석 상태 조회

## API (Gemini Embedding POC)

OpenCLIP 파이프라인과 완전히 독립된 실험적 기능. 결과 비교 목적이며 운영 그룹핑에는 영향을 주지 않는다.

- `POST /analyze/gemini` (헤더 `X-Internal-Token` 필요) — body `{"project_id": "...", "limit"?: number, "force"?: boolean}`. `limit`은 number 순 앞 N장만 분석(비용 통제용), `force`는 이미 저장된 임베딩도 재계산.
- `GET /analyze/gemini/{project_id}/status` (헤더 `X-Internal-Token` 필요) — 가장 최근 실행(run)의 상태/처리량/실패수/예상비용 조회
- `DELETE /analyze/gemini/{project_id}` (헤더 `X-Internal-Token` 필요) — 진행 중인 분석 취소
- `GET /analyze/gemini/{project_id}/groups?threshold=0.96` (헤더 `X-Internal-Token` 필요) — **Gemini API를 다시 호출하지 않고** 저장된 임베딩으로 그룹핑만 재계산(threshold 실험용). Gemini Flash 품질 분석 결과가 있으면 `quality_by_photo`/`recommended_photo_id`로 함께 응답한다(아래 참고).

## API (Gemini Flash 품질 판정 POC)

Gemini Embedding과도 완전히 독립된 실험적 기능. 사진별로 눈감음/흔들림/초점/얼굴판정 의심 여부를 4단계(ok/possible/likely/unknown)로 판정하고, 위 `/groups` 응답에 결합해 품질 이슈가 적으면서 그룹 대표성이 높은 사진을 `recommended_photo_id`로 추천한다. 자동 삭제·숨김 기능이 아니라 작가의 1차 검토를 돕는 보조 정보다.

- `POST /analyze/gemini/quality` (헤더 `X-Internal-Token` 필요) — body `{"project_id": "...", "limit"?: number, "force"?: boolean}`. 같은 모델+프롬프트 버전으로 이미 판정된 사진은 자동 재사용(재호출 없음), `force`는 강제 재판정.
- `GET /analyze/gemini/quality/{project_id}/status` (헤더 `X-Internal-Token` 필요) — 가장 최근 실행의 상태/처리량/실패수/재사용수/실사용 토큰 기반 예상비용 조회
- `DELETE /analyze/gemini/quality/{project_id}` (헤더 `X-Internal-Token` 필요) — 진행 중인 분석 취소

## Railway 배포

1. 같은 GitHub repo(`photo-selection-be`)를 연결한 신규 Railway 서비스 생성
2. Settings → Root Directory를 `clip-service`로 지정 (이 폴더의 Dockerfile로 빌드됨)
3. 환경변수 설정: `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `CLIP_INTERNAL_TOKEN`(FE와 동일 값 공유), 그 외 `.env.example` 참고. Gemini POC를 쓰려면 `GEMINI_API_KEY` 추가 설정 필요(미설정 시 OpenCLIP 기능에는 영향 없이 Gemini 쪽만 실패). Embedding과 Flash 품질 판정 모두 같은 키를 공유한다.
4. DB 마이그레이션(`migration.sql`, `migration_002_quality_flags.sql`, `migration_003_gemini_poc.sql`, `migration_004_gemini_quality_poc.sql`)을 Supabase SQL 에디터에서 순서대로 실행해야 한다.

## 알고리즘

`photos.number` 순서로 정렬 후 인접한 두 사진의 코사인 유사도가 `CLIP_SIMILARITY_THRESHOLD`(기본 0.96) 이상이면 같은 그룹으로 묶는다. burst shot은 항상 연속된 number로 업로드되므로 전체 N×N 비교 없이 인접 비교만으로 충분하다. Gemini Embedding POC도 동일한 그룹핑 알고리즘(`grouping.py`)을 재사용하되 임베딩과 threshold(`GEMINI_SIMILARITY_THRESHOLD`, 기본 0.96)만 다르다 — 둘의 점수 분포가 다르므로 OpenCLIP의 threshold를 그대로 쓰지 않는다. 그룹 대표 이미지는 medoid(그룹 내 다른 이미지들과 평균 유사도가 가장 높은 실제 이미지)로 선정한다. Gemini Flash 품질 판정이 있으면 이 medoid 대표 이미지와 별개로, 품질 이슈가 적은 사진을 우선하는 단계형(tiered) 로직으로 `recommended_photo_id`를 다시 계산한다(`gemini_analyzer.py`의 `_recommend_with_quality`) — 품질 분석이 없으면 medoid와 동일하게 나온다.
