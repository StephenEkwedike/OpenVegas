from __future__ import annotations

from server.services import demo_admin


def test_empty_allowlist_not_admin_by_default(monkeypatch):
    monkeypatch.setenv("OPENVEGAS_DEMO_ALWAYS_WIN_ENABLED", "1")
    monkeypatch.setenv("OPENVEGAS_DEMO_ADMIN_USER_IDS", "")
    monkeypatch.setenv("OPENVEGAS_DEMO_ALLOW_LOCAL_OPEN", "0")
    assert demo_admin.is_demo_admin_user("u1") is False


def test_empty_allowlist_admin_when_local_open_enabled(monkeypatch):
    monkeypatch.setenv("OPENVEGAS_DEMO_ALWAYS_WIN_ENABLED", "1")
    monkeypatch.setenv("OPENVEGAS_DEMO_ADMIN_USER_IDS", "")
    monkeypatch.setenv("OPENVEGAS_DEMO_ALLOW_LOCAL_OPEN", "1")
    assert demo_admin.is_demo_admin_user("u1") is True


def test_explicit_allowlist_controls_access(monkeypatch):
    monkeypatch.setenv("OPENVEGAS_DEMO_ALWAYS_WIN_ENABLED", "1")
    monkeypatch.setenv("OPENVEGAS_DEMO_ADMIN_USER_IDS", "admin-a, admin-b")
    monkeypatch.setenv("OPENVEGAS_DEMO_ALLOW_LOCAL_OPEN", "0")
    assert demo_admin.is_demo_admin_user("admin-a") is True
    assert demo_admin.is_demo_admin_user("admin-b") is True
    assert demo_admin.is_demo_admin_user("random-user") is False
