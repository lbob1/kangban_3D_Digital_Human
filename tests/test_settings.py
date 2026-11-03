"""Settings 加载测试：.env / 环境变量；后续变量为空不影响 Day1。"""

from app.core.config import Settings, get_settings


def test_settings_loads_from_env(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test-env")
    monkeypatch.setenv("APP_PORT", "9999")
    monkeypatch.setattr("app.core.config.get_settings.cache_clear", lambda: None)
    settings = Settings()
    assert settings.app_env == "test-env"
    assert settings.app_port == 9999


def test_settings_defaults():
    settings = Settings()
    assert settings.app_env == "local"
    assert settings.app_timezone == "Asia/Shanghai"
    assert settings.audio_retention_hours == 72
    assert settings.database_url.startswith("postgresql+asyncpg://")


def test_future_optional_fields_empty_by_default():
    """后续能力变量为空/占位时，Settings 可正常构造且应用可启动。"""
    settings = Settings()
    assert settings.llm_api_key in (None, "")
    assert settings.jwt_secret_key in (None, "")
    assert settings.asr_provider in (None, "", "CHANGE_ME")
    assert settings.tts_provider in (None, "", "CHANGE_ME")


def test_get_settings_cached():
    assert get_settings() is get_settings()
