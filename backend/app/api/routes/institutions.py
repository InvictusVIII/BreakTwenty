import math
import re
from io import BytesIO
from xml.etree import ElementTree

from defusedxml import ElementTree as DefusedElementTree
from defusedxml.common import DefusedXmlException
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import Response
from PIL import Image, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes import CurrentUser, get_current_user, get_db
from app.models import Account, Institution
from app.services.networth import get_accounts_payload
from app.provider_catalog import (
    get_provider_credential_storage_provider,
    get_related_state_providers,
    iter_available_institutions,
    provider_uses_background_transaction_import,
)
from app.services.institution_cleanup import delete_institution_and_accounts, delete_provider_state
from app.services.manual_institutions import (
    add_accounts_to_manual_institution,
    create_manual_institution,
)
from app.services.runtime_state import normalize_visible_auth_attempt_id
from app.services.sqlite_write_gate import sqlite_write_gate
from app.services.transaction_import_jobs import enqueue_provider_transaction_import

router = APIRouter()


def _is_pending_add_institution(institution: Institution) -> bool:
    return not bool(institution.enabled) and bool(institution.hidden)


def _provider_add_state_key(provider: str) -> str:
    try:
        return get_provider_credential_storage_provider(provider)
    except KeyError:
        return provider


def _provider_transaction_import_key(provider: str) -> str:
    candidates = (provider, *get_related_state_providers(provider))
    for candidate in candidates:
        try:
            if provider_uses_background_transaction_import(candidate):
                return candidate
        except KeyError:
            continue
    return provider


async def _institution_has_accounts(db: AsyncSession, user_id: int, institution_id: int) -> bool:
    accounts_result = await db.execute(
        select(Account.id).where(
            Account.institution_id == institution_id,
            Account.user_id == user_id,
        ).limit(1)
    )
    return accounts_result.scalar_one_or_none() is not None


@router.post("/institutions/manual")
async def add_manual_institution(
    body: dict,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Create a user-defined manual institution + accounts (each one currency).

    Manual institutions are never synced (their provider is not in the connector
    catalog) and are the only valid targets for generic CSV import. Body shape:
    ``{"name": str, "accounts": [{"name", "account_type"?, "currency"?,
    "is_liability"?}, ...]}``.
    """
    async with sqlite_write_gate():
        result = await create_manual_institution(
            db,
            current_user.id,
            name=body.get("name", ""),
            accounts=body.get("accounts") or [],
            category=body.get("category"),
        )
        if result.get("status") != "ok":
            await db.rollback()
            return result
        await db.commit()
    return result


@router.post("/institutions/{institution_id}/accounts")
async def add_manual_institution_accounts(
    institution_id: int,
    body: dict,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Append accounts to an existing user manual institution (provider
    ``manual_custom``). Manual-only — synced and asset-group institutions are
    rejected. Body shape: ``{"accounts": [{"name", "account_type"?, "currency"?,
    "opening_balance"?, "is_liability"?}, ...]}``.
    """
    async with sqlite_write_gate():
        result = await add_accounts_to_manual_institution(
            db,
            current_user.id,
            institution_id,
            body.get("accounts") or [],
        )
        if result.get("status") != "ok":
            await db.rollback()
            return result
        await db.commit()
    return result


MAX_LOGO_BYTES = 1024 * 1024  # 1 MB cap — institution logos are small icons
MAX_LOGO_DIMENSION = 4096
MAX_LOGO_PIXELS = 16_000_000
MAX_SVG_ELEMENTS = 5_000
MAX_SVG_ATTRIBUTES = 20_000
SVG_NAMESPACE = "http://www.w3.org/2000/svg"
XLINK_NAMESPACE = "http://www.w3.org/1999/xlink"
_SVG_ALLOWED_ELEMENTS = frozenset(
    {
        "circle",
        "clipPath",
        "defs",
        "desc",
        "ellipse",
        "g",
        "line",
        "linearGradient",
        "mask",
        "path",
        "polygon",
        "polyline",
        "radialGradient",
        "rect",
        "stop",
        "svg",
        "symbol",
        "text",
        "title",
        "tspan",
        "use",
    }
)
_SVG_LENGTH_RE = re.compile(r"^(?:\d+(?:\.\d+)?|\.\d+)(?:px)?$", re.IGNORECASE)
_SVG_URL_RE = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.IGNORECASE)


