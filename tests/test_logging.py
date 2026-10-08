import logging

from app.main import RedactQuerySecrets


def _redact(path: str) -> str:
    r = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1,
                          '%s - "%s %s HTTP/%s" %d', ("127.0.0.1:1", "POST", path, "1.1", 200), None)
    RedactQuerySecrets().filter(r)
    return r.getMessage()


def test_access_log_masks_token():
    msg = _redact("/replay/issue/abc?project=TB&token=s3cret")
    assert "s3cret" not in msg and "token=***" in msg and "project=TB" in msg


def test_access_log_masks_signature_and_first_param():
    msg = _redact("/x?token=a1&sig=b2")
    assert "a1" not in msg and "b2" not in msg


def test_httpx_quieted():
    assert logging.getLogger("httpx").level == logging.WARNING
