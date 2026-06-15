"""SQLAlchemy models matching the original Durable Object SQLite schema."""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


class ApiKey(Base):
    __tablename__ = "api_keys"
    id = Column(String, primary_key=True)
    key_hash = Column(String, nullable=False, unique=True)
    key_prefix = Column(String, nullable=False)
    role = Column(String, nullable=False)
    created_at = Column(String, nullable=False)


class Product(Base):
    __tablename__ = "products"
    id = Column(String, primary_key=True)
    title = Column(String, nullable=False)
    description = Column(Text, default="")
    image_url = Column(String)
    status = Column(String, nullable=False, default="active")
    created_at = Column(String, nullable=False)


class Variant(Base):
    __tablename__ = "variants"
    id = Column(String, primary_key=True)
    product_id = Column(String, ForeignKey("products.id"), nullable=False)
    sku = Column(String, nullable=False, unique=True)
    title = Column(String, nullable=False)
    price_cents = Column(Integer, nullable=False)
    currency = Column(String, nullable=False, default="USD")
    weight_g = Column(Integer, nullable=False, default=0)
    dims_cm = Column(Text)
    image_url = Column(String)
    status = Column(String, nullable=False, default="active")
    created_at = Column(String, nullable=False)


class Inventory(Base):
    __tablename__ = "inventory"
    id = Column(String, primary_key=True)
    sku = Column(String, nullable=False, unique=True)
    on_hand = Column(Integer, nullable=False, default=0)
    reserved = Column(Integer, nullable=False, default=0)
    updated_at = Column(String, nullable=False)


class InventoryLog(Base):
    __tablename__ = "inventory_logs"
    id = Column(String, primary_key=True)
    sku = Column(String, nullable=False)
    delta = Column(Integer, nullable=False)
    reason = Column(String, nullable=False)
    created_at = Column(String, nullable=False)


class Discount(Base):
    __tablename__ = "discounts"
    id = Column(String, primary_key=True)
    code = Column(String, unique=True)
    type = Column(String, nullable=False)
    value = Column(Integer, nullable=False)
    status = Column(String, nullable=False, default="active")
    min_purchase_cents = Column(Integer, default=0)
    max_discount_cents = Column(Integer)
    starts_at = Column(String)
    expires_at = Column(String)
    usage_limit = Column(Integer)
    usage_limit_per_customer = Column(Integer, default=1)
    usage_count = Column(Integer, nullable=False, default=0)
    stripe_coupon_id = Column(String)
    stripe_promotion_code_id = Column(String)
    created_at = Column(String, nullable=False)
    updated_at = Column(String, nullable=False)


class Cart(Base):
    __tablename__ = "carts"
    id = Column(String, primary_key=True)
    status = Column(String, nullable=False, default="open")
    customer_email = Column(String, nullable=False)
    currency = Column(String, nullable=False, default="USD")
    stripe_checkout_session_id = Column(String)
    discount_code = Column(String)
    discount_id = Column(String, ForeignKey("discounts.id"))
    discount_amount_cents = Column(Integer, default=0)
    expires_at = Column(String, nullable=False)
    created_at = Column(String, nullable=False)
    updated_at = Column(String, nullable=False)


class CartItem(Base):
    __tablename__ = "cart_items"
    id = Column(String, primary_key=True)
    cart_id = Column(String, ForeignKey("carts.id"), nullable=False)
    sku = Column(String, nullable=False)
    title = Column(String, nullable=False)
    qty = Column(Integer, nullable=False)
    unit_price_cents = Column(Integer, nullable=False)


class Customer(Base):
    __tablename__ = "customers"
    id = Column(String, primary_key=True)
    email = Column(String, nullable=False, unique=True)
    name = Column(String)
    phone = Column(String)
    password_hash = Column(String)
    email_verified_at = Column(String)
    auth_provider = Column(String)
    auth_provider_id = Column(String)
    accepts_marketing = Column(Integer, default=0)
    locale = Column(String, default="en")
    metadata_json = Column("metadata", Text)
    order_count = Column(Integer, default=0)
    total_spent_cents = Column(Integer, default=0)
    created_at = Column(String, nullable=False)
    updated_at = Column(String, nullable=False)
    last_order_at = Column(String)


class CustomerAddress(Base):
    __tablename__ = "customer_addresses"
    id = Column(String, primary_key=True)
    customer_id = Column(String, ForeignKey("customers.id"), nullable=False)
    label = Column(String)
    is_default = Column(Integer, default=0)
    name = Column(String)
    company = Column(String)
    line1 = Column(String, nullable=False)
    line2 = Column(String)
    city = Column(String, nullable=False)
    state = Column(String)
    postal_code = Column(String, nullable=False)
    country = Column(String, nullable=False, default="US")
    phone = Column(String)
    created_at = Column(String, nullable=False)
    updated_at = Column(String, nullable=False)


class Order(Base):
    __tablename__ = "orders"
    id = Column(String, primary_key=True)
    customer_id = Column(String, ForeignKey("customers.id"))
    number = Column(String, nullable=False, unique=True)
    status = Column(String, nullable=False, default="paid")
    customer_email = Column(String, nullable=False)
    shipping_name = Column(String)
    shipping_phone = Column(String)
    ship_to = Column(Text)
    subtotal_cents = Column(Integer, nullable=False)
    tax_cents = Column(Integer, nullable=False)
    shipping_cents = Column(Integer, nullable=False, default=0)
    total_cents = Column(Integer, nullable=False)
    currency = Column(String, nullable=False, default="USD")
    discount_code = Column(String)
    discount_id = Column(String, ForeignKey("discounts.id"))
    discount_amount_cents = Column(Integer, default=0)
    tracking_number = Column(String)
    tracking_url = Column(String)
    shipped_at = Column(String)
    stripe_checkout_session_id = Column(String)
    stripe_payment_intent_id = Column(String)
    created_at = Column(String, nullable=False)


