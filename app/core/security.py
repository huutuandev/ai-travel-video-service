from hashlib import sha256
from hmac import compare_digest
from typing import Annotated
from uuid import UUID

from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader

from app.core.config import settings

api_key_header = APIKeyHeader(name="X-Service-API-Key", auto_error=False)


def authenticated_tenant(key: Annotated[str | None, Security(api_key_header)]) -> UUID:
    supplied = sha256((key or "").encode()).digest()
    tenant = None
    for tenant_id, secret in settings().service_api_keys.items():
        expected = sha256(secret.get_secret_value().encode()).digest()
        if compare_digest(supplied, expected):
            tenant = tenant_id
    if tenant is None:
        raise HTTPException(401, "Invalid service API key")
    return tenant

