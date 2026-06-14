from drishti_observability.sentry import _scrub


def test_auth_headers_scrubbed():
    event = {"request": {"headers": {"Authorization": "Bearer secret",
                                     "X-Shopify-Hmac-SHA256": "sig", "Accept": "json"}}}
    out = _scrub(event, None)
    h = out["request"]["headers"]
    assert h["Authorization"] == "***"
    assert h["X-Shopify-Hmac-SHA256"] == "***"
    assert h["Accept"] == "json"


def test_cookie_scrubbed():
    event = {"request": {"headers": {"Cookie": "session=abc123", "Host": "example.com"}}}
    out = _scrub(event, None)
    h = out["request"]["headers"]
    assert h["Cookie"] == "***"
    assert h["Host"] == "example.com"


def test_non_auth_headers_preserved():
    event = {"request": {"headers": {"Content-Type": "application/json", "Accept": "text/html"}}}
    out = _scrub(event, None)
    assert out["request"]["headers"]["Content-Type"] == "application/json"
    assert out["request"]["headers"]["Accept"] == "text/html"
