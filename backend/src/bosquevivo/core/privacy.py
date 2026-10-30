import hashlib
import hmac

INSECURE_KEYS = {"", "dev-only-change-me"}


def phone_hmac(phone: str, key: str) -> str:
    if not key:
        raise ValueError("PHONE_HMAC_KEY no está configurada")
    normalized = "".join(ch for ch in phone if ch.isdigit())
    return hmac.new(key.encode(), normalized.encode(), hashlib.sha256).hexdigest()
