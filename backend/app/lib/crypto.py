import hashlib
import secrets
from dataclasses import dataclass, field


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def generate_api_key(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(24)}"


def generate_webhook_secret() -> str:
    return f"whsec_{secrets.token_hex(32)}"


def generate_secret() -> str:
    return secrets.token_hex(32)


@dataclass
class AuthContext:
    role: str  # public | admin | oauth
    stripe_secret_key: str | None = None
    stripe_webhook_secret: str | None = None
    oauth_scopes: list[str] = field(default_factory=list)
    customer_email: str | None = None
