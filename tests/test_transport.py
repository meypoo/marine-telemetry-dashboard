"""Offline tests for the transport layer, using httpx.MockTransport.

The retry/backoff/mirror-rotation behaviour in ``_BaseClient._fetch`` is what
keeps the dashboard alive when a public mirror sheds load, and the telemetry it
records is what the console renders. Both were previously only reachable
through the live end-to-end test. These drive them against a scripted transport
instead — deterministic, and no network.

Backoff sleeps are patched out so the suite stays fast; the retry *decisions*
are what is under test, not the wall-clock spacing.
"""

from __future__ import annotations

import asyncio
import sys
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import api_clients  # noqa: E402
from api_clients import (  # noqa: E402
    REGIONS_BY_CODE, ApiError, ErddapClient, OverpassClient, TelemetryEvent,
    TelemetrySink, _BaseClient, _haversine_km, _linear_trend,
)


def _run(coro):
    return asyncio.run(coro)


class _NoSleep:
    """Patch asyncio.sleep to a no-op for the duration of a test."""

    def __enter__(self):
        self._original = api_clients.asyncio.sleep

        async def _instant(_delay):  # noqa: ANN001, ANN202
            return None

        api_clients.asyncio.sleep = _instant
        return self

    def __exit__(self, *_exc):
        api_clients.asyncio.sleep = self._original
        return False


async def _with_client(handler, fn, **client_kwargs):
    """Run ``fn(client, sink)`` against a MockTransport-backed AsyncClient."""
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, **client_kwargs) as client:
        sink = TelemetrySink()
        return await fn(client, sink), sink


def test_successful_fetch_records_one_event() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    async def body(client, sink):
        c = _BaseClient(client, sink)
        return await c._fetch("https://example.test/data", "probe")

    response, sink = _run(_with_client(handler, body))
    assert response.status_code == 200
    assert sink.total_requests == 1, f"expected 1 telemetry event, got {sink.total_requests}"
    assert sink.failed_requests == 0
    event = sink.events[0]
    assert event.ok and event.attempts == 1 and event.status == 200
    assert event.payload_bytes > 0, "payload size should be measured, not zero"


def test_retries_transient_status_then_succeeds() -> None:
    """A 503 must be retried on the same host rather than failing outright."""
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    async def body(client, sink):
        c = _BaseClient(client, sink)
        return await c._fetch("https://example.test/data", "probe")

    with _NoSleep():
        response, sink = _run(_with_client(handler, body))

    assert response.status_code == 200
    assert calls["n"] == 3, f"expected 3 attempts, made {calls['n']}"
    # Every attempt is recorded, including the two failures.
    assert sink.total_requests == 3
    assert sink.failed_requests == 2
    assert sink.events[-1].attempts == 3


def test_rotates_to_mirror_after_exhausting_first_host() -> None:
    """Overpass-style behaviour: stop hammering a host that said it is busy."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if request.url.host == "primary.test":
            return httpx.Response(504)
        return httpx.Response(200, json={"ok": True})

    async def body(client, sink):
        c = _BaseClient(client, sink, max_attempts=2)
        return await c._fetch(
            "https://primary.test/api", "probe",
            mirrors=["https://mirror.test/api"],
        )

    with _NoSleep():
        response, sink = _run(_with_client(handler, body))

    assert response.status_code == 200
    assert seen[:2] == ["primary.test", "primary.test"], (
        f"should exhaust max_attempts on the primary first, saw {seen}"
    )
    assert "mirror.test" in seen, "never rotated to the configured mirror"
    assert sink.failed_requests == 2, "both primary failures should be recorded"


def test_exhausting_every_endpoint_raises_api_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(504)

    async def body(client, sink):
        c = _BaseClient(client, sink, max_attempts=2)
        return await c._fetch(
            "https://primary.test/api", "probe",
            mirrors=["https://mirror.test/api"],
        )

    with _NoSleep():
        try:
            _run(_with_client(handler, body))
        except ApiError as exc:
            assert "probe" in str(exc), f"error should name the label: {exc}"
            return
    raise AssertionError("exhausting all endpoints should raise ApiError")


def test_transport_error_is_retried_and_recorded() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, json={"ok": True})

    async def body(client, sink):
        c = _BaseClient(client, sink)
        return await c._fetch("https://example.test/data", "probe")

    with _NoSleep():
        response, sink = _run(_with_client(handler, body))

    assert response.status_code == 200
    assert sink.total_requests == 2, "the failed transport attempt must be logged too"
    failed = sink.events[0]
    assert not failed.ok and failed.status is None
    assert "ConnectError" in (failed.error or "")


def test_non_retryable_status_fails_immediately() -> None:
    """A 404 is a real answer, not congestion — it must not be retried."""
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404)

    async def body(client, sink):
        c = _BaseClient(client, sink)
        return await c._fetch("https://example.test/data", "probe")

    with _NoSleep():
        try:
            _run(_with_client(handler, body))
        except ApiError:
            assert calls["n"] == 1, f"404 retried {calls['n']} times; should be 1"
            return
    raise AssertionError("a 404 should raise ApiError")


def _overpass_query(request: httpx.Request) -> str:
    """The Overpass query out of a form-encoded POST body."""
    return parse_qs(request.content.decode()).get("data", [""])[0]


def _seamarks(nodes: int, ways: int) -> dict:
    return {
        "elements": [
            *({"type": "node", "id": i, "tags": {"seamark:type": "buoy_lateral"}}
              for i in range(nodes)),
            *({"type": "way", "id": i, "tags": {"seamark:type": "separation_lane"}}
              for i in range(ways)),
        ]
    }


def _run_overpass(handler, *, sample_cap: int = 800):
    region = REGIONS_BY_CODE["MTBY"]

    async def body(client, sink):
        return await OverpassClient(
            client, sink, max_attempts=2, budget_seconds=30
        ).fetch(region, sample_cap=sample_cap)

    return _run(_with_client(handler, body))


def test_overpass_asks_once_when_under_the_cap() -> None:
    """Overpass is the slowest feed, so round trips matter more than anything.
    ``out tags`` already returns one element per feature with its type, so the
    counts come from the response instead of a second identical-selector query."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert "out count" not in _overpass_query(request), (
            "a separate count query should not be issued below the cap"
        )
        return httpx.Response(200, json=_seamarks(50, 31))

    snapshot, sink = _run_overpass(handler)
    assert sink.total_requests == 1, f"expected 1 request, made {sink.total_requests}"
    assert (snapshot.node_count, snapshot.way_count) == (50, 31)
    assert snapshot.total_count == 81, "total must be derived from the elements"
    assert snapshot.sampled_elements == 81
    assert snapshot.traffic_features == 31, "separation lanes are traffic features"


