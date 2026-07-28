-- Gemini Flash 사진 품질 판정 POC를 위한 스키마 변경.
-- migration.sql / migration_002_quality_flags.sql / migration_003_gemini_poc.sql과 동일하게
-- Supabase SQL 에디터에서 한 번 수동 실행한다.
--
-- 기존 OpenCLIP(photos.blur_variance/is_blurry/face_detected/eyes_closed, photo_groups)과
-- Gemini Embedding POC(gemini_analysis_runs, gemini_embeddings)는 전혀 건드리지 않는다.
-- 완전히 새로운 테이블 2개만 추가하므로 기존 쿼리(SELECT * 포함)에 영향을 주지 않는다.

-- 실행(run) 단위 메타데이터 — Gemini Embedding의 gemini_analysis_runs와 동일한 패턴이되
-- 완전히 독립된 상태 머신(embedding 분석과 별도로 트리거/취소/재사용된다).
CREATE TABLE IF NOT EXISTS gemini_quality_runs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  status TEXT NOT NULL CHECK (status IN ('processing', 'completed', 'failed')),
  requested_image_limit INT,
  model TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  image_count INT NOT NULL DEFAULT 0,
  processed_count INT NOT NULL DEFAULT 0,
  failed_count INT NOT NULL DEFAULT 0,
  reused_count INT NOT NULL DEFAULT 0,
  estimated_cost_usd NUMERIC(10,6),
  usage_metadata JSONB,
  error TEXT,
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at TIMESTAMPTZ,
  duration_ms INT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_gemini_quality_runs_project_id ON gemini_quality_runs(project_id);

-- 사진별 Gemini Flash 품질 판정 결과. photos 테이블과 완전히 분리된 저장소.
-- UNIQUE(project_id, photo_id, model, prompt_version) — 모델/프롬프트 버전이 바뀌면 새 행으로
-- 쌓여 기존 버전과 나란히 비교할 수 있고, 동일 버전 재요청은 자동으로 재사용(스킵)된다.
CREATE TABLE IF NOT EXISTS gemini_quality_assessments (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  photo_id UUID NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
  model TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  eyes_closed TEXT NOT NULL CHECK (eyes_closed IN ('ok', 'possible', 'likely', 'unknown')),
  blur_or_shake TEXT NOT NULL CHECK (blur_or_shake IN ('ok', 'possible', 'likely', 'unknown')),
  focus_issue TEXT NOT NULL CHECK (focus_issue IN ('ok', 'possible', 'likely', 'unknown')),
  face_occluded TEXT NOT NULL CHECK (face_occluded IN ('ok', 'possible', 'likely', 'unknown')),
  primary_subject_detected BOOLEAN,
  notes TEXT,
  raw_response JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (project_id, photo_id, model, prompt_version)
);

CREATE INDEX IF NOT EXISTS idx_gemini_quality_assessments_project_id ON gemini_quality_assessments(project_id);
