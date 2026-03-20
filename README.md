# photo-selection-be

FastAPI 백엔드 프로젝트

## 설정

1. `.env` 파일에 Supabase 및 JWT 관련 키를 입력하세요.
2. 가상환경 생성 및 의존성 설치:
   ```bash
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```
3. 서버 실행: `.venv/bin/uvicorn app.main:app --reload`

## API

- `GET /health` - 헬스체크
