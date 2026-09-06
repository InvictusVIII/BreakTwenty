from fastapi import APIRouter, Depends

from app.api.routes import CurrentUser, get_current_user
from app.brand import APP_BRAND_NAME
from app.database_encryption import sqlcipher_diagnostics
from app.services.startup_health import get_startup_health

router = APIRouter()


@router.get("/health")
async def health_check(_current_user: CurrentUser = Depends(get_current_user)):
    startup = get_startup_health()
    return {
        "status": startup["status"],
        "app": APP_BRAND_NAME,
        "startup": startup,
        "security": {"databaseEncryption": sqlcipher_diagnostics()},
    }
