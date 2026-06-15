import random
import re
import uuid
from datetime import datetime, timezone


def uuid4() -> str:
    return str(uuid.uuid4())


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def generate_order_number() -> str:
    date_part = datetime.now(timezone.utc).strftime("%y%m%d")
    chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    suffix = "".join(random.choice(chars) for _ in range(4))
    return f"ORD-{date_part}-{suffix}"


def is_valid_email(email: str) -> bool:
    return bool(re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", email))
