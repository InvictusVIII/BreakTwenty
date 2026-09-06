from __future__ import annotations

import csv
import inspect
import io
import struct
import unittest
import zlib

from fastapi import HTTPException

from app.api.routes import _parse_csv_ints, _parse_iso_date
from app.api.routes.accounts import router as accounts_router
from app.api.routes.categories import router as categories_router
from app.api.routes.recurring import router as recurring_router
from app.api.routes.transactions import get_transactions, router as transactions_router
from app.api.routes.imports import (
    MAX_AGGREGATE_UPLOAD_BYTES,
    MAX_IMPORT_FILES,
    _read_bounded_upload,
    _validate_upload_count,
)
from app.api.routes.institutions import _validate_logo, _validate_raster_logo
from app.api.routes.settings import _validate_settings_payload
from app.services.csv_export import _render_csv
from app.services import event_bus


class _Upload:
    def __init__(self, data: bytes, filename: str = "upload.bin") -> None:
        self.data = data
        self.filename = filename
        self.read_sizes: list[int] = []

    async def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return self.data if size < 0 else self.data[:size]


def _png(width: int, height: int) -> bytes:
    signature = b"\x89PNG\r\n\x1a\n"

    def chunk(kind: bytes, data: bytes) -> bytes:
        payload = kind + data
        return struct.pack(">I", len(data)) + payload + struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    scanlines = (b"\x00" + b"\x00" * (width * 4)) * height if width * height <= 4096 else b"\x00"
    return signature + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(scanlines)) + chunk(b"IEND", b"")