class OrderItem(Base):
    __tablename__ = "order_items"
    id = Column(String, primary_key=True)
    order_id = Column(String, ForeignKey("orders.id"), nullable=False)
    sku = Column(String, nullable=False)
    title = Column(String, nullable=False)
    qty = Column(Integer, nullable=False)
    unit_price_cents = Column(Integer, nullable=False)


class Refund(Base):
    __tablename__ = "refunds"
    id = Column(String, primary_key=True)
    order_id = Column(String, ForeignKey("orders.id"), nullable=False)
    stripe_refund_id = Column(String, nullable=False)
    amount_cents = Column(Integer, nullable=False)
    status = Column(String, nullable=False)
    created_at = Column(String, nullable=False)


class DiscountUsage(Base):
    __tablename__ = "discount_usage"
    id = Column(String, primary_key=True)
    discount_id = Column(String, ForeignKey("discounts.id"), nullable=False)
    order_id = Column(String, ForeignKey("orders.id"), nullable=False)
    customer_email = Column(String, nullable=False)
    discount_amount_cents = Column(Integer, nullable=False)
    created_at = Column(String, nullable=False)
    __table_args__ = (UniqueConstraint("order_id", "discount_id"),)


class Event(Base):
    __tablename__ = "events"
    id = Column(String, primary_key=True)
    stripe_event_id = Column(String, unique=True)
    type = Column(String, nullable=False)
    payload = Column(Text, nullable=False)
    processed_at = Column(String, nullable=False)


class Webhook(Base):
    __tablename__ = "webhooks"
    id = Column(String, primary_key=True)
    url = Column(String, nullable=False)
    events = Column(Text, nullable=False)
    secret = Column(String, nullable=False)
    status = Column(String, nullable=False, default="active")
    created_at = Column(String, nullable=False)


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"
    id = Column(String, primary_key=True)
    webhook_id = Column(String, ForeignKey("webhooks.id"), nullable=False)
    event_type = Column(String, nullable=False)
    payload = Column(Text, nullable=False)
    status = Column(String, nullable=False, default="pending")
    attempts = Column(Integer, nullable=False, default=0)
    last_attempt_at = Column(String)
    response_code = Column(Integer)
    response_body = Column(Text)
    created_at = Column(String, nullable=False)


class OAuthClient(Base):
    __tablename__ = "oauth_clients"
    id = Column(String, primary_key=True)
    client_id = Column(String, nullable=False, unique=True)
    client_secret_hash = Column(String)
    name = Column(String, nullable=False)
    redirect_uris = Column(Text, nullable=False, default="[]")
    created_at = Column(String, nullable=False)


class OAuthAuthorization(Base):
    __tablename__ = "oauth_authorizations"
    id = Column(String, primary_key=True)
    client_id = Column(String, nullable=False)
    redirect_uri = Column(String, nullable=False)
    scope = Column(String, nullable=False)
    state = Column(String)
    code_challenge = Column(String, nullable=False)
    customer_email = Column(String)
    magic_token_hash = Column(String)
    magic_expires_at = Column(String)
    code_hash = Column(String)
    code_expires_at = Column(String)
    status = Column(String, nullable=False, default="pending")
    expires_at = Column(String, nullable=False)
    created_at = Column(String, nullable=False)


class OAuthToken(Base):
    __tablename__ = "oauth_tokens"
    id = Column(String, primary_key=True)
    client_id = Column(String, nullable=False)
    customer_id = Column(String, ForeignKey("customers.id"), nullable=False)
    access_token_hash = Column(String, nullable=False)
    refresh_token_hash = Column(String, nullable=False)
    scope = Column(String, nullable=False)
    access_expires_at = Column(String, nullable=False)
    refresh_expires_at = Column(String, nullable=False)
    created_at = Column(String, nullable=False)


class Config(Base):
    __tablename__ = "config"
    key = Column(String, primary_key=True)
    value = Column(Text, nullable=False)
    updated_at = Column(String, nullable=False)


class UcpCheckoutSession(Base):
    __tablename__ = "ucp_checkout_sessions"
    id = Column(String, primary_key=True)
    status = Column(String, nullable=False, default="incomplete")
    currency = Column(String, nullable=False)
    line_items = Column(Text, nullable=False)
    buyer = Column(Text)
    totals = Column(Text, nullable=False)
    messages = Column(Text)
    payment_instruments = Column(Text)
    stripe_session_id = Column(String)
    order_id = Column(String)
    order_number = Column(String)
    expires_at = Column(String)
    created_at = Column(String, nullable=False)
    updated_at = Column(String, nullable=False)


_engine = None
_SessionLocal = None


def get_engine():
    global _engine
    if _engine is None:
        url = get_settings().database_url
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        _engine = create_engine(url, connect_args=connect_args)
    return _engine


def get_session_factory():
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), autoflush=False, autocommit=False)
    return _SessionLocal


def init_db():
    Path = __import__("pathlib").Path
    Path(get_settings().storage_root).mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(bind=get_engine())
