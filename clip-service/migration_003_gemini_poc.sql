-- Gemini Embedding 유사컷 그룹핑 POC를 위한 스키마 변경.
-- migration.sql / migration_002_quality_flags.sql과 동일하게 Supabase SQL 에디터에서 한 번 수동 실행한다.
--
-- 기존 OpenCLIP 관련 테이블/컬럼(photo_groups, photos.similarity_group_id, photos.clip_embedding,
-- projects.clip_analysis_*)은 전혀 건드리지 않는다. 완전히 새로운 테이블 2개만 추가하므로
-- 기존 쿼리(SELECT * 포함)에 영향을 주지 않는다.

-- 실행(run) 단위 메타데이터 — 진행 상태 폴링, 처리/실패 건수, 소요 시간, 예상 비용 확인용.
-- projects 테이블에는 컬럼을 추가하지 않는다(운영 스키마 비침습).
CREATE TABLE IF NOT EXISTS gemini_analysis_runs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  status TEXT NOT NULL CHECK (status IN ('processing', 'completed', 'failed')),
  requested_image_limit INT,
  embedding_model TEXT NOT NULL,
  embedding_dimension INT NOT NULL,
  similarity_threshold NUMERIC(5,4) NOT NULL,
  image_count INT NOT NULL DEFAULT 0,
  processed_count INT NOT NULL DEFAULT 0,
  failed_count INT NOT NULL DEFAULT 0,
  estimated_cost_usd NUMERIC(10,6),
  usage_metadata JSONB,
  error TEXT,
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at TIMESTAMPTZ,
  duration_ms INT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_gemini_analysis_runs_project_id ON gemini_analysis_runs(project_id);

-- 사진별 Gemini 임베딩. photos 테이블과는 완전히 분리된 저장소 —
-- threshold를 바꿔가며 재그룹핑할 때 Gemini API를 다시 호출하지 않기 위한 캐시 역할도 겸한다.
CREATE TABLE IF NOT EXISTS gemini_embeddings (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  photo_id UUID NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
  embedding_model TEXT NOT NULL,
  embedding DOUBLE PRECISION[] NOT NULL,
  dimension INT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (project_id, photo_id, embedding_model)
);

CREATE INDEX IF NOT EXISTS idx_gemini_embeddings_project_id ON gemini_embeddings(project_id);