def _validate_raster_logo(data: bytes) -> tuple[str, str]:
    formats = {
        "PNG": ("image/png", "png"),
        "JPEG": ("image/jpeg", "jpg"),
        "WEBP": ("image/webp", "webp"),
    }
    try:
        with Image.open(BytesIO(data)) as image:
            media = formats.get(str(image.format or "").upper())
            width, height = image.size
            if media is None:
                raise ValueError("Logo must be a valid PNG, JPEG, or WebP image")
            if width <= 0 or height <= 0:
                raise ValueError("Logo dimensions are invalid")
            if (
                width > MAX_LOGO_DIMENSION
                or height > MAX_LOGO_DIMENSION
                or width * height > MAX_LOGO_PIXELS
            ):
                raise ValueError("Logo dimensions are too large")
            image.load()
    except ValueError:
        raise
    except (Image.DecompressionBombError, OSError, SyntaxError, UnidentifiedImageError) as exc:
        raise ValueError("Logo must be a valid PNG, JPEG, or WebP image") from exc
    return media


def _svg_local_name(value: str) -> tuple[str, str]:
    if value.startswith("{") and "}" in value:
        namespace, local_name = value[1:].split("}", 1)
        return namespace, local_name
    return "", value


def _svg_dimension(value: str | None) -> float | None:
    raw = str(value or "").strip()
    if not raw or not _SVG_LENGTH_RE.fullmatch(raw):
        return None
    number = float(raw[:-2] if raw.lower().endswith("px") else raw)
    return number if math.isfinite(number) and number > 0 else None


def _validate_svg_dimensions(root: ElementTree.Element) -> None:
    width = _svg_dimension(root.attrib.get("width"))
    height = _svg_dimension(root.attrib.get("height"))
    view_box = str(root.attrib.get("viewBox") or "").strip()
    if view_box:
        try:
            parts = [float(part) for part in re.split(r"[\s,]+", view_box) if part]
        except ValueError as exc:
            raise ValueError("SVG logo viewBox is invalid") from exc
        if len(parts) != 4 or not all(math.isfinite(part) for part in parts):
            raise ValueError("SVG logo viewBox is invalid")
        if parts[2] <= 0 or parts[3] <= 0:
            raise ValueError("SVG logo dimensions are invalid")
        width = width or parts[2]
        height = height or parts[3]
    if width is None or height is None:
        raise ValueError("SVG logo requires bounded width, height, or viewBox dimensions")
    if width > MAX_LOGO_DIMENSION or height > MAX_LOGO_DIMENSION or width * height > MAX_LOGO_PIXELS:
        raise ValueError("Logo dimensions are too large")


