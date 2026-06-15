from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.db.models import Cart, CartItem, Inventory
from app.domain.utils import now_iso


def cleanup_expired_carts(db: Session) -> int:
    now = now_iso()
    expired = db.query(Cart).filter(Cart.status == "open", Cart.expires_at < now).all()
    if not expired:
        return 0

    cart_ids = [c.id for c in expired]
    items = db.query(CartItem).filter(CartItem.cart_id.in_(cart_ids)).all()
    sku_qty: dict[str, int] = {}
    for item in items:
        sku_qty[item.sku] = sku_qty.get(item.sku, 0) + item.qty

    for sku, qty in sku_qty.items():
        inv = db.query(Inventory).filter(Inventory.sku == sku).first()
        if inv:
            inv.reserved = max(0, inv.reserved - qty)
            inv.updated_at = now_iso()

    for cart in expired:
        cart.status = "expired"
    db.query(CartItem).filter(CartItem.cart_id.in_(cart_ids)).delete()
    db.commit()
    return len(expired)
