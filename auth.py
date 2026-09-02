import jwt
from fastapi import Cookie, Header, HTTPException, Request, status

from config import settings


def decode_access_token(access_token: str | None, jwt_secret: str) -> str | None:
    if not access_token:
        return None
    try:
        payload = jwt.decode(access_token, jwt_secret, algorithms=["HS256"])
        return payload.get("sub")
    except (jwt.InvalidTokenError, KeyError):
        return None


def _extract_token(request: Request, access_token: str | None, authorization: str | None) -> str | None:
    token = access_token
    if not token and authorization:
        parts = authorization.split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1]
        elif len(parts) == 1:
            token = parts[0]
    if not token:
        token = request.cookies.get("access_token")
    return token


async def current_user_id(
    request: Request,
    access_token: str | None = Cookie(default=None),
    authorization: str | None = Header(default=None),
) -> str:
    token = _extract_token(request, access_token, authorization)
    user_id = decode_access_token(token, settings.jwt_secret)
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    return user_id


async def optional_user_id(
    request: Request,
    access_token: str | None = Cookie(default=None),
    authorization: str | None = Header(default=None),
) -> str | None:
    token = _extract_token(request, access_token, authorization)
    return decode_access_token(token, settings.jwt_secret)
