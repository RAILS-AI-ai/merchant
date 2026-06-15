from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import ApiKey, Customer, OAuthToken
from app.db.session import get_db, get_stripe_config
from app.domain.errors import ApiError
from app.domain.utils import now_iso
from app.lib.crypto import AuthContext, hash_key


async def get_auth_context(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    db: Session = Depends(get_db),
) -> AuthContext:
    if not authorization or not authorization.startswith("Bearer "):
        raise ApiError.unauthorized("Missing or invalid Authorization header")

    token = authorization[7:]
    stripe_cfg = get_stripe_config(db)

    if len(token) == 64 and all(c in "0123456789abcdef" for c in token):
        token_hash = hash_key(token)
        row = (
            db.query(OAuthToken, Customer.email)
            .join(Customer, OAuthToken.customer_id == Customer.id)
            .filter(
                OAuthToken.access_token_hash == token_hash,
                OAuthToken.access_expires_at > now_iso(),
            )
            .first()
        )
        if row:
            oauth_token, email = row
            return AuthContext(
                role="oauth",
                stripe_secret_key=stripe_cfg["secret_key"],
                stripe_webhook_secret=stripe_cfg["webhook_secret"],
                oauth_scopes=(oauth_token.scope or "").split(),
                customer_email=email,
            )

    key_hash = hash_key(token)
    api_key = db.query(ApiKey).filter(ApiKey.key_hash == key_hash).first()
    if not api_key:
        raise ApiError.unauthorized("Invalid API key")

    return AuthContext(
        role=api_key.role,
        stripe_secret_key=stripe_cfg["secret_key"],
        stripe_webhook_secret=stripe_cfg["webhook_secret"],
    )


def require_admin(auth: AuthContext = Depends(get_auth_context)) -> AuthContext:
    if auth.role != "admin":
        raise ApiError.forbidden("Admin access required")
    return auth


def require_scope(*scopes: str):
    def _dep(auth: AuthContext = Depends(get_auth_context)) -> AuthContext:
        if auth.role == "oauth":
            if not all(s in auth.oauth_scopes for s in scopes):
                raise ApiError.forbidden(f"Required scopes: {', '.join(scopes)}")
        return auth

    return _dep
