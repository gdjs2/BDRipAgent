import hashlib
import hmac
import secrets
import time

from fastapi import HTTPException, Request

from shared.config import get_settings


def issue_cookie():
    payload = f"{int(time.time()) + 86400}.{secrets.token_hex(16)}"
    signature = hmac.new(get_settings().api_token.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def authenticated(request: Request):
    token = get_settings().api_token
    if not token:
        raise HTTPException(503, "Set API_TOKEN before using the service")
    header = request.headers.get("authorization", "")
    if header.startswith("Bearer ") and secrets.compare_digest(header[7:], token):
        return "operator"
    cookie = request.cookies.get("movie_session", "")
    try:
        expiry, nonce, signature = cookie.split(".")
        expected = hmac.new(token.encode(), f"{expiry}.{nonce}".encode(), hashlib.sha256).hexdigest()
        if int(expiry) > time.time() and hmac.compare_digest(signature, expected):
            return "operator"
    except ValueError:
        pass
    raise HTTPException(401, "Authentication required")
