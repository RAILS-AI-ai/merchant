"""OAuth 2.0 Authorization Code flow with PKCE for UCP compliance."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Customer, OAuthAuthorization, OAuthClient, OAuthToken
from app.db.session import get_db
from app.domain.errors import ApiError
from app.domain.utils import now_iso, uuid4
from app.lib.crypto import generate_secret, hash_key

logger = logging.getLogger(__name__)

VALID_SCOPES = frozenset(
    {
        "openid",
        "profile",
        "ucp:scopes:checkout_session",
        "ucp:scopes:order",
        "ucp:scopes:identity",
        "checkout",
        "orders.read",
        "orders.write",
        "addresses.read",
        "addresses.write",
    }
)

SCOPE_DESCRIPTIONS: dict[str, str] = {
    "openid": "Verify your identity",
    "profile": "Access your name and email",
    "ucp:scopes:checkout_session": "Create and manage checkout sessions",
    "ucp:scopes:order": "Access order information and updates",
    "ucp:scopes:identity": "Link your account",
    "checkout": "Create orders on your behalf",
    "orders.read": "View your order history",
    "orders.write": "Manage your orders",
    "addresses.read": "Access your saved addresses",
    "addresses.write": "Manage your addresses",
}

router = APIRouter(prefix="/oauth", tags=["OAuth"])
root_router = APIRouter(tags=["OAuth"])


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _generate_code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _get_or_create_customer(db: Session, email: str) -> Customer:
    normalized = email.lower().strip()
    customer = db.query(Customer).filter(Customer.email == normalized).first()
    if customer:
        return customer

    now = now_iso()
    customer = Customer(
        id=uuid4(),
        email=normalized,
        created_at=now,
        updated_at=now,
    )
    db.add(customer)
    db.flush()
    return customer


def _parse_redirect_uris(raw: str | None) -> list[str]:
    try:
        parsed = json.loads(raw or "[]")
        return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        return []


def _generate_login_page(auth_id: str, client_id: str, scope: str, store_name: str) -> str:
    scopes = [s for s in scope.split(" ") if s in SCOPE_DESCRIPTIONS]
    scope_list = "".join(f"<li>{SCOPE_DESCRIPTIONS[s]}</li>" for s in scopes)

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Sign In - {store_name}</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: system-ui, sans-serif; background: #f5f5f5; min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 20px; }}
    .card {{ background: white; border-radius: 12px; padding: 32px; max-width: 400px; width: 100%; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
    h1 {{ font-size: 24px; margin-bottom: 8px; }}
    .subtitle {{ color: #666; margin-bottom: 24px; }}
    .permissions {{ background: #f9f9f9; border-radius: 8px; padding: 16px; margin-bottom: 24px; }}
    .permissions h3 {{ font-size: 14px; color: #666; margin-bottom: 8px; }}
    .permissions ul {{ list-style: none; }}
    .permissions li {{ padding: 4px 0; font-size: 14px; }}
    .permissions li::before {{ content: "✓"; color: #22c55e; margin-right: 8px; }}
    label {{ display: block; font-size: 14px; font-weight: 500; margin-bottom: 8px; }}
    input[type="email"] {{ width: 100%; padding: 12px; border: 1px solid #ddd; border-radius: 8px; font-size: 16px; }}
    input[type="email"]:focus {{ outline: none; border-color: #000; }}
    button {{ width: 100%; padding: 12px; background: #000; color: white; border: none; border-radius: 8px; font-size: 16px; cursor: pointer; margin-top: 16px; }}
    button:hover {{ background: #333; }}
    .footer {{ text-align: center; margin-top: 16px; font-size: 12px; color: #999; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>Sign in to {store_name}</h1>
    <p class="subtitle"><strong>{client_id}</strong> wants to access your account</p>

    <div class="permissions">
      <h3>This will allow them to:</h3>
      <ul>{scope_list}</ul>
    </div>

    <form method="POST" action="/oauth/authorize">
      <input type="hidden" name="auth_id" value="{auth_id}">
      <label for="email">Email address</label>
      <input type="email" id="email" name="email" placeholder="you@example.com" required autofocus>
      <button type="submit">Continue with Email</button>
    </form>

    <p class="footer">We'll send you a link to verify your email</p>
  </div>
</body>
</html>"""


