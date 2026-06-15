from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.models import Inventory, InventoryLog, Product, Variant
from app.db.session import get_db
from app.deps.auth import require_admin
from app.domain.errors import ApiError
from app.domain.utils import now_iso, uuid4
from app.lib.crypto import AuthContext
from app.lib.webhooks import check_low_inventory

router = APIRouter(prefix="/v1/inventory", tags=["Inventory"])

AdjustmentReason = Literal["restock", "correction", "damaged", "return"]


class AdjustInventoryBody(BaseModel):
    delta: int
    reason: AdjustmentReason


def _inventory_item_dict(
    inventory: Inventory,
    variant_title: str | None = None,
    product_title: str | None = None,
    *,
    include_details: bool = True,
) -> dict:
    item = {
        "sku": inventory.sku,
        "on_hand": inventory.on_hand,
        "reserved": inventory.reserved,
        "available": inventory.on_hand - inventory.reserved,
    }
    if include_details:
        item["variant_title"] = variant_title
        item["product_title"] = product_title
    return item


def _inventory_query(db: Session):
    return (
        db.query(
            Inventory,
            Variant.title.label("variant_title"),
            Product.title.label("product_title"),
        )
        .outerjoin(Variant, Inventory.sku == Variant.sku)
        .outerjoin(Product, Variant.product_id == Product.id)
    )


@router.get("")
def list_inventory(
    db: Session = Depends(get_db),
    _auth: AuthContext = Depends(require_admin),
    sku: str | None = Query(default=None),
    limit: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    low_stock: str | None = Query(default=None),
):
    if sku:
        row = _inventory_query(db).filter(Inventory.sku == sku).first()
        if not row:
            raise ApiError.not_found("SKU not found")

        inventory, variant_title, product_title = row
        return {
            "items": [_inventory_item_dict(inventory, variant_title, product_title)],
            "pagination": {"has_more": False, "next_cursor": None},
        }

    page_limit = min(int(limit or "100"), 500)
    low_stock_filter = low_stock == "true"

    query = _inventory_query(db)
    if low_stock_filter:
        query = query.filter((Inventory.on_hand - Inventory.reserved) <= 10)
    if cursor:
        query = query.filter(Inventory.sku > cursor)

    rows = query.order_by(Inventory.sku).limit(page_limit + 1).all()
    has_more = len(rows) > page_limit
    if has_more:
        rows = rows[:page_limit]

    items = [
        _inventory_item_dict(inventory, variant_title, product_title)
        for inventory, variant_title, product_title in rows
    ]
    next_cursor = items[-1]["sku"] if has_more and items else None

    return {
        "items": items,
        "pagination": {"has_more": has_more, "next_cursor": next_cursor},
    }


@router.post("/{sku}/adjust")
async def adjust_inventory(
    sku: str,
    body: AdjustInventoryBody,
    db: Session = Depends(get_db),
    _auth: AuthContext = Depends(require_admin),
):
    inventory = db.query(Inventory).filter(Inventory.sku == sku).first()
    if not inventory:
        raise ApiError.not_found("SKU not found")

    if body.delta < 0 and inventory.on_hand + body.delta < 0:
        raise ApiError.invalid_request(
            f"Cannot reduce inventory below 0. Current on_hand: {inventory.on_hand}"
        )

    inventory.on_hand += body.delta
    inventory.updated_at = now_iso()

    db.add(
        InventoryLog(
            id=uuid4(),
            sku=sku,
            delta=body.delta,
            reason=body.reason,
            created_at=now_iso(),
        )
    )
    db.commit()
    db.refresh(inventory)

    available = inventory.on_hand - inventory.reserved
    await check_low_inventory(db, sku, available)

    return _inventory_item_dict(inventory, include_details=False)
