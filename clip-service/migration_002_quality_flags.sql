-- 흔들림(블러)/눈감음 AI 경고 배지 기능을 위한 스키마 변경.
-- migration.sql과 동일하게 Supabase SQL 에디터에서 한 번 수동 실행한다.
-- 전부 신규 컬럼 추가이므로 기존 쿼리(SELECT * 포함)에 영향을 주지 않는다.
-- 정보성 배지 전용 컬럼 — 이 값들을 근거로 사진을 자동 삭제/제외하지 않는다.

-- 300px 썸네일(r2_thumb_url) 기준 raw Laplacian 분산. NULL = 미분석 또는 디코딩 실패.
-- 원값을 별도 보관해 is_blurry 임계값을 나중에 재조정할 때 사진 재다운로드 없이 재계산 가능하게 한다.
ALTER TABLE photos ADD COLUMN IF NOT EXISTS blur_variance DOUBLE PRECISION;

-- blur_variance를 BLUR_VARIANCE_THRESHOLD와 비교해 파생된 플래그. NULL = 미분석.
ALTER TABLE photos ADD COLUMN IF NOT EXISTS is_blurry BOOLEAN;

-- 얼굴 검출 여부. NULL = 미분석. FALSE = 분석했지만 얼굴 없음(풍경/사물 사진 등) — eyes_closed는 이 경우 의미 없음.
ALTER TABLE photos ADD COLUMN IF NOT EXISTS face_detected BOOLEAN;

-- 눈감음 의심 여부. face_detected = TRUE일 때만 유효한 값.
ALTER TABLE photos ADD COLUMN IF NOT EXISTS eyes_closed BOOLEAN;