def test_overpass_falls_back_to_count_query_when_truncated() -> None:
    """At the cap the elements are a sample and cannot supply the true total,
    so the count query is still needed — but only then."""
    def handler(request: httpx.Request) -> httpx.Response:
        if "out count" in _overpass_query(request):
            return httpx.Response(200, json={"elements": [
                {"type": "count",
                 "tags": {"nodes": "700", "ways": "108", "relations": "0",
                          "total": "808"}}
            ]})
        return httpx.Response(200, json=_seamarks(5, 5))

    snapshot, sink = _run_overpass(handler, sample_cap=10)
    assert sink.total_requests == 2, "a truncated sample needs the count query"
    assert snapshot.total_count == 808, "the authoritative total must win"
    assert snapshot.sampled_elements == 10, "composition stays the capped sample"


def test_overpass_degrades_to_counts_when_tags_fail() -> None:
    """Composition is lost but density must still be scoreable."""
    def handler(request: httpx.Request) -> httpx.Response:
        if "out count" in _overpass_query(request):
            return httpx.Response(200, json={"elements": [
                {"type": "count",
                 "tags": {"nodes": "120", "ways": "26", "relations": "0",
                          "total": "146"}}
            ]})
        return httpx.Response(504)

    with _NoSleep():
        snapshot, _ = _run_overpass(handler)
    assert snapshot.total_count == 146, "density needs a total even without tags"
    assert snapshot.sampled_elements == 0, "no composition was sampled"
    assert snapshot.type_breakdown == {}


def test_overpass_survives_total_failure() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(504)

    with _NoSleep():
        snapshot, _ = _run_overpass(handler)
    assert snapshot.total_count == 0 and snapshot.sampled_elements == 0


def test_rate_limit_moves_to_the_next_mirror_instead_of_retrying() -> None:
    """429 is a per-IP limit — it will not clear during a one-second backoff,
    so retrying the same host just burns the budget."""
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return httpx.Response(429)

    async def body(client, sink):
        return await _BaseClient(client, sink, max_attempts=3)._fetch(
            "https://a.test/x", "probe",
            mirrors=["https://b.test/x", "https://c.test/x"],
        )

    with _NoSleep():
        try:
            _run(_with_client(handler, body))
        except ApiError:
            pass
    counts = Counter(hosts)
    assert set(counts) == {"a.test", "b.test", "c.test"}, counts
    assert all(n == 1 for n in counts.values()), (
        f"429 was retried on the same host: {dict(counts)}"
    )