class ApiDataSafetyTests(unittest.IsolatedAsyncioTestCase):
    def test_csv_integer_filters_reject_partial_or_oversized_input(self) -> None:
        self.assertEqual(_parse_csv_ints("1,-1,42"), [1, -1, 42])
        for raw in ("1,garbage,2", "1,,2", ",1"):
            with self.subTest(raw=raw), self.assertRaises(HTTPException) as caught:
                _parse_csv_ints(raw)
            self.assertEqual(caught.exception.status_code, 422)
        with self.assertRaises(HTTPException):
            _parse_csv_ints(",".join(str(value) for value in range(251)))

    def test_date_filters_reject_values_that_would_previously_widen_queries(self) -> None:
        self.assertEqual(_parse_iso_date("2026-08-13").isoformat(), "2026-08-13")
        for raw in ("not-a-date", "2026-02-30", " 2026-08-13", "2026-08-13T12:00:00"):
            with self.subTest(raw=raw), self.assertRaises(HTTPException) as caught:
                _parse_iso_date(raw, field_name="start_date")
            self.assertEqual(caught.exception.status_code, 422)

    def test_dead_routes_and_singular_transaction_filters_are_absent(self) -> None:
        route_methods = {
            (route.path, method)
            for router in (accounts_router, categories_router, recurring_router, transactions_router)
            for route in router.routes
            for method in (route.methods or set())
        }
        for route in (
            ("/allocation", "GET"),
            ("/categories/{category_id}", "GET"),
            ("/recurring/{series_id}/confirm", "POST"),
            ("/recurring/{series_id}/restore", "POST"),
            ("/transactions/manual/{transaction_id}", "PATCH"),
            ("/transactions/{transaction_id}/category", "PATCH"),
        ):
            self.assertNotIn(route, route_methods)
        self.assertIn(("/categories/{category_id}", "PATCH"), route_methods)
        self.assertIn(("/transactions/manual/{transaction_id}", "DELETE"), route_methods)
        parameters = inspect.signature(get_transactions).parameters
        self.assertNotIn("account_id", parameters)
        self.assertNotIn("institution_id", parameters)
        self.assertNotIn("category_id", parameters)

    def test_settings_allowlist_rejects_unknown_reserved_and_bad_controls(self) -> None:
        self.assertEqual(
            _validate_settings_payload(
                {
                    "primary_currency": "usd",
                    "user_timezone": "America/Toronto",
                    "user_time_format": "12H",
                }
            ),
            {
                "primary_currency": "USD",
                "user_timezone": "America/Toronto",
                "user_time_format": "12h",
            },
        )
        for payload in (
            {"unknown_plaintext_secret": "secret"},
            {"market_data_api_key": "secret"},
            {"institution_id": 4},
            {"user_timezone": "Not/A_Timezone"},
        ):
            with self.subTest(payload=payload), self.assertRaises(HTTPException) as caught:
                _validate_settings_payload(payload)
            self.assertEqual(caught.exception.status_code, 422)

    def test_csv_export_neutralizes_formula_text_without_touching_numbers(self) -> None:
        rendered = _render_csv(
            ["description", "user_notes", "amount"],
            ["Description", "Notes", "Amount"],
            [{"description": "=HYPERLINK(\"https://bad\")", "user_notes": "  +SUM(1,2)", "amount": "-12.34"}],
        )
        rows = list(csv.reader(io.StringIO(rendered.lstrip("\ufeff"))))
        self.assertEqual(rows[1][0], "'=HYPERLINK(\"https://bad\")")
        self.assertEqual(rows[1][1], "'  +SUM(1,2)")
        self.assertEqual(rows[1][2], "-12.34")

    async def test_upload_reads_are_bounded_and_file_count_is_capped(self) -> None:
        upload = _Upload(b"x" * 12)
        with self.assertRaises(HTTPException) as caught:
            await _read_bounded_upload(upload, per_file_limit=10, aggregate_remaining=100)
        self.assertEqual(caught.exception.status_code, 413)
        self.assertEqual(upload.read_sizes, [11])

        uploads = [_Upload(b"") for _ in range(MAX_IMPORT_FILES + 1)]
        with self.assertRaises(HTTPException) as caught:
            _validate_upload_count(uploads)
        self.assertEqual(caught.exception.status_code, 413)

        multi_year_statement_batch = [_Upload(b"") for _ in range(120)]
        _validate_upload_count(multi_year_statement_batch)

        aggregate_upload = _Upload(b"x" * 6)
        with self.assertRaises(HTTPException):
            await _read_bounded_upload(
                aggregate_upload,
                per_file_limit=10,
                aggregate_remaining=5,
            )
        self.assertLess(MAX_IMPORT_FILES * 10, MAX_AGGREGATE_UPLOAD_BYTES)

    def test_logo_validation_accepts_bounded_raster_and_safe_svg(self) -> None:
        self.assertEqual(_validate_raster_logo(_png(64, 64)), ("image/png", "png"))
        safe_svg = b"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><path d='M0 0h64v64H0z'/></svg>"
        sanitized, media_type, extension = _validate_logo(safe_svg)
        self.assertEqual((media_type, extension), ("image/svg+xml", "svg"))
        self.assertIn(b"<path", sanitized)
        for unsafe_svg in (
            b"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><script>alert(1)</script></svg>",
            b"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><use href='https://example.com/x.svg#logo'/></svg>",
            b"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><path onload='alert(1)'/></svg>",
            b"<!DOCTYPE svg [<!ENTITY secret SYSTEM 'file:///etc/passwd'>]><svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><text>&secret;</text></svg>",
        ):
            with self.subTest(unsafe_svg=unsafe_svg), self.assertRaises(ValueError):
                _validate_logo(unsafe_svg)
        with self.assertRaises(ValueError):
            _validate_raster_logo(_png(4096, 4096))

    def test_event_bus_subscription_admission_is_database_backed_and_async(self) -> None:
        self.assertTrue(inspect.iscoroutinefunction(event_bus.subscribe))
        self.assertTrue(inspect.iscoroutinefunction(event_bus.unsubscribe))
        self.assertTrue(inspect.iscoroutinefunction(event_bus._acquire_event_stream_lease))


if __name__ == "__main__":
    unittest.main()
