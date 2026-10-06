"""A bad Discord value in .env must switch the buttons off, never crash the dashboard or scheduler."""
from src.config import settings as cfg


def test_valid_ids():
    assert cfg._discord_ids("X", "111, 222") == (111, 222)
    assert cfg._discord_ids("X", "") == () and cfg._discord_ids("X", None) == ()


def test_url_pasted_by_mistake_is_ignored_with_warning(capsys):
    assert cfg._discord_ids("DISCORD_APPROVER_IDS", "https://discord.com/oauth2/authorize?client_id=1&scope=bot") == ()
    assert "must be plain numbers" in capsys.readouterr().out


def test_get_settings_survives_bad_values(monkeypatch):
    monkeypatch.setenv("DISCORD_APPROVER_IDS", "https://discord.com/x")
    monkeypatch.setenv("DISCORD_CHANNEL_ID", "oops")
    get = cfg.get_settings
    getattr(get, "cache_clear", lambda: None)()
    s = get()
    assert s.discord_approver_ids == () and s.discord_channel_id == 0
    getattr(get, "cache_clear", lambda: None)()