def _sanitize_svg_logo(data: bytes) -> bytes:
    try:
        root = DefusedElementTree.fromstring(
            data,
            forbid_dtd=True,
            forbid_entities=True,
            forbid_external=True,
        )
    except (DefusedXmlException, ElementTree.ParseError, ValueError) as exc:
        raise ValueError("Logo must be a valid SVG image") from exc
    root_namespace, root_name = _svg_local_name(root.tag)
    if root_name != "svg" or root_namespace not in {"", SVG_NAMESPACE}:
        raise ValueError("Logo must be a valid SVG image")
    _validate_svg_dimensions(root)

    element_count = 0
    attribute_count = 0
    for element in root.iter():
        element_count += 1
        if element_count > MAX_SVG_ELEMENTS:
            raise ValueError("SVG logo is too complex")
        namespace, local_name = _svg_local_name(element.tag)
        if namespace not in {"", SVG_NAMESPACE} or local_name not in _SVG_ALLOWED_ELEMENTS:
            raise ValueError("SVG logo contains unsupported content")
        attribute_count += len(element.attrib)
        if attribute_count > MAX_SVG_ATTRIBUTES:
            raise ValueError("SVG logo is too complex")
        for raw_name, raw_value in element.attrib.items():
            attribute_namespace, attribute_name = _svg_local_name(raw_name)
            if attribute_namespace not in {"", XLINK_NAMESPACE}:
                raise ValueError("SVG logo contains unsupported attributes")
            lowered_name = attribute_name.lower()
            value = str(raw_value or "").strip()
            lowered_value = value.lower()
            if lowered_name.startswith("on") or lowered_name == "style":
                raise ValueError("SVG logo contains unsafe attributes")
            if lowered_name == "href" and value and not value.startswith("#"):
                raise ValueError("SVG logo cannot reference external content")
            if any(token in lowered_value for token in ("javascript:", "data:", "http:", "https:", "//", "@import")):
                raise ValueError("SVG logo cannot reference external content")
            for _quote, target in _SVG_URL_RE.findall(value):
                if not target.strip().startswith("#"):
                    raise ValueError("SVG logo cannot reference external content")

    ElementTree.register_namespace("", SVG_NAMESPACE)
    ElementTree.register_namespace("xlink", XLINK_NAMESPACE)
    return ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)


def _validate_logo(data: bytes) -> tuple[bytes, str, str]:
    if data.lstrip().startswith(b"<"):
        return _sanitize_svg_logo(data), "image/svg+xml", "svg"
    content_type, extension = _validate_raster_logo(data)
    return data, content_type, extension


async def _load_owned_institution(
    db: AsyncSession, user_id: int, institution_id: int
) -> Institution | None:
    return (
        await db.execute(
            select(Institution).where(
                Institution.id == institution_id,
                Institution.user_id == user_id,
            )
        )
    ).scalar_one_or_none()


