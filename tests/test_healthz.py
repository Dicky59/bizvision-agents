from httpx import ASGITransport, AsyncClient

from api.main import app


async def test_healthz_accepts_head() -> None:
    """`curl -I` and many uptime monitors probe with HEAD."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.head("/healthz")
    assert response.status_code == 200


async def test_healthz_returns_ok() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
