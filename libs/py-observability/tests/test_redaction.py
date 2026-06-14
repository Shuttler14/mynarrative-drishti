from drishti_observability.logging import _redactor


def test_secrets_redacted():
    out = _redactor(None, None, {"password": "hunter2", "otp": "123456",
                                 "authorization": "Bearer x", "user": "ok"})
    assert out["password"] == "***redacted***"
    assert out["otp"] == "***redacted***"
    assert out["authorization"] == "***redacted***"
    assert out["user"] == "ok"


def test_nested_secret_key_names():
    out = _redactor(None, None, {"jwt_secret": "x", "card_number": "4111"})
    assert out["jwt_secret"] == "***redacted***"
    assert out["card_number"] == "***redacted***"


def test_non_secret_keys_untouched():
    out = _redactor(None, None, {"status": "ok", "count": 5, "name": "test"})
    assert out["status"] == "ok"
    assert out["count"] == 5
    assert out["name"] == "test"