def test_wall_clock_budget_bounds_a_slow_failure() -> None:
    """An attempt cap bounds nothing when one attempt can take 20 s; the
    budget is what actually limits the worst case."""
    attempts = {"n": 0}

    async def slow_sleep(_delay):
        return None

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(504)

    async def body(client, sink):
        client_under_test = _BaseClient(
            client, sink, max_attempts=3, budget_seconds=0.0
        )
        return await client_under_test._fetch(
            "https://a.test/x", "probe", mirrors=["https://b.test/x"]
        )

    original = api_clients.asyncio.sleep
    api_clients.asyncio.sleep = slow_sleep
    try:
        _run(_with_client(handler, body))
    except ApiError as exc:
        assert "budget" in str(exc), f"should report the budget: {exc}"
        assert attempts["n"] == 0, (
            "an exhausted budget should stop before issuing a request"
        )
        return
    finally:
        api_clients.asyncio.sleep = original
    raise AssertionError("an exhausted budget should raise ApiError")


def test_latency_percentile_nearest_rank() -> None:
    sink = TelemetrySink()
    now = datetime.now(timezone.utc)
    for ms in (10.0, 20.0, 30.0, 40.0, 50.0):
        sink.record(
            TelemetryEvent(source="T", label="l", method="GET", url="u",
                           status=200, ok=True, latency_ms=ms, payload_bytes=1,
                           attempts=1, started_at=now)
        )
    assert sink.latency_percentile(50) == 30.0
    assert sink.latency_percentile(100) == 50.0
    assert sink.latency_percentile(1) == 10.0
    assert TelemetrySink().latency_percentile(95) == 0.0, "empty sink must be 0.0"


def test_sst_url_normalises_longitude_per_dataset() -> None:
    """ncdcOisst21Agg takes 0-360; the _LonPM180 variant takes -180-180."""
    client = ErddapClient.__new__(ErddapClient)  # no client needed for URL building
    start = end = date(2020, 6, 1)

    wrapped = client._sst_url("ncdcOisst21Agg", 36.75, -121.9, start, end)
    assert "(238.1000)" in wrapped, f"-121.9 should map to 238.1 in 0-360: {wrapped}"

    signed = client._sst_url("ncdcOisst21Agg_LonPM180", 36.75, -121.9, start, end)
    assert "(-121.9000)" in signed, f"_LonPM180 must keep the sign: {signed}"

    # Constraints follow a literal '&' and the bracket encoding must survive.
    assert "%5B" in wrapped and wrapped.count("?") == 1


def test_linear_trend_reports_slope_with_uncertainty() -> None:
    xs = [float(i) for i in range(10)]
    clean = [2.0 * x + 1.0 for x in xs]
    result = _linear_trend(xs, clean)
    assert result is not None
    slope, stderr, r2 = result
    assert abs(slope - 2.0) < 1e-9, f"slope {slope} should recover 2.0"
    assert stderr < 1e-9, "a perfect fit should have ~zero standard error"
    assert abs(r2 - 1.0) < 1e-9

    noisy = [1.0, 9.0, 2.0, 8.0, 3.0, 7.0, 4.0, 6.0, 5.0, 5.0]
    noisy_result = _linear_trend(xs, noisy)
    assert noisy_result is not None
    _, noisy_stderr, noisy_r2 = noisy_result
    assert noisy_stderr > 0.0, "a scattered fit must report real uncertainty"
    assert noisy_r2 < 0.5, f"scattered data should not report r2={noisy_r2}"

    assert _linear_trend([1.0], [1.0]) is None, "a single point has no trend"


def test_haversine_known_distance() -> None:
    # San Francisco to Los Angeles is ~559 km.
    km = _haversine_km(37.7749, -122.4194, 34.0522, -118.2437)
    assert 540.0 < km < 580.0, f"SF-LA computed as {km:.1f} km"
    assert _haversine_km(10.0, 20.0, 10.0, 20.0) == 0.0


ALL = [
    test_successful_fetch_records_one_event,
    test_retries_transient_status_then_succeeds,
    test_rotates_to_mirror_after_exhausting_first_host,
    test_exhausting_every_endpoint_raises_api_error,
    test_transport_error_is_retried_and_recorded,
    test_non_retryable_status_fails_immediately,
    test_overpass_asks_once_when_under_the_cap,
    test_overpass_falls_back_to_count_query_when_truncated,
    test_overpass_degrades_to_counts_when_tags_fail,
    test_overpass_survives_total_failure,
    test_rate_limit_moves_to_the_next_mirror_instead_of_retrying,
    test_wall_clock_budget_bounds_a_slow_failure,
    test_latency_percentile_nearest_rank,
    test_sst_url_normalises_longitude_per_dataset,
    test_linear_trend_reports_slope_with_uncertainty,
    test_haversine_known_distance,
]


if __name__ == "__main__":
    failures = 0
    for fn in ALL:
        try:
            fn()
            print(f"[ OK ] {fn.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"[FAIL] {fn.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"[ERR ] {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(ALL) - failures}/{len(ALL)} passed")
    sys.exit(1 if failures else 0)
