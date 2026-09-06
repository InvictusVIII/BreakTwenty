from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response

from app.api.routes import (
    CurrentUser,
    _parse_csv_ints,
    _parse_csv_strings,
    _parse_iso_date,
    get_current_user,
    get_db,
)
from app.services import csv_export

router = APIRouter()


def _csv_response(filename: str, csv_text: str) -> Response:
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/export/transactions.csv")
async def export_transactions(
    account_ids: str | None = Query(None),
    institution_ids: str | None = Query(None),
    type: str | None = Query(None, max_length=1_024),
    category_ids: str | None = Query(None),
    symbol: str | None = Query(None, max_length=64),
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    include_hidden: bool = Query(False),
    include_internal_ids: bool = Query(False),
    db=Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    _parse_csv_ints(account_ids)
    _parse_csv_ints(institution_ids)
    _parse_csv_ints(category_ids)
    _parse_csv_strings(type, field_name="type")
    _parse_iso_date(start_date, field_name="start_date")
    _parse_iso_date(end_date, field_name="end_date")
    filename, csv_text = await csv_export.export_transactions_csv(
        db,
        current_user.id,
        account_ids=account_ids,
        institution_ids=institution_ids,
        type=type,
        category_ids=category_ids,
        symbol=symbol,
        start_date=start_date,
        end_date=end_date,
        include_hidden=include_hidden,
        include_internal_ids=include_internal_ids,
    )
    return _csv_response(filename, csv_text)


@router.get("/export/balances.csv")
async def export_balances(
    include_hidden: bool = Query(False),
    include_internal_ids: bool = Query(False),
    latest_only: bool = Query(False),
    db=Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    filename, csv_text = await csv_export.export_balances_csv(
        db,
        current_user.id,
        include_hidden=include_hidden,
        include_internal_ids=include_internal_ids,
        latest_only=latest_only,
    )
    return _csv_response(filename, csv_text)


@router.get("/export/holdings.csv")
async def export_holdings(
    include_hidden: bool = Query(False),
    include_internal_ids: bool = Query(False),
    category: str | None = Query(None, description="Scope to sub-tab(s): spot,cash | option | crypto"),
    account_ids: str | None = Query(None, description="Comma-separated account ids (institution filter)"),
    db=Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    _parse_csv_ints(account_ids)
    normalized_category = (category or "").strip()
    filename_slug = {
        "": "all-holdings",
        "spot,cash": "holdings",
        "option": "options",
        "crypto": "crypto",
    }.get(normalized_category, "holdings")
    filename, csv_text = await csv_export.export_holdings_csv(
        db,
        current_user.id,
        include_hidden=include_hidden,
        include_internal_ids=include_internal_ids,
        category=category,
        account_ids=account_ids,
        filename_slug=filename_slug,
    )
    return _csv_response(filename, csv_text)


@router.get("/export/networth-history.csv")
async def export_networth_history(
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    include_hidden: bool = Query(False),
    db=Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Net-worth history; account scope follows hidden state (the Dashboard scope
    toggle), and an optional `start_date`/`end_date` window matches the chart's
    on-screen timeframe."""
    _parse_iso_date(start_date, field_name="start_date")
    _parse_iso_date(end_date, field_name="end_date")
    filename, csv_text = await csv_export.export_networth_history_csv(
        db,
        current_user.id,
        start_date=start_date,
        end_date=end_date,
        include_hidden=include_hidden,
    )
    return _csv_response(filename, csv_text)


@router.get("/export/all.zip")
async def export_all_data(
    db=Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    filename, archive_bytes = await csv_export.export_all_data_archive(
        db,
        current_user.id,
    )
    return Response(
        content=archive_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/export/income.csv")
async def export_income(
    kind: str | None = Query(None, description="dividends | interest; default = all income"),
    account_ids: str | None = Query(None, description="Comma-separated account ids (institution filter)"),
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    db=Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Dividends export aggregates by position × month in native currency; interest
    is summarized per month in the primary currency, split US/CAD (same shape as the
    dividends monthly summary). Scoped to the selected accounts and the Income
    timeline. Raw income rows live in the transactions export."""
    # The Income view's end bound is inclusive of the whole day; widen a date-only
    # end to end-of-day so the export matches the on-screen row set.
    _parse_csv_ints(account_ids)
    _parse_iso_date(start_date, field_name="start_date")
    _parse_iso_date(end_date, field_name="end_date")
    normalized_end = end_date
    if end_date and len(end_date.strip()) == 10 and "T" not in end_date:
        normalized_end = f"{end_date.strip()}T23:59:59.999999"
    normalized_kind = (kind or "").strip().lower()
    filename_slug = {
        "dividends": "dividends",
        "interest": "interest",
    }.get(normalized_kind, "income")
    if normalized_kind == "interest":
        filename, csv_text = await csv_export.export_interest_monthly_csv(
            db,
            current_user.id,
            account_ids=account_ids,
            start_date=start_date,
            end_date=normalized_end,
            filename_slug=filename_slug,
        )
    elif normalized_kind == "dividends":
        # One combined file (two stacked tables) so the click yields a single Save
        # dialog instead of two separate downloads.
        filename, csv_text = await csv_export.export_dividends_combined_csv(
            db,
            current_user.id,
            account_ids=account_ids,
            start_date=start_date,
            end_date=normalized_end,
        )
    else:
        filename, csv_text = await csv_export.export_income_by_position_csv(
            db,
            current_user.id,
            kind=kind,
            account_ids=account_ids,
            start_date=start_date,
            end_date=normalized_end,
            filename_slug=filename_slug,
        )
    return _csv_response(filename, csv_text)
