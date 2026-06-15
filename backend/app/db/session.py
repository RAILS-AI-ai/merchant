import json
from typing import Generator

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Config, get_session_factory


def get_db() -> Generator[Session, None, None]:
    factory = get_session_factory()
    db = factory()
    try:
        yield db
    finally:
        db.close()


def get_stripe_config(db: Session) -> dict:
    settings = get_settings()
    row = db.get(Config, "stripe")
    if row:
        try:
            parsed = json.loads(row.value)
            return {
                "secret_key": parsed.get("secret_key") or settings.stripe_secret_key,
                "webhook_secret": parsed.get("webhook_secret") or settings.stripe_webhook_secret,
            }
        except json.JSONDecodeError:
            pass
    return {
        "secret_key": settings.stripe_secret_key,
        "webhook_secret": settings.stripe_webhook_secret,
    }