def _generate_magic_link_sent_page(email: str, magic_link: str) -> str:
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Check Your Email</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: system-ui, sans-serif; background: #f5f5f5; min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 20px; }}
    .card {{ background: white; border-radius: 12px; padding: 32px; max-width: 400px; width: 100%; box-shadow: 0 2px 8px rgba(0,0,0,0.1); text-align: center; }}
    .icon {{ font-size: 48px; margin-bottom: 16px; }}
    h1 {{ font-size: 24px; margin-bottom: 8px; }}
    .subtitle {{ color: #666; margin-bottom: 24px; }}
    .email {{ font-weight: 600; color: #000; }}
    .dev-link {{ background: #fef3c7; border: 1px solid #f59e0b; border-radius: 8px; padding: 16px; margin-top: 24px; font-size: 14px; }}
    .dev-link a {{ color: #92400e; word-break: break-all; }}
    .dev-label {{ font-size: 12px; color: #92400e; margin-bottom: 8px; font-weight: 600; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">✉️</div>
    <h1>Check Your Email</h1>
    <p class="subtitle">We sent a verification link to<br><span class="email">{email}</span></p>
    <p style="color: #666; font-size: 14px;">Click the link in the email to continue</p>

    <div class="dev-link">
      <p class="dev-label">⚠️ DEV MODE - No email service configured</p>
      <a href="{magic_link}">Click here to verify (dev only)</a>
    </div>
  </div>
</body>
</html>"""


@root_router.get("/.well-known/oauth-authorization-server")
def oauth_discovery(request: Request) -> dict:
    base = _base_url(request)
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "revocation_endpoint": f"{base}/oauth/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": [
            "client_secret_basic",
            "client_secret_post",
            "none",
        ],
        "scopes_supported": sorted(VALID_SCOPES),
        "service_documentation": "https://ucp.dev/specification/overview",
    }


@router.get("/authorize", response_class=HTMLResponse)
def authorize_get(
    request: Request,
    client_id: str | None = None,
    redirect_uri: str | None = None,
    response_type: str | None = None,
    scope: str = "openid profile",
    state: str | None = None,
    code_challenge: str | None = None,
    code_challenge_method: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    if not client_id:
        raise ApiError.invalid_request("client_id is required")
    if not redirect_uri:
        raise ApiError.invalid_request("redirect_uri is required")
    if response_type != "code":
        raise ApiError.invalid_request('response_type must be "code"')
    if not code_challenge:
        raise ApiError.invalid_request("code_challenge is required (PKCE)")
    if code_challenge_method != "S256":
        raise ApiError.invalid_request("code_challenge_method must be S256")

    requested_scopes = [s for s in scope.split(" ") if s]
    invalid_scopes = [s for s in requested_scopes if s not in VALID_SCOPES]
    if invalid_scopes:
        raise ApiError.invalid_request(f"Invalid scopes: {', '.join(invalid_scopes)}")

    store_name = get_settings().store_name

    client = db.query(OAuthClient).filter(OAuthClient.client_id == client_id).first()
    if not client:
        domain = urlparse(redirect_uri).hostname or client_id
        now = now_iso()
        client = OAuthClient(
            id=uuid4(),
            client_id=client_id,
            name=domain,
            redirect_uris=json.dumps([redirect_uri]),
            created_at=now,
        )
        db.add(client)
        db.flush()
    else:
        allowed_uris = _parse_redirect_uris(client.redirect_uris)
        if redirect_uri not in allowed_uris:
            allowed_uris.append(redirect_uri)
            client.redirect_uris = json.dumps(allowed_uris)

    auth_id = uuid4()
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    now = now_iso()

    db.add(
        OAuthAuthorization(
            id=auth_id,
            client_id=client_id,
            redirect_uri=redirect_uri,
            scope=scope,
            state=state or "",
            code_challenge=code_challenge,
            expires_at=expires_at,
            created_at=now,
        )
    )
    db.commit()

    html = _generate_login_page(auth_id, client_id, scope, store_name)
    return HTMLResponse(content=html)


@router.post("/authorize", response_class=HTMLResponse)
def authorize_post(
    request: Request,
    auth_id: str = Form(...),
    email: str = Form(...),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    normalized_email = email.lower().strip()
    if not auth_id or not normalized_email:
        raise ApiError.invalid_request("Missing auth_id or email")

    auth = (
        db.query(OAuthAuthorization)
        .filter(
            OAuthAuthorization.id == auth_id,
            OAuthAuthorization.status == "pending",
            OAuthAuthorization.expires_at > now_iso(),
        )
        .first()
    )
    if not auth:
        raise ApiError.invalid_request("Authorization expired or invalid")

    magic_token = generate_secret()
    magic_expires_at = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()

    auth.customer_email = normalized_email
    auth.magic_token_hash = hash_key(magic_token)
    auth.magic_expires_at = magic_expires_at
    db.commit()

    base = _base_url(request)
    magic_link = f"{base}/oauth/verify?token={magic_token}&auth={auth_id}"
    logger.info("[OAuth] Magic link for %s: %s", normalized_email, magic_link)

    html = _generate_magic_link_sent_page(normalized_email, magic_link)
    return HTMLResponse(content=html)


@router.get("/verify")
def verify(
    token: str | None = None,
    auth: str | None = None,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    if not token or not auth:
        raise ApiError.invalid_request("Invalid verification link")

    token_hash = hash_key(token)
    authorization = (
        db.query(OAuthAuthorization)
        .filter(
            OAuthAuthorization.id == auth,
            OAuthAuthorization.magic_token_hash == token_hash,
            OAuthAuthorization.status == "pending",
            OAuthAuthorization.magic_expires_at > now_iso(),
        )
        .first()
    )
    if not authorization:
        raise ApiError.invalid_request("Link expired or already used")

    code = generate_secret()
    code_expires_at = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()

    authorization.status = "authorized"
    authorization.code_hash = hash_key(code)
    authorization.code_expires_at = code_expires_at
    db.commit()

    params: dict[str, str] = {"code": code}
    if authorization.state:
        params["state"] = authorization.state

    redirect_url = f"{authorization.redirect_uri}?{urlencode(params)}"
    return RedirectResponse(url=redirect_url, status_code=302)


@router.post("/token")
async def token(
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        body: dict[str, Any] = await request.json()
    else:
        form = await request.form()
        body = {k: str(v) for k, v in form.items()}

    grant_type = body.get("grant_type")
    if grant_type == "authorization_code":
        return _handle_authorization_code_grant(db, body)
    if grant_type == "refresh_token":
        return _handle_refresh_token_grant(db, body)
    raise ApiError.invalid_request("Unsupported grant_type")


def _handle_authorization_code_grant(db: Session, body: dict[str, Any]) -> dict:
    code = body.get("code")
    redirect_uri = body.get("redirect_uri")
    client_id = body.get("client_id")
    code_verifier = body.get("code_verifier")

    if not code or not redirect_uri or not client_id or not code_verifier:
        raise ApiError.invalid_request("Missing required parameters")

    code_hash = hash_key(code)
    auth = (
        db.query(OAuthAuthorization)
        .filter(
            OAuthAuthorization.code_hash == code_hash,
            OAuthAuthorization.client_id == client_id,
            OAuthAuthorization.redirect_uri == redirect_uri,
            OAuthAuthorization.status == "authorized",
            OAuthAuthorization.code_expires_at > now_iso(),
        )
        .first()
    )
    if not auth:
        raise ApiError.invalid_request("Invalid or expired authorization code")

    expected_challenge = _generate_code_challenge(code_verifier)
    if expected_challenge != auth.code_challenge:
        raise ApiError.invalid_request("Invalid code_verifier")

    auth.status = "used"

    if not auth.customer_email:
        raise ApiError.invalid_request("Missing customer email on authorization")

    customer = _get_or_create_customer(db, auth.customer_email)

    access_token = generate_secret()
    refresh_token = generate_secret()
    access_expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    refresh_expires_at = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    now = now_iso()

    db.add(
        OAuthToken(
            id=uuid4(),
            client_id=client_id,
            customer_id=customer.id,
            access_token_hash=hash_key(access_token),
            refresh_token_hash=hash_key(refresh_token),
            scope=auth.scope,
            access_expires_at=access_expires_at,
            refresh_expires_at=refresh_expires_at,
            created_at=now,
        )
    )
    db.commit()

    return {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": 3600,
        "refresh_token": refresh_token,
        "scope": auth.scope,
    }


def _handle_refresh_token_grant(db: Session, body: dict[str, Any]) -> dict:
    refresh_token = body.get("refresh_token")
    client_id = body.get("client_id")

    if not refresh_token or not client_id:
        raise ApiError.invalid_request("Missing refresh_token or client_id")

    token_hash = hash_key(refresh_token)
    token = (
        db.query(OAuthToken)
        .filter(
            OAuthToken.refresh_token_hash == token_hash,
            OAuthToken.client_id == client_id,
            OAuthToken.refresh_expires_at > now_iso(),
        )
        .first()
    )
    if not token:
        raise ApiError.unauthorized("Invalid or expired refresh token")

    new_access_token = generate_secret()
    access_expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

    token.access_token_hash = hash_key(new_access_token)
    token.access_expires_at = access_expires_at
    db.commit()

    return {
        "access_token": new_access_token,
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": token.scope,
    }


@router.post("/revoke")
async def revoke(
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        body: dict[str, Any] = await request.json()
        token = body.get("token")
    else:
        form = await request.form()
        token = form.get("token")

    if not token:
        return {"revoked": True}

    token_hash = hash_key(str(token))
    db.query(OAuthToken).filter(
        (OAuthToken.access_token_hash == token_hash)
        | (OAuthToken.refresh_token_hash == token_hash)
    ).delete()
    db.commit()

    return {"revoked": True}
