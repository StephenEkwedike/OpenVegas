from __future__ import annotations

from server.services import demo_admin


def test_empty_allowlist_not_admin_by_default(monkeypatch):
    monkeypatch.delenv("OPENVEGAS_WIN_ALWAYS", raising=False)
    monkeypatch.delenv("OPENVEGAS_WIN_ALWAYS_USER_IDS", raising=False)
    monkeypatch.setenv("OPENVEGAS_DEMO_ALWAYS_WIN_ENABLED", "1")
    monkeypatch.setenv("OPENVEGAS_DEMO_ADMIN_USER_IDS", "")
    monkeypatch.setenv("OPENVEGAS_DEMO_ALLOW_LOCAL_OPEN", "0")
    assert demo_admin.is_demo_admin_user("u1") is False


def test_empty_allowlist_admin_when_local_open_enabled(monkeypatch):
    monkeypatch.delenv("OPENVEGAS_WIN_ALWAYS", raising=False)
    monkeypatch.delenv("OPENVEGAS_WIN_ALWAYS_USER_IDS", raising=False)
    monkeypatch.setenv("OPENVEGAS_DEMO_ALWAYS_WIN_ENABLED", "1")
    monkeypatch.setenv("OPENVEGAS_DEMO_ADMIN_USER_IDS", "")
    monkeypatch.setenv("OPENVEGAS_DEMO_ALLOW_LOCAL_OPEN", "1")
    assert demo_admin.is_demo_admin_user("u1") is True


def test_explicit_allowlist_controls_access(monkeypatch):
    monkeypatch.delenv("OPENVEGAS_WIN_ALWAYS", raising=False)
    monkeypatch.delenv("OPENVEGAS_WIN_ALWAYS_USER_IDS", raising=False)
    monkeypatch.setenv("OPENVEGAS_DEMO_ALWAYS_WIN_ENABLED", "1")
    monkeypatch.setenv("OPENVEGAS_DEMO_ADMIN_USER_IDS", "admin-a, admin-b")
    monkeypatch.setenv("OPENVEGAS_DEMO_ALLOW_LOCAL_OPEN", "0")
    assert demo_admin.is_demo_admin_user("admin-a") is True
    assert demo_admin.is_demo_admin_user("admin-b") is True
    assert demo_admin.is_demo_admin_user("random-user") is False


def test_new_win_always_alias_supports_y_and_per_user_allowlist(monkeypatch):
    monkeypatch.setenv("OPENVEGAS_WIN_ALWAYS", "Y")
    monkeypatch.setenv("OPENVEGAS_WIN_ALWAYS_USER_IDS", "qa-user, qa-content")
    monkeypatch.setenv("OPENVEGAS_DEMO_ALWAYS_WIN_ENABLED", "0")
    monkeypatch.setenv("OPENVEGAS_DEMO_ADMIN_USER_IDS", "")
    assert demo_admin.demo_mode_enabled() is True
    assert demo_admin.is_demo_admin_user("qa-user") is True
    assert demo_admin.is_demo_admin_user("qa-content") is True
    assert demo_admin.is_demo_admin_user("random-user") is False


def test_new_win_always_alias_takes_precedence_when_set_to_no(monkeypatch):
    monkeypatch.setenv("OPENVEGAS_WIN_ALWAYS", "N")
    monkeypatch.setenv("OPENVEGAS_WIN_ALWAYS_USER_IDS", "qa-user")
    monkeypatch.setenv("OPENVEGAS_DEMO_ALWAYS_WIN_ENABLED", "1")
    monkeypatch.setenv("OPENVEGAS_DEMO_ADMIN_USER_IDS", "qa-user")
    assert demo_admin.demo_mode_enabled() is False
    assert demo_admin.is_demo_admin_user("qa-user") is False


def test_new_win_always_single_switch_enables_all_accounts_without_allowlist(monkeypatch):
    monkeypatch.setenv("OPENVEGAS_WIN_ALWAYS", "Y")
    monkeypatch.delenv("OPENVEGAS_WIN_ALWAYS_USER_IDS", raising=False)
    monkeypatch.setenv("OPENVEGAS_DEMO_ALWAYS_WIN_ENABLED", "0")
    monkeypatch.setenv("OPENVEGAS_DEMO_ADMIN_USER_IDS", "")
    monkeypatch.setenv("OPENVEGAS_DEMO_ALLOW_LOCAL_OPEN", "0")
    assert demo_admin.demo_mode_enabled() is True
    assert demo_admin.is_demo_admin_user("random-user-a") is True
    assert demo_admin.is_demo_admin_user("random-user-b") is True
