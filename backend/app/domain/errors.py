from fastapi import HTTPException


class ApiError(HTTPException):
    def __init__(
        self,
        code: str,
        status_code: int,
        message: str,
        details: dict | None = None,
    ):
        body: dict = {"error": {"code": code, "message": message}}
        if details:
            body["error"]["details"] = details
        super().__init__(status_code=status_code, detail=body)

    @staticmethod
    def unauthorized(message: str = "Unauthorized") -> "ApiError":
        return ApiError("unauthorized", 401, message)

    @staticmethod
    def forbidden(message: str = "Forbidden") -> "ApiError":
        return ApiError("forbidden", 403, message)

    @staticmethod
    def not_found(message: str = "Not found") -> "ApiError":
        return ApiError("not_found", 404, message)

    @staticmethod
    def invalid_request(message: str, details: dict | None = None) -> "ApiError":
        return ApiError("invalid_request", 400, message, details)

    @staticmethod
    def conflict(message: str) -> "ApiError":
        return ApiError("conflict", 409, message)

    @staticmethod
    def insufficient_inventory(sku: str) -> "ApiError":
        return ApiError(
            "insufficient_inventory",
            409,
            f"Insufficient inventory for SKU: {sku}",
            {"sku": sku},
        )

    @staticmethod
    def stripe_error(message: str) -> "ApiError":
        return ApiError("stripe_error", 502, message)

    @staticmethod
    def rate_limit_exceeded(message: str) -> "ApiError":
        return ApiError("rate_limit_exceeded", 429, message)
