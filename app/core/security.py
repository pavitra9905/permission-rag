from datetime import datetime, timedelta, timezone
from uuid import UUID

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from pydantic import BaseModel, ConfigDict

from app.core.config import settings
from app.core.constants import VALID_ROLES

_hasher = PasswordHasher()  # argon2id, library defaults
_DUMMY_HASH = _hasher.hash("dummy-password-used-for-timing")


class Principal(BaseModel):
    """The authenticated caller, built from a verified JWT."""

    model_config = ConfigDict(frozen=True)
    user_id: UUID
    tenant_id: UUID
    roles: frozenset[str]


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        return _hasher.verify(stored_hash, password)
    except (VerificationError, InvalidHashError):  # mismatch subclasses VerificationError
        return False


def burn_password_check(password: str) -> None:
    """Spend the same time as a real check when the user doesn't exist."""
    verify_password(password, _DUMMY_HASH)


def create_access_token(
    *, user_id: UUID, tenant_id: UUID, roles: list[str], expires_delta: timedelta | None = None
) -> str:
    now = datetime.now(timezone.utc)
    exp = now + (expires_delta or timedelta(minutes=settings.access_token_minutes))
    payload = {
        "sub": str(user_id),
        "tenant_id": str(tenant_id),
        "roles": sorted(roles),
        "iss": settings.jwt_issuer,
        "iat": now,
        "exp": exp,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> Principal:
    """Raises jwt.PyJWTError / ValueError / KeyError on anything wrong."""
    claims = jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=[settings.jwt_algorithm],  # pinned: blocks alg=none and alg confusion
        issuer=settings.jwt_issuer,
        leeway=10,
        options={"require": ["exp", "iat", "iss", "sub"]},
    )
    roles = claims["roles"]
    if not isinstance(roles, list) or not set(roles) <= VALID_ROLES:
        raise jwt.InvalidTokenError("bad roles claim")
    return Principal(user_id=claims["sub"], tenant_id=claims["tenant_id"], roles=frozenset(roles))