@router.post("/institutions/{institution_id}/logo")
async def upload_institution_logo(
    institution_id: int,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Store a user-uploaded institution logo in-DB (owner-scoped); served back via
    GET /institutions/{id}/logo. Used by the redesigned Manual Institutions wizard."""
    institution = await _load_owned_institution(db, current_user.id, institution_id)
    if institution is None:
        raise HTTPException(status_code=404, detail="Institution not found")
    data = await file.read(MAX_LOGO_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(data) > MAX_LOGO_BYTES:
        raise HTTPException(status_code=413, detail="Logo too large (max 1 MB)")
    try:
        data, content_type, _ = _validate_logo(data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    institution.logo = data
    institution.logo_mime = content_type
    await db.commit()
    return {"status": "ok", "has_logo": True}


@router.get("/institutions/{institution_id}/logo")
async def get_institution_logo(
    institution_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    institution = await _load_owned_institution(db, current_user.id, institution_id)
    if institution is None or not institution.logo:
        raise HTTPException(status_code=404, detail="No logo")
    try:
        data, content_type, extension = _validate_logo(institution.logo)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="No valid logo") from exc
    return Response(
        content=data,
        media_type=content_type,
        headers={
            "Cache-Control": "private, max-age=3600",
            "Content-Disposition": f'inline; filename="institution-logo.{extension}"',
            "Content-Security-Policy": "default-src 'none'; style-src 'none'; sandbox",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.delete("/institutions/{institution_id}/logo")
async def delete_institution_logo(
    institution_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    institution = await _load_owned_institution(db, current_user.id, institution_id)
    if institution is None:
        raise HTTPException(status_code=404, detail="Institution not found")
    institution.logo = None
    institution.logo_mime = None
    await db.commit()
    return {"status": "ok", "has_logo": False}


@router.get("/institutions")
async def get_institutions(
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    result = await db.execute(
        select(Institution).where(
            Institution.user_id == current_user.id,
            Institution.enabled.is_(True),
            Institution.hidden.is_(False),
        )
    )
    institutions = result.scalars().all()
    return [
        {
            "id": i.id,
            "name": i.name,
            "type": i.type,
            "provider": i.provider,
            "category": i.category,
            "has_logo": i.logo is not None,
            "sync_status": i.sync_status or "ok",
            "added_at": (i.created_at.isoformat() + "Z") if i.created_at else None,
        }
        for i in institutions
    ]


@router.get("/institutions/all")
async def get_all_institutions(
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Get ALL institutions including hidden ones (for visibility dropdown)."""
    result = await db.execute(
        select(Institution).where(
            Institution.user_id == current_user.id,
            Institution.enabled.is_(True),
        ).order_by(Institution.created_at.asc(), Institution.id.asc())
    )
    institutions = result.scalars().all()
    return [
        {
            "id": i.id,
            "name": i.name,
            "type": i.type,
            "provider": i.provider,
            "sync_status": i.sync_status or "ok",
            "hidden": i.hidden or False,
            "has_logo": i.logo is not None,
            "added_at": (i.created_at.isoformat() + "Z") if i.created_at else None,
        }
        for i in institutions
    ]


@router.get("/institutions/scope")
async def get_scope_institutions(
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Get institutions with their accounts in one payload for visibility controls."""
    result = await db.execute(
        select(Institution).where(
            Institution.user_id == current_user.id,
            Institution.enabled.is_(True),
        ).order_by(Institution.created_at.asc(), Institution.id.asc())
    )
    institutions = result.scalars().all()
    accounts = await get_accounts_payload(db, current_user.id, include_hidden=True)
    accounts_by_institution: dict[int, list[dict]] = {}
    for account in accounts:
        institution_id = account.get("institution_id")
        if institution_id is None:
            continue
        accounts_by_institution.setdefault(institution_id, []).append(account)

    payload = []
    for institution in institutions:
        institution_accounts = [
            {
                **account,
                "institution": institution.name,
                "institution_id": institution.id,
                "provider": institution.provider,
            }
            for account in accounts_by_institution.get(institution.id, [])
        ]
        payload.append({
            "id": institution.id,
            "name": institution.name,
            "type": institution.type,
            "provider": institution.provider,
            "category": institution.category,
            "sync_status": institution.sync_status or "ok",
            "hidden": institution.hidden or False,
            "has_logo": institution.logo is not None,
            "added_at": (institution.created_at.isoformat() + "Z") if institution.created_at else None,
            "key": f"institution-{institution.id}",
            "accounts": institution_accounts,
            "accountIds": [account["id"] for account in institution_accounts],
        })
    return payload


@router.get("/institutions/{institution_id}/accounts")
async def get_institution_accounts(
    institution_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Get ALL accounts for an institution, including hidden ones (for settings modal)."""
    return await get_accounts_payload(
        db,
        current_user.id,
        institution_id=institution_id,
        include_hidden=True,
    )


@router.put("/institutions/{institution_id}/hidden")
async def toggle_institution_hidden(
    institution_id: int,
    body: dict,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    result = await db.execute(
        select(Institution).where(
            Institution.id == institution_id,
            Institution.user_id == current_user.id,
        )
    )
    institution = result.scalar_one_or_none()
    if not institution:
        return {"status": "error", "message": "Institution not found"}
    institution.hidden = body.get("hidden", False)
    await db.commit()
    return {"status": "ok", "hidden": institution.hidden}


@router.put("/institutions/{institution_id}/sync-status")
async def reset_sync_status(
    institution_id: int,
    body: dict,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    result = await db.execute(
        select(Institution).where(
            Institution.id == institution_id,
            Institution.user_id == current_user.id,
        )
    )
    institution = result.scalar_one_or_none()
    if not institution:
        return {"status": "error", "message": "Institution not found"}
    institution.sync_status = body.get("sync_status", "ok")
    await db.commit()
    return {"status": "ok"}


@router.get("/institutions/available")
async def get_available_institutions(
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Return supported providers the current user has not already connected."""
    connected_providers = set(
        (
            await db.execute(
                select(Institution.provider).where(
                    Institution.user_id == current_user.id,
                    Institution.type.in_(("api", "scraper")),
                )
            )
        ).scalars()
    )
    return [
        institution
        for institution in iter_available_institutions()
        if institution["provider"] not in connected_providers
    ]


@router.delete("/institutions/provider/{provider}/state")
async def remove_provider_add_state(
    provider: str,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Remove incomplete add-flow state for a provider."""
    provider = _provider_add_state_key(provider)
    async with sqlite_write_gate(), db.begin():
        result = await db.execute(
            select(Institution).where(
                Institution.provider == provider,
                Institution.user_id == current_user.id,
            )
        )
        institutions = result.scalars().all()
        removed_institutions = 0
        skipped_institutions = 0
        retained_institution = False
        for institution in institutions:
            if _is_pending_add_institution(institution):
                await delete_institution_and_accounts(
                    db,
                    current_user.id,
                    institution,
                    purge_diagnostics=False,
                )
                removed_institutions += 1
                continue
            if await _institution_has_accounts(db, current_user.id, institution.id):
                skipped_institutions += 1
                retained_institution = True
                continue
            await delete_institution_and_accounts(
                db,
                current_user.id,
                institution,
                purge_diagnostics=False,
            )
            removed_institutions += 1
        if not retained_institution and removed_institutions == 0:
            await delete_provider_state(db, current_user.id, provider)

    return {
        "status": "ok",
        "removed_institutions": removed_institutions,
        "skipped_institutions": skipped_institutions,
    }


@router.post("/institutions/provider/{provider}/add-confirm")
async def confirm_provider_add(
    provider: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Promote an add after accounts are ready, then queue transaction import."""
    provider = _provider_add_state_key(provider)
    source_sync_id = None
    attempt_id = None
    institution_id = None
    try:
        payload = await request.json()
        if isinstance(payload, dict):
            source_sync_id = str(payload.get("source_sync_id") or payload.get("sync_id") or "").strip() or None
            attempt_id = normalize_visible_auth_attempt_id(payload.get("attempt_id")) or None
            try:
                institution_id = int(payload.get("institution_id") or 0) or None
            except (TypeError, ValueError):
                institution_id = None
    except Exception:
        source_sync_id = None
    if institution_id is None:
        return {"status": "error", "message": "institution_id is required"}
    async with sqlite_write_gate(), db.begin():
        result = await db.execute(
            select(Institution).where(
                Institution.id == institution_id,
                Institution.provider == provider,
                Institution.user_id == current_user.id,
                Institution.enabled.is_(False),
                Institution.hidden.is_(True),
            )
        )
        institutions = result.scalars().all()
        confirmed_institutions = 0
        confirmed_institution_ids: list[int] = []
        removed_institutions = 0
        for institution in institutions:
            if await _institution_has_accounts(db, current_user.id, institution.id):
                institution.enabled = True
                institution.hidden = False
                confirmed_institutions += 1
                confirmed_institution_ids.append(int(institution.id))
                continue
            await delete_institution_and_accounts(db, current_user.id, institution)
            removed_institutions += 1

    transaction_import_job = None
    if confirmed_institution_ids:
        transaction_import_job = await enqueue_provider_transaction_import(
            current_user.id,
            _provider_transaction_import_key(provider),
            source_sync_id=source_sync_id,
            attempt_id=attempt_id,
            reason="add_connection",
            institution_id=institution_id,
        )

    return {
        "status": "ok",
        "confirmed_institutions": confirmed_institutions,
        "removed_institutions": removed_institutions,
        "transaction_import_job": transaction_import_job,
    }


@router.delete("/institutions/{institution_id}")
async def remove_institution(
    institution_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Remove an institution and all its accounts."""
    async with sqlite_write_gate(), db.begin():
        result = await db.execute(
            select(Institution).where(
                Institution.id == institution_id,
                Institution.user_id == current_user.id,
            )
        )
        institution = result.scalar_one_or_none()
        if not institution:
            return {"status": "error", "message": "Institution not found"}
        await delete_institution_and_accounts(db, current_user.id, institution)

    return {"status": "ok"}
