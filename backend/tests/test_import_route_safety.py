from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.api.routes import imports as import_routes


class _ScalarResult:
    def __init__(self, value) -> None:
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _Db:
    def __init__(self, owned_institution_id: int | None) -> None:
        self.execute = AsyncMock(return_value=_ScalarResult(owned_institution_id))
        self.rollback = AsyncMock()


class _Upload:
    def __init__(self, data: bytes, filename: str) -> None:
        self.data = data
        self.filename = filename
        self.read_sizes: list[int] = []

    async def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return self.data if size < 0 else self.data[:size]


class ImportRouteSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_routes_authorize_connection_before_reading_uploads(self) -> None:
        user = SimpleNamespace(id=7)
        cases = (
            (import_routes.import_ibkr_flex, _Upload(b"<xml />", "statement.xml")),
            (import_routes.import_questrade_statement, _Upload(b"%PDF-", "statement.pdf")),
        )
        for route, upload in cases:
            with self.subTest(route=route.__name__), self.assertRaises(HTTPException) as caught:
                await route(files=[upload], institution_id=42, db=_Db(None), current_user=user)
            self.assertEqual(caught.exception.status_code, 404)
            self.assertEqual(upload.read_sizes, [])

    async def test_routes_reject_excess_file_count_before_ownership_lookup(self) -> None:
        user = SimpleNamespace(id=7)
        cases = (
            (import_routes.import_ibkr_flex, "statement.xml"),
            (import_routes.import_questrade_statement, "statement.pdf"),
        )
        for route, filename in cases:
            db = _Db(42)
            uploads = [
                _Upload(b"", filename)
                for _ in range(import_routes.MAX_IMPORT_FILES + 1)
            ]
            with self.subTest(route=route.__name__), self.assertRaises(HTTPException) as caught:
                await route(files=uploads, institution_id=42, db=db, current_user=user)
            self.assertEqual(caught.exception.status_code, 413)
            db.execute.assert_not_awaited()

    async def test_ibkr_route_enforces_aggregate_size_before_import(self) -> None:
        first = _Upload(b"<a", "first.xml")
        second = _Upload(b"bcde", "second.xml")
        importer = AsyncMock()
        with (
            patch.object(import_routes, "MAX_AGGREGATE_UPLOAD_BYTES", 5),
            patch.object(import_routes, "import_flex_transactions", new=importer),
            self.assertRaises(HTTPException) as caught,
        ):
            await import_routes.import_ibkr_flex(
                files=[first, second],
                institution_id=42,
                db=_Db(42),
                current_user=SimpleNamespace(id=7),
            )
        self.assertEqual(caught.exception.status_code, 413)
        self.assertEqual(second.read_sizes, [4])
        importer.assert_not_awaited()

    async def test_questrade_route_enforces_aggregate_size_before_import(self) -> None:
        first = _Upload(b"%P", "first.pdf")
        second = _Upload(b"DF--", "second.pdf")
        importer = AsyncMock()
        with (
            patch.object(import_routes, "MAX_AGGREGATE_UPLOAD_BYTES", 5),
            patch(
                "app.services.questrade_statement_import.import_questrade_statements",
                new=importer,
            ),
            self.assertRaises(HTTPException) as caught,
        ):
            await import_routes.import_questrade_statement(
                files=[first, second],
                institution_id=42,
                db=_Db(42),
                current_user=SimpleNamespace(id=7),
            )
        self.assertEqual(caught.exception.status_code, 413)
        self.assertEqual(second.read_sizes, [4])
        importer.assert_not_awaited()

    async def test_questrade_route_closes_precheck_transaction_before_upload(self) -> None:
        events = []
        db = _Db(42)
        db.rollback.side_effect = lambda: events.append("rollback")
        upload = _Upload(b"%PDF-", "statement.pdf")
        original_read = upload.read

        async def tracked_read(size=-1):
            events.append("read")
            return await original_read(size)

        async def importer(*_args, **_kwargs):
            events.append("import")
            return {"status": "ok", "imported": 1}

        upload.read = tracked_read
        with patch(
            "app.services.questrade_statement_import.import_questrade_statements",
            new=importer,
        ):
            result = await import_routes.import_questrade_statement(
                files=[upload],
                institution_id=42,
                db=db,
                current_user=SimpleNamespace(id=7),
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(events, ["rollback", "read", "import"])

    async def test_ibkr_route_decodes_xml_off_event_loop(self) -> None:
        functions = []

        async def run_in_thread(function, *args, **kwargs):
            functions.append(function)
            return function(*args, **kwargs)

        importer = AsyncMock(return_value={"status": "ok", "imported": 1})
        with (
            patch.object(import_routes.asyncio, "to_thread", side_effect=run_in_thread),
            patch.object(import_routes, "import_flex_transactions", new=importer),
        ):
            result = await import_routes.import_ibkr_flex(
                files=[_Upload(b"<FlexQueryResponse />", "statement.xml")],
                institution_id=42,
                db=_Db(42),
                current_user=SimpleNamespace(id=7),
            )

        self.assertEqual(result["status"], "ok")
        self.assertIn(import_routes._decode_xml_upload, functions)
        importer.assert_awaited_once_with("<FlexQueryResponse />", 7, 42)

    async def test_csv_upload_decodes_off_event_loop(self) -> None:
        functions = []

        async def run_in_thread(function, *args, **kwargs):
            functions.append(function)
            return function(*args, **kwargs)

        with patch.object(import_routes.asyncio, "to_thread", side_effect=run_in_thread):
            decoded = await import_routes._read_csv_upload(
                _Upload(b"Date,Amount\n2026-08-13,1\n", "transactions.csv")
            )

        self.assertIn(import_routes._decode_csv_upload, functions)
        self.assertEqual(decoded, "Date,Amount\n2026-08-13,1\n")


if __name__ == "__main__":
    unittest.main()
