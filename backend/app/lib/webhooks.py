import asyncio
import hashlib
import hmac
import json
import time
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.domain.utils import now_iso, uuid4
from app.db.models import Webhook, WebhookDelivery

MAX_ATTEMPTS = 3
LOW_INVENTORY_THRESHOLD = 5

WebhookEventType = str


def _sign_payload(payload: str, secret: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


async def _deliver(
    db: Session,
    webhook_id: str,
    url: str,
    secret: str,
    delivery_id: str,
    payload: dict[str, Any],
) -> None:
    payload_string = json.dumps(payload)
    signature = _sign_payload(payload_string, secret)
    timestamp = int(time.time())
    last_error: str | None = None
    response_code: int | None = None
    response_body: str | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        delivery = db.get(WebhookDelivery, delivery_id)
        if delivery:
            delivery.attempts = attempt
            delivery.last_attempt_at = now_iso()
            db.commit()

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    url,
                    content=payload_string,
                    headers={
                        "Content-Type": "application/json",
                        "X-Merchant-Signature": signature,
                        "X-Merchant-Timestamp": str(timestamp),
                        "X-Merchant-Delivery-Id": delivery_id,
                        "User-Agent": "Merchant-Webhook/1.0",
                    },
                )
            response_code = response.status_code
            response_body = response.text[:1000]

            delivery = db.get(WebhookDelivery, delivery_id)
            if response.is_success and delivery:
                delivery.status = "success"
                delivery.response_code = response_code
                delivery.response_body = response_body
                db.commit()
                return

            if 400 <= response_code < 500 and response_code != 429 and delivery:
                delivery.status = "failed"
                delivery.response_code = response_code
                delivery.response_body = response_body
                db.commit()
                return

            last_error = f"HTTP {response_code}"
        except Exception as exc:
            last_error = str(exc)

        if attempt < MAX_ATTEMPTS:
            await asyncio.sleep(2**attempt)

    delivery = db.get(WebhookDelivery, delivery_id)
    if delivery:
        delivery.status = "failed"
        delivery.response_code = response_code
        delivery.response_body = (last_error or response_body or "")[:1000]
        db.commit()


def _is_subscribed(events: list[str], event_type: str) -> bool:
    for e in events:
        if e == "*" or e == event_type:
            return True
        if e.endswith(".*") and event_type.startswith(e[:-2] + "."):
            return True
    return False


async def dispatch_webhooks(
    db: Session,
    event_type: WebhookEventType,
    data: dict[str, Any],
) -> None:
    webhooks = db.query(Webhook).filter(Webhook.status == "active").all()
    for webhook in webhooks:
        subscribed = json.loads(webhook.events or "[]")
        if not _is_subscribed(subscribed, event_type):
            continue

        delivery_id = uuid4()
        payload = {
            "id": delivery_id,
            "type": event_type,
            "created_at": now_iso(),
            "data": data,
        }
        db.add(
            WebhookDelivery(
                id=delivery_id,
                webhook_id=webhook.id,
                event_type=event_type,
                payload=json.dumps(payload),
                status="pending",
                created_at=now_iso(),
            )
        )
        db.commit()
        asyncio.create_task(
            _deliver(db, webhook.id, webhook.url, webhook.secret, delivery_id, payload)
        )


async def retry_delivery(
    db: Session,
    webhook: Webhook,
    delivery: WebhookDelivery,
) -> None:
    payload = json.loads(delivery.payload)
    await _deliver(db, webhook.id, webhook.url, webhook.secret, delivery.id, payload)


async def check_low_inventory(db: Session, sku: str, available: int) -> None:
    if 0 <= available <= LOW_INVENTORY_THRESHOLD:
        await dispatch_webhooks(
            db,
            "inventory.low",
            {"sku": sku, "available": available, "threshold": LOW_INVENTORY_THRESHOLD},
        )
