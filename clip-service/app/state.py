"""프로세스 내 in-flight 프로젝트 추적 (단일 replica 가정의 메모리 가드).

DB의 projects.clip_analysis_status='processing' 컬럼이 영속적 가드 역할을 하고,
이 메모리 set은 같은 프로세스 내에서의 빠른 중복 요청을 즉시 막기 위한 보조 가드다.
"""
import threading

_in_flight: set[str] = set()
_cancel_requested: set[str] = set()
_lock = threading.Lock()


def try_start(project_id: str) -> bool:
    with _lock:
        if project_id in _in_flight:
            return False
        _in_flight.add(project_id)
        _cancel_requested.discard(project_id)
        return True


def finish(project_id: str) -> None:
    with _lock:
        _in_flight.discard(project_id)


def is_in_flight(project_id: str) -> bool:
    with _lock:
        return project_id in _in_flight


def request_cancel(project_id: str) -> None:
    with _lock:
        _cancel_requested.add(project_id)


def is_cancel_requested(project_id: str) -> bool:
    with _lock:
        return project_id in _cancel_requested


def clear_cancel(project_id: str) -> None:
    with _lock:
        _cancel_requested.discard(project_id)
