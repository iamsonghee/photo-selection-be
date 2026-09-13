# A-CUT — Codex Agent Instructions (backend)

이 저장소(`photo-selection-be`, FastAPI)는 프론트엔드 저장소 `photo-selection-fe`(Next.js)와 같은 상위 폴더 아래 sibling 디렉터리로 함께 체크아웃되어 있다는 전제로 운영된다.

공통 문서가 존재하면 반드시 읽고 따른다.

공통 문서에 접근할 수 없는 경우에도 최소한:
- 실제 코드를 Source of Truth로 한다.
- API/DB/storage/worker/주요 flow 변경 시 관련 문서 영향을 확인한다.
- 미사용/legacy 코드를 임의 삭제하지 않는다.
- 작업 완료 시 Documentation impact를 보고한다.

이 저장소에서 작업하기 전에:

1. **`../photo-selection-fe/docs/agent-guidelines.md`를 읽는다.** Claude Code와 Codex가 공통으로 따르는 프로젝트 규칙의 Source of Truth이며, 여기서는 요약·복제하지 않는다. (sibling 저장소가 로컬에 없다면 그 사실을 먼저 알리고, 접근 가능한 범위 안에서만 판단한다.)
2. 그 문서의 공통 규칙을 이 저장소의 프로젝트 지침으로 그대로 따른다.
3. 구현 변경이 공통 규칙의 "Documentation sync" 기준에 해당하면, `../photo-selection-fe/docs/architecture.md` / `upload-flow.md` / `user-flow.md` 등 관련 문서(FE 저장소에 위치)를 코드 변경과 **같은 작업 안에서** 함께 최신화한다. 문서가 다른 저장소에 있다는 이유로 갱신을 건너뛰지 않는다.
4. 작업 완료 전에 문서 영향 여부를 확인하고, `agent-guidelines.md`의 "Documentation impact check" 형식으로 결과를 보고한다.

## 저장소 구조 (Codex 전용 참고)

- 이 저장소는 프론트엔드 저장소 `../photo-selection-fe`와 **별도 git 저장소**다. 두 저장소를 하나의 서비스로 함께 조사하되, 커밋은 각 저장소 기준으로 분리한다(이 저장소 커밋에 프론트엔드 변경을 함께 넣지 않는다).
- 프론트엔드 저장소에는 별도의 `AGENTS.md`(Codex)와 `CLAUDE.md`(Claude Code)가 있다. 세 파일 모두 공통 규칙은 `photo-selection-fe/docs/agent-guidelines.md` 하나만 참조한다.

