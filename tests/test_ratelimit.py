import pytest
from starlette.requests import Request

from api.errors import ApiError
from api.ratelimit import InMemoryLimiter, client_ip, enforce


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_window_blocks_then_recovers() -> None:
    clock = Clock()
    lim = InMemoryLimiter(clock)
    assert all(lim.hit("ip", 3, 60).allowed for _ in range(3))
    d = lim.hit("ip", 3, 60)
    assert not d.allowed and 1 <= d.retry_after <= 61
    clock.t += 30
    assert not lim.hit("ip", 3, 60).allowed
    clock.t += 31  # first hit is now outside the window
    assert lim.hit("ip", 3, 60).allowed


def test_keys_are_independent_and_rejections_not_counted() -> None:
    clock = Clock()
    lim = InMemoryLimiter(clock)
    for _ in range(2):
        assert lim.hit("a", 2, 60).allowed
    for _ in range(10):
        assert not lim.hit("a", 2, 60).allowed
    assert lim.hit("b", 2, 60).allowed
    clock.t += 61  # rejected attempts did not extend the block
    assert lim.hit("a", 2, 60).allowed


def test_enforce_raises_429_with_retry_after() -> None:
    lim = InMemoryLimiter(Clock())
    rules = [("chat:min:ip", 1, 60), ("chat:day:ip", 5, 86400)]
    enforce(lim, rules)
    with pytest.raises(ApiError) as e:
        enforce(lim, rules)
    assert e.value.status == 429 and int(e.value.headers["Retry-After"]) >= 1


def test_prune_drops_idle_keys() -> None:
    clock = Clock()
    lim = InMemoryLimiter(clock)
    lim.hit("old", 1, 60)
    clock.t += 90000
    lim.hit("new", 1, 60)
    lim.prune()
    assert set(lim._hits) == {"new"}  # pyright: ignore[reportPrivateUsage]


def _request(peer: str, xff: str | None) -> Request:
    headers = [(b"x-forwarded-for", xff.encode())] if xff else []
    return Request({"type": "http", "headers": headers, "client": (peer, 1234)})


def test_client_ip_ignores_xff_without_trusted_proxy() -> None:
    assert client_ip(_request("10.0.0.1", "6.6.6.6"), 0) == "10.0.0.1"


def test_client_ip_uses_rightmost_trusted_entry() -> None:
    # client-supplied spoof on the left, the proxy appended the real address on the right
    req = _request("10.0.0.1", "6.6.6.6, 198.51.100.7")
    assert client_ip(req, 1) == "198.51.100.7"
    assert client_ip(_request("10.0.0.1", "198.51.100.7, 172.16.0.2"), 2) == "198.51.100.7"


def test_client_ip_falls_back_to_peer_when_header_short() -> None:
    assert client_ip(_request("10.0.0.1", None), 1) == "10.0.0.1"
    assert client_ip(_request("10.0.0.1", "1.1.1.1"), 2) == "10.0.0.1"
