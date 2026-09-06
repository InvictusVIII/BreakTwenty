from dataclasses import dataclass

from fastapi import HTTPException, Request

from app.launch_auth import AuthPrincipal


DEFAULT_DEV_USER_ID = 1


@dataclass(frozen=True)
class CurrentUser:
    id: int = DEFAULT_DEV_USER_ID


async def get_current_user(request: Request) -> CurrentUser:
    principal = getattr(request.state, "auth_principal", None)
    if not isinstance(principal, AuthPrincipal) or principal.user_id <= 0:
        raise HTTPException(status_code=401, detail="Authentication required")
    return CurrentUser(id=principal.user_id)


def get_auth_principal(request: Request) -> AuthPrincipal:
    principal = getattr(request.state, "auth_principal", None)
    if not isinstance(principal, AuthPrincipal):
        raise HTTPException(status_code=401, detail="Authentication required")
    return principal
