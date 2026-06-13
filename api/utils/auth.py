from __future__ import annotations

import hashlib
import hmac
import json
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import datetime

from api.config import get_settings

settings = get_settings()


def create_token(payload: dict) -> str:
    header = urlsafe_b64encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode()).decode().rstrip("=")
    payload_encoded = urlsafe_b64encode(json.dumps(payload, default=str).encode()).decode().rstrip("=")
    signing_input = f"{header}.{payload_encoded}"
    signature = hmac.new(settings.JWT_SECRET.encode(), signing_input.encode(), hashlib.sha256).digest()
    signature_encoded = urlsafe_b64encode(signature).decode().rstrip("=")
    return f"{header}.{payload_encoded}.{signature_encoded}"


def verify_token(token: str) -> dict | None:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None

        header, payload_encoded, signature_encoded = parts

        signing_input = f"{header}.{payload_encoded}"
        expected_sig = hmac.new(settings.JWT_SECRET.encode(), signing_input.encode(), hashlib.sha256).digest()
        actual_sig = urlsafe_b64decode(signature_encoded + "==")

        if not hmac.compare_digest(expected_sig, actual_sig):
            return None

        padding = 4 - len(payload_encoded) % 4
        if padding != 4:
            payload_encoded += "=" * padding
        payload = json.loads(urlsafe_b64decode(payload_encoded))

        if "exp" in payload:
            exp = datetime.fromisoformat(payload["exp"]) if isinstance(payload["exp"], str) else datetime.utcfromtimestamp(payload["exp"])
            if exp < datetime.utcnow():
                return None

        return payload
    except Exception:
        return None


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def verify_password(password: str, hashed: str) -> bool:
    return hmac.compare_digest(hash_password(password), hashed)


def generate_otp() -> str:
    import secrets
    return f"{secrets.randbelow(900000) + 100000}"
