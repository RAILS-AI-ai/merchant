import time
from dataclasses import dataclass

from fastapi import Request

from app.domain.errors import ApiError

RATE_LIMITS = {
    "default": {"requests": 100, "window_ms": 60_000},
    "admin": {"requests": 500, "window_ms": 60_000},
    "public": {"requests": 60, "window_ms": 60_000},
    "endpoints": {
        "/v1/carts": {"requests": 30, "window_ms": 60_000},
        "/v1/webhooks/stripe": {"requests": 1000, "window_ms": 60_000},
        "/v1/images": {"requests": 20, "window_ms": 60_000},
    },
    "whitelist": [],
    "include_headers": True,
}

_counters: dict[str, dict] = {}
_last_cleanup = time.time()


@dataclass
class RateLimitConfig:
    requests: int
    window_ms: int


def _cleanup():
    global _last_cleanup
    now = time.time() * 1000
    if now - _last_cleanup < 60_000:
        return
    _last_cleanup = now
    cutoff = now - 5 * 60_000
    for key in list(_counters.keys()):
        if _counters[key]["window_start"] < cutoff:
            del _counters[key]


def get_limit_for_request(path: str, role: str | None) -> RateLimitConfig:
    for prefix, cfg in RATE_LIMITS["endpoints"].items():
        if path.startswith(prefix):
            return RateLimitConfig(**cfg)
    if role == "admin":
        return RateLimitConfig(**RATE_LIMITS["admin"])
    if role == "public":
        return RateLimitConfig(**RATE_LIMITS["public"])
    return RateLimitConfig(**RATE_LIMITS["default"])


def check_rate_limit(identifier: str, config: RateLimitConfig) -> tuple[bool, int, int]:
    _cleanup()
    now = time.time() * 1000
    window_start = int(now // config.window_ms) * config.window_ms
    key = f"{identifier}:{window_start}"
    counter = _counters.get(key, {"count": 0, "window_start": window_start})
    if counter["window_start"] != window_start:
        counter = {"count": 0, "window_start": window_start}
    remaining = max(0, config.requests - counter["count"])
    reset_at = window_start + config.window_ms
    if counter["count"] >= config.requests:
        _counters[key] = counter
        return False, 0, int(reset_at / 1000)
    counter["count"] += 1
    _counters[key] = counter
    return True, remaining - 1, int(reset_at / 1000)


async def rate_limit_middleware(request: Request, call_next):
    if not request.url.path.startswith("/v1/"):
        return await call_next(request)

    auth_header = request.headers.get("Authorization", "")
    api_key = auth_header.replace("Bearer ", "") if auth_header.startswith("Bearer ") else None
    ip = request.headers.get("X-Forwarded-For", request.client.host if request.client else "unknown")
    identifier = api_key or ip

    if api_key and any(api_key.startswith(w) for w in RATE_LIMITS["whitelist"]):
        return await call_next(request)

    role = None
    if api_key and api_key.startswith("sk_"):
        role = "admin"
    elif api_key and api_key.startswith("pk_"):
        role = "public"

    config = get_limit_for_request(request.url.path, role)
    allowed, remaining, reset_at = check_rate_limit(identifier, config)

    if RATE_LIMITS["include_headers"]:
        request.state.rate_limit_headers = {
            "X-RateLimit-Limit": str(config.requests),
            "X-RateLimit-Remaining": str(remaining),
            "X-RateLimit-Reset": str(reset_at),
        }

    if not allowed:
        retry_after = max(1, reset_at - int(time.time()))
        raise ApiError.rate_limit_exceeded(
            f"Rate limit exceeded. Try again in {retry_after} seconds."
        )

    response = await call_next(request)
    if hasattr(request.state, "rate_limit_headers"):
        for k, v in request.state.rate_limit_headers.items():
            response.headers[k] = v
    return response
