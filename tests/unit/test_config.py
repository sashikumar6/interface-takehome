import pytest

from computer_use.config import Settings


def test_blank_provider_keys_are_treated_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("OPENAI_API_KEY", "unit-test-key")
    monkeypatch.setenv("COMPUTER_USE_PROVIDER", "openai")
    settings = Settings(_env_file=None)
    assert settings.anthropic_api_key is None
    assert settings.openai_api_key is not None
    assert settings.provider_key() == settings.openai_api_key
