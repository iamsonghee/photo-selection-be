"""공개 주소를 우리 도메인으로 바꾼 뒤에도 DB에 남은 예전 r2.dev 주소의 key를 읽을 수 있어야 한다."""
import pytest

from app import storage


def test_new_and_legacy_public_hosts_are_both_allowed(monkeypatch):
    monkeypatch.setattr(storage, "R2_PUBLIC_URL", "https://img.acut.kr")
    monkeypatch.setattr(storage, "R2_LEGACY_PUBLIC_URLS", "https://pub-abc.r2.dev, ")
    assert storage.r2_key_from_url("https://img.acut.kr/photos/a%20b.jpg") == "photos/a b.jpg"
    assert storage.r2_key_from_url("https://pub-abc.r2.dev/photos/a.jpg") == "photos/a.jpg"
    with pytest.raises(ValueError):
        storage.r2_key_from_url("https://evil.example/photos/a.jpg")


def test_single_host_keeps_working_without_legacy(monkeypatch):
    monkeypatch.setattr(storage, "R2_PUBLIC_URL", "https://pub-abc.r2.dev")
    monkeypatch.setattr(storage, "R2_LEGACY_PUBLIC_URLS", "")
    assert storage.r2_key_from_url("https://pub-abc.r2.dev/x.jpg") == "x.jpg"
    with pytest.raises(ValueError):
        storage.r2_key_from_url("https://img.acut.kr/x.jpg")
