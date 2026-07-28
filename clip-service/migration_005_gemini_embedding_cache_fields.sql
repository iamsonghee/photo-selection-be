-- Gemini Embedding 캐시 판별 정확도 개선 (베타 전환 작업의 일부).
-- migration_003_gemini_poc.sql 이후 Supabase SQL 에디터에서 한 번 수동 실행한다.
--
-- 배경: 기존 캐시 판별(get_existing_photo_ids)이 project_id+photo_id+embedding_model만 보고
-- dimension을 확인하지 않아, GEMINI_EMBEDDING_DIMENSION을 바꾸면 이전 차원의 임베딩을 잘못
-- "이미 분석됨"으로 재사용할 위험이 있었다. 이번에 dimension/embedding_version을 UNIQUE
-- 제약에도 포함시켜, 설정이 바뀌면 새 조합으로 취급되어 upsert 충돌 없이 별도 행이 생기고
-- (기존 행은 자연히 무시됨), 그룹 계산 시에도 서로 다른 차원의 벡터가 섞이지 않는다.
-- 기존 photo_groups/photos.similarity_group_id/photos.clip_embedding 등 OpenCLIP 스키마는
-- 전혀 건드리지 않는다.

-- 프롬프트/전처리 버전 관리 — GEMINI_QUALITY_PROMPT_VERSION과 동일 패턴.
-- 기존 행은 전부 'v1'으로 채워짐(지금까지의 암묵적 동작과 동일하므로 데이터 마이그레이션 불필요).
ALTER TABLE gemini_embeddings ADD COLUMN IF NOT EXISTS embedding_version TEXT NOT NULL DEFAULT 'v1';

-- r2_thumb_url에서 R2_PUBLIC_URL 접두사를 제거한 순수 R2 object key. URL 자체(도메인 구성)가
-- 아니라 실제 객체를 가리키는 안정적인 식별자로 캐시 판별에 방어적으로 사용한다.
-- 기존 행은 NULL로 남고(과거 분석 당시 key를 기록하지 않았음), 캐시 판별 시 NULL이면
-- "일치 확인 불가"로 보아 안전하게 미분석으로 취급한다(재분석 1회만 유발, 데이터 유실 없음).
ALTER TABLE gemini_embeddings ADD COLUMN IF NOT EXISTS source_object_key TEXT;

-- 기존 3-컬럼 UNIQUE를 5-컬럼으로 교체. 기본 명명 규칙(테이블_컬럼들_key)으로 자동 생성된
-- 제약 이름을 명시적으로 지정해 안전하게 교체한다.
ALTER TABLE gemini_embeddings
  DROP CONSTRAINT IF EXISTS gemini_embeddings_project_id_photo_id_embedding_model_key;

ALTER TABLE gemini_embeddings
  ADD CONSTRAINT gemini_embeddings_project_photo_model_dim_version_key
  UNIQUE (project_id, photo_id, embedding_model, dimension, embedding_version);
