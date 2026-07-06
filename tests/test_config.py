from app.config import Settings


def test_settings_loads_defaults():
    settings = Settings()
    assert settings.app_name == "Prompt2PBI"
    assert settings.api_port == 8000
    assert ".csv" in settings.allowed_extensions

