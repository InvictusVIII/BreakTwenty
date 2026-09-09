import unittest

import httpx
from fastapi import FastAPI, Response

from app.api_cache_policy import ApiNoStoreMiddleware


class ApiCachePolicyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        app = FastAPI()

        @app.get("/api/data")
        async def api_data():
            return {"status": "ok"}

        @app.get("/api/previously-cacheable")
        async def previously_cacheable():
            return Response(
                content=b"asset",
                media_type="application/octet-stream",
                headers={"Cache-Control": "private, max-age=3600"},
            )

        @app.get("/asset")
        async def asset():
            return Response(content=b"asset", headers={"Cache-Control": "max-age=3600"})

        app.add_middleware(ApiNoStoreMiddleware)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()

    async def test_marks_api_responses_no_store(self) -> None:
        response = await self.client.get("/api/data")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")

    async def test_overrides_route_cache_headers_for_api_responses(self) -> None:
        response = await self.client.get("/api/previously-cacheable")

        self.assertEqual(response.headers["cache-control"], "no-store")

    async def test_leaves_non_api_cache_policy_unchanged(self) -> None:
        response = await self.client.get("/asset")

        self.assertEqual(response.headers["cache-control"], "max-age=3600")


if __name__ == "__main__":
    unittest.main()
