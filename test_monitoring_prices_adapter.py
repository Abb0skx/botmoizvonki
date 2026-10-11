import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from monitoring.adapters.prices import PriceAdapter


class _ClientContext:
    def __init__(self, request):
        self.request = request

    async def __aenter__(self):
        return SimpleNamespace(request=self.request)

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class PriceAdapterConnectRetryTests(unittest.IsolatedAsyncioTestCase):
    def adapter(self):
        settings = SimpleNamespace(
            price_base_url="http://price.internal:8080",
            price_admin_service_token="admin-token",
            price_management_configured=lambda: True,
        )
        return PriceAdapter(settings)

    async def test_admin_post_retries_connect_timeout_with_same_request(self):
        request = httpx.Request("POST", "http://price.internal:8080/save")
        response = httpx.Response(
            200,
            content=b'{"status":"applied"}',
            headers={"content-type": "application/json"},
        )
        send = AsyncMock(side_effect=[
            httpx.ConnectTimeout("connect timed out", request=request),
            response,
        ])
        with (
            patch(
                "monitoring.adapters.prices.httpx.AsyncClient",
                return_value=_ClientContext(send),
            ),
            patch(
                "monitoring.adapters.prices.asyncio.sleep",
                new=AsyncMock(),
            ) as sleep,
        ):
            result = await self.adapter().admin_request(
                "POST",
                "/price/api/v1/entry/save/1622297209",
                body=b'{"changes":[]}',
                idempotency_key="a7ffece0-0d6e-473a-8029-6c38e34ce57b",
            )

        self.assertEqual(result, (200, b'{"status":"applied"}', "application/json"))
        self.assertEqual(send.await_count, 2)
        for call in send.await_args_list:
            self.assertEqual(call.args[:2], (
                "POST",
                "http://price.internal:8080/price/api/v1/entry/save/1622297209",
            ))
            self.assertEqual(call.kwargs["content"], b'{"changes":[]}')
            self.assertEqual(
                call.kwargs["headers"]["Idempotency-Key"],
                "a7ffece0-0d6e-473a-8029-6c38e34ce57b",
            )
        sleep.assert_awaited_once_with(0.1)

    async def test_admin_post_does_not_retry_ambiguous_read_timeout(self):
        request = httpx.Request("POST", "http://price.internal:8080/save")
        send = AsyncMock(side_effect=httpx.ReadTimeout(
            "response timed out", request=request
        ))
        with (
            patch(
                "monitoring.adapters.prices.httpx.AsyncClient",
                return_value=_ClientContext(send),
            ),
            patch(
                "monitoring.adapters.prices.asyncio.sleep",
                new=AsyncMock(),
            ) as sleep,
        ):
            with self.assertRaises(httpx.ReadTimeout):
                await self.adapter().admin_request(
                    "POST",
                    "/price/api/v1/entry/save/1622297209",
                    body=b'{"changes":[]}',
                    idempotency_key="a7ffece0-0d6e-473a-8029-6c38e34ce57b",
                )

        self.assertEqual(send.await_count, 1)
        sleep.assert_not_awaited()

    async def test_admin_post_stops_after_three_connect_failures(self):
        request = httpx.Request("POST", "http://price.internal:8080/save")
        send = AsyncMock(side_effect=httpx.ConnectError(
            "connection failed", request=request
        ))
        with (
            patch(
                "monitoring.adapters.prices.httpx.AsyncClient",
                return_value=_ClientContext(send),
            ),
            patch(
                "monitoring.adapters.prices.asyncio.sleep",
                new=AsyncMock(),
            ) as sleep,
        ):
            with self.assertRaises(httpx.ConnectError):
                await self.adapter().admin_request(
                    "POST",
                    "/price/api/v1/entry/save/1622297209",
                    body=b'{"changes":[]}',
                    idempotency_key="a7ffece0-0d6e-473a-8029-6c38e34ce57b",
                )

        self.assertEqual(send.await_count, 3)
        self.assertEqual(
            [call.args for call in sleep.await_args_list],
            [(0.1,), (0.2,)],
        )


if __name__ == "__main__":
    unittest.main()
