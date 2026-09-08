from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.routes import CurrentUser, get_current_user
from app.brand import APP_BRAND_NAME
from app.database_encryption import sqlcipher_diagnostics
from app.launch_auth import prove_backend_ownership
from app.services.startup_health import get_startup_health

router = APIRouter()


@router.get("/health/ownership")
async def health_ownership(
    challenge: str = Query(min_length=43, max_length=43),
):
    try:
        proof = prove_backend_ownership(challenge)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail="Backend ownership proof is unavailable",
        ) from exc
    return {"proof": proof}


@router.get("/health")
async def health_check(_current_user: CurrentUser = Depends(get_current_user)):
    startup = get_startup_health()
    return {
        "status": startup["status"],
        "app": APP_BRAND_NAME,
        "startup": startup,
        "security": {"databaseEncryption": sqlcipher_diagnostics()},
    }
