import json
from typing import Literal

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.models import ApiKey, Config
from app.db.session import get_db
from app.deps.auth import AuthContext, get_auth_context, require_admin
from app.domain.errors import ApiError
from app.domain.utils import now_iso

router = APIRouter(tags=["Setup"])


class InitKeyItem(BaseModel):
    id: str
    key_hash: str
    key_prefix: str
    role: Literal["public", "admin"]


class InitKeysBody(BaseModel):
    keys: list[InitKeyItem]


class SetupStripeBody(BaseModel):
    stripe_secret_key: str = Field(..., pattern=r"^sk_")
    stripe_webhook_secret: str | None = Field(default=None, pattern=r"^whsec_")


@router.post("/init")
def init_keys(body: InitKeysBody, db: Session = Depends(get_db)) -> dict:
    existing = db.query(ApiKey.id).limit(1).first()
    if existing:
        raise ApiError.conflict("API keys already exist. Use admin key to manage keys.")

    now = now_iso()
    for key in body.keys:
        db.add(
            ApiKey(
                id=key.id,
                key_hash=key.key_hash,
                key_prefix=key.key_prefix,
                role=key.role,
                created_at=now,
            )
        )
    db.commit()
    return {"ok": True}


@router.post("/stripe")
async def setup_stripe(
    body: SetupStripeBody,
    db: Session = Depends(get_db),
    _auth: AuthContext = Depends(require_admin),
) -> dict:
    async with httpx.AsyncClient() as client:
        res = await client.get(
            "https://api.stripe.com/v1/balance",
            headers={"Authorization": f"Bearer {body.stripe_secret_key}"},
        )

    if not res.is_success:
        raise ApiError.invalid_request("Invalid Stripe secret key")

    config_value = json.dumps(
        {
            "secret_key": body.stripe_secret_key,
            "webhook_secret": body.stripe_webhook_secret,
        }
    )
    now = now_iso()
    row = db.get(Config, "stripe")
    if row:
        row.value = config_value
        row.updated_at = now
    else:
        db.add(Config(key="stripe", value=config_value, updated_at=now))
    db.commit()
    return {"ok": True}
