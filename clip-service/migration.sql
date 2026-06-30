-- clip-service 도입을 위한 DB 스키마 변경.
-- 이 저장소는 ORM/마이그레이션 파일을 쓰지 않으므로(기존 스키마는 Supabase 대시보드에서 직접 관리),
-- 이 SQL을 Supabase 프로젝트의 SQL 에디터에서 한 번 실행한다.
-- 전부 신규 테이블/컬럼 추가이므로 기존 쿼리(SELECT * 포함)에 영향을 주지 않는다.

CREATE TABLE IF NOT EXISTS photo_groups (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  representative_photo_id UUID REFERENCES photos(id) ON DELETE SET NULL,
  photo_count INT NOT NULL DEFAULT 0,
  avg_similarity NUMERIC(5,4),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_photo_groups_project_id ON photo_groups(project_id);

ALTER TABLE photos
  ADD COLUMN IF NOT EXISTS similarity_group_id UUID REFERENCES photo_groups(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_photos_similarity_group_id ON photos(similarity_group_id);

ALTER TABLE projects
  ADD COLUMN IF NOT EXISTS clip_analysis_status TEXT
    CHECK (clip_analysis_status IN ('processing', 'completed', 'failed') OR clip_analysis_status IS NULL);

ALTER TABLE projects ADD COLUMN IF NOT EXISTS clip_analysis_started_at TIMESTAMPTZ;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS clip_analysis_completed_at TIMESTAMPTZ;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS clip_analysis_error TEXT;

-- 보정본 CLIP 매칭을 위한 원본 임베딩 영속화 (pgvector 미설치 환경이므로 double precision[] 사용).
-- 유사도 검색용 인덱스 불필요 — project_id+id IN (...) 소규모 후보군 조회만 사용.
ALTER TABLE photos ADD COLUMN IF NOT EXISTS clip_embedding DOUBLE PRECISION[];
