import asyncio

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes import CurrentUser, get_current_user, get_db
from app.models import Account, Institution
from app.services import csv_import
from app.services.flex_query import import_flex_transactions

# 50 MB cap for uploaded XML payloads
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
# 25 MB cap for uploaded CSV payloads
MAX_CSV_BYTES = 25 * 1024 * 1024
# Monthly statement imports routinely span many years/accounts. Keep a count guard
# for pathological multipart requests, but let the byte caps do the real work.
MAX_IMPORT_FILES = 240
MAX_AGGREGATE_UPLOAD_BYTES = 100 * 1024 * 1024
SUPPORTED_IMPORT_DATASETS = ("balances", "transactions")

router = APIRouter()


async def _require_import_institution(
    db: AsyncSession,
    user_id: int,
    institution_id: int,
    *,
    provider: str,
) -> None:
    institution = (
        await db.execute(
            select(Institution.id).where(
                Institution.id == institution_id,
                Institution.user_id == user_id,
                Institution.provider == provider,
                Institution.enabled.is_(True),
            )
        )
    ).scalar_one_or_none()
    if institution is None:
        raise HTTPException(status_code=404, detail="Enabled institution connection not found")


async def _require_manual_import_account(
    db: AsyncSession,
    user_id: int,
    account_id: int,
) -> None:
    account = (
        await db.execute(
            select(Account.id)
            .join(Institution, Institution.id == Account.institution_id)
            .where(
                Account.id == account_id,
                Account.user_id == user_id,
                Institution.user_id == user_id,
                Institution.provider == csv_import.MANUAL_INSTITUTION_PROVIDER,
                Institution.enabled.is_(True),
            )
        )
    ).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Manual import account not found")


def _validate_upload_count(files: list[UploadFile]) -> None:
    if not files:
        raise HTTPException(status_code=400, detail="At least one file is required")
    if len(files) > MAX_IMPORT_FILES:
        raise HTTPException(
            status_code=413,
            detail=f"A maximum of {MAX_IMPORT_FILES} files can be uploaded at once",
        )


async def _read_bounded_upload(
    upload: UploadFile,
    *,
    per_file_limit: int,
    aggregate_remaining: int,
) -> bytes:
    read_limit = min(per_file_limit, aggregate_remaining)
    if read_limit <= 0:
        raise HTTPException(status_code=413, detail="Combined upload size exceeds the allowed maximum")
    data = await upload.read(read_limit + 1)
    if len(data) > per_file_limit:
        raise HTTPException(
            status_code=413,
            detail=f"{upload.filename or 'File'} exceeds max size of {per_file_limit // (1024 * 1024)}MB",
        )
    if len(data) > aggregate_remaining:
        raise HTTPException(status_code=413, detail="Combined upload size exceeds the allowed maximum")
    return data


