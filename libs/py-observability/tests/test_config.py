from drishti_observability.config import ObsSettings


def test_defaults():
    s = ObsSettings()
    assert s.env == "local"
    assert s.log_level == "INFO"
    assert s.sentry_dsn == ""
    assert s.otel_endpoint == ""
    assert s.service_version == "dev"


def test_load_from_env(monkeypatch):
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("SENTRY_DSN", "https://key@sentry.io/1")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel:4317")
    monkeypatch.setenv("SERVICE_VERSION", "abc123")
    s = ObsSettings.load()
    assert s.env == "production"
    assert s.log_level == "DEBUG"
    assert s.sentry_dsn == "https://key@sentry.io/1"
    assert s.otel_endpoint == "http://otel:4317"
    assert s.service_version == "abc123"
    assert s.log_json is True


def test_local_json_disabled(monkeypatch):
    monkeypatch.setenv("ENV", "local")
    s = ObsSettings.load()
    assert s.log_json is False