def _decode_xml_upload(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


@router.post("/import/ibkr-flex")
async def import_ibkr_flex(
    files: list[UploadFile] = File(...),
    institution_id: int = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Accept one or more IBKR Flex Query XML file uploads: import each statement's
    transactions (trades + cash) and backfill daily account-value history from its
    EquitySummaryInBase rows. A file can span the account's full statement range (beyond
    the 365-day sync cap), extending the net-worth graph back accordingly; a Flex account
    number matching no live or already-imported account becomes an ``is_imported`` (defunct)
    account. Does not touch holdings. Counts are aggregated across all files, mirroring the
    Questrade multi-file statement-import contract.
    """
    _validate_upload_count(files)
    await _require_import_institution(db, current_user.id, institution_id, provider="ibkr")
    xml_payloads: list[str] = []
    uploaded_bytes = 0
    for upload in files:
        name = (upload.filename or "").lower()
        if name and not name.endswith(".xml"):
            raise HTTPException(status_code=400, detail=f"{upload.filename}: file must be an XML document")
        data = await _read_bounded_upload(
            upload,
            per_file_limit=MAX_UPLOAD_BYTES,
            aggregate_remaining=MAX_AGGREGATE_UPLOAD_BYTES - uploaded_bytes,
        )
        if not data:
            continue
        uploaded_bytes += len(data)
        xml_payloads.append(await asyncio.to_thread(_decode_xml_upload, data))
    if not xml_payloads:
        raise HTTPException(status_code=400, detail="No readable XML files were uploaded")

    totals = {"imported": 0, "skipped": 0, "errors": 0, "accounts_created": 0, "accounts_updated": 0}
    warnings: list[str] = []
    succeeded = 0
    for xml_text in xml_payloads:
        result = await import_flex_transactions(xml_text, current_user.id, institution_id)
        if result.get("status") == "error":
            warnings.append(result.get("message", "Import failed"))
            continue
        succeeded += 1
        for key in totals:
            totals[key] += result.get(key, 0)
    if succeeded == 0:
        raise HTTPException(status_code=400, detail="; ".join(warnings) or "Import failed")
    return {"status": "ok", **totals, "warnings": warnings}


# 25 MB cap per uploaded statement PDF
MAX_PDF_BYTES = 25 * 1024 * 1024


@router.post("/import/questrade-statement")
async def import_questrade_statement(
    files: list[UploadFile] = File(...),
    institution_id: int = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Accept one or more Questrade monthly statement PDFs and backfill month-end balance
    history. Statements for a known account number extend that account's history
    (idempotent per local day); unknown account numbers become ``is_imported`` accounts
    under the user's Questrade institution. Mirrors the IBKR Flex import contract,
    returning import/skip/error counts.
    """
    _validate_upload_count(files)
    await _require_import_institution(db, current_user.id, institution_id, provider="questrade")
    await db.rollback()
    items: list[tuple[str, bytes]] = []
    uploaded_bytes = 0
    for upload in files:
        name = upload.filename or "statement.pdf"
        if not name.lower().endswith(".pdf"):
            raise HTTPException(status_code=400, detail=f"{name}: file must be a PDF document")
        data = await _read_bounded_upload(
            upload,
            per_file_limit=MAX_PDF_BYTES,
            aggregate_remaining=MAX_AGGREGATE_UPLOAD_BYTES - uploaded_bytes,
        )
        if not data:
            continue
        uploaded_bytes += len(data)
        items.append((name, data))
    if not items:
        raise HTTPException(status_code=400, detail="No readable PDF files were uploaded")

    from app.services.questrade_statement_import import import_questrade_statements

    result = await import_questrade_statements(
        db,
        current_user.id,
        institution_id,
        items,
    )
    if result.get("status") == "error":
        raise HTTPException(status_code=400, detail=result.get("message", "Import failed"))
    return result


async def _read_csv_upload(file: UploadFile) -> str:
    filename = (file.filename or "").lower()
    if filename and not filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="File must be a .csv document")
    data = await file.read(MAX_CSV_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    if len(data) > MAX_CSV_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds max size of {MAX_CSV_BYTES // (1024 * 1024)}MB",
        )
    try:
        return await asyncio.to_thread(_decode_csv_upload, data)
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="Could not decode file as text") from exc


def _decode_csv_upload(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")


async def _resolve_import_dataset(dataset: str | None, csv_text: str) -> str:
    if dataset:
        normalized = dataset.strip().lower()
        if normalized not in SUPPORTED_IMPORT_DATASETS:
            raise HTTPException(status_code=400, detail=f"Unsupported import dataset: {dataset}")
        return normalized
    headers, _ = await asyncio.to_thread(csv_import.parse_csv, csv_text)
    detected = csv_import.detect_dataset(headers)
    if detected not in SUPPORTED_IMPORT_DATASETS:
        raise HTTPException(
            status_code=400,
            detail="Could not detect dataset from CSV headers; pass dataset=balances or dataset=transactions",
        )
    return detected


@router.post("/import/account/preview")
async def import_account_preview(
    file: UploadFile = File(...),
    account_id: int = Form(...),
    dataset: str | None = Form(None),
    db=Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Dry-run a CSV import into a manual account (counts/warnings, nothing persisted)."""
    await _require_manual_import_account(db, current_user.id, account_id)
    csv_text = await _read_csv_upload(file)
    resolved = await _resolve_import_dataset(dataset, csv_text)
    return await csv_import.import_csv(
        db, current_user.id, account_id=account_id, dataset=resolved, csv_text=csv_text, commit=False,
    )


@router.post("/import/account/commit")
async def import_account_commit(
    file: UploadFile = File(...),
    account_id: int = Form(...),
    dataset: str | None = Form(None),
    db=Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Commit a CSV import into a manual account (transactions or balances)."""
    await _require_manual_import_account(db, current_user.id, account_id)
    csv_text = await _read_csv_upload(file)
    resolved = await _resolve_import_dataset(dataset, csv_text)
    return await csv_import.import_csv(
        db, current_user.id, account_id=account_id, dataset=resolved, csv_text=csv_text, commit=True,
    )
