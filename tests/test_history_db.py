"""The durable-history Postgres backend.

Two layers, mirroring `test_geocoding` (offline always; live gated by env):

* Offline: a fake psycopg connection backs `_pg_append_history` / `_pg_load_history`
  so the dispatch, rate-limit, prune and reconnect-on-drop logic run
  deterministically without a database.
* Live: set `MEHI_HISTORY_DB` to a real (throwaway) Postgres URL to exercise the
  actual SQL round-trip. Skipped otherwise.
"""

from __future__ import annotations

import os
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import data_access  # noqa: E402
from analyzer import StressAssessment  # noqa: E402


# --------------------------------------------------------------------------- #
# Fake psycopg — just enough to interpret the four statements we issue.
# --------------------------------------------------------------------------- #
class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else (None,)


class _FakeConn:
    def __init__(self, store, fail_first=False):
        self.store = store
        self.closed = False
        self._fail_first = fail_first

    def execute(self, sql, params=None):
        if self._fail_first:
            self._fail_first = False
            raise RuntimeError("simulated dropped connection")
        s = " ".join(sql.split())
        if s.startswith("CREATE"):
            return _FakeCursor([])
        if s.startswith("SELECT max(at)"):
            ats = [r["at"] for r in self.store if r["code"] == params[0]]
            return _FakeCursor([(max(ats) if ats else None,)])
        if s.startswith("INSERT"):
            code, at, score, band, conf = params
            self.store.append(
                dict(code=code, at=at, score=score, band=band, confidence=conf)
            )
            return _FakeCursor([])
        if s.startswith("DELETE"):
            code, cutoff = params
            self.store[:] = [
                r for r in self.store
                if not (r["code"] == code and r["at"] < cutoff)
            ]
            return _FakeCursor([])
        if s.startswith("SELECT at, score"):
            rows = sorted(
                (r for r in self.store
                 if r["code"] == params[0] and r["score"] is not None),
                key=lambda r: r["at"],
            )
            return _FakeCursor(
                [(r["at"], r["score"], r["band"], r["confidence"]) for r in rows]
            )
        return _FakeCursor([])

    def close(self):
        self.closed = True


def _install_fake_db(store, *, fail_first_connect=False):
    """Point data_access at a fake psycopg; return a restore() callable."""
    state = {"first": fail_first_connect}

    def connect(url, **kw):
        conn = _FakeConn(store, fail_first=state["first"])
        state["first"] = False
        return conn

    fake = types.ModuleType("psycopg")
    fake.connect = connect
    saved_mod = sys.modules.get("psycopg")
    saved_url = data_access.DATABASE_URL
    saved_disabled = data_access._pg_disabled
    sys.modules["psycopg"] = fake
    data_access.DATABASE_URL = "postgresql://fake/db"
    data_access._pg_conn = None
    data_access._pg_disabled = False

    def restore():
        if saved_mod is not None:
            sys.modules["psycopg"] = saved_mod
        else:
            sys.modules.pop("psycopg", None)
        data_access.DATABASE_URL = saved_url
        data_access._pg_conn = None
        data_access._pg_disabled = saved_disabled

    return restore


def _assess(score: float) -> StressAssessment:
    return StressAssessment(region_code="X", region_name="X", score=score,
                            band="MODERATE", confidence=0.8)


# --------------------------------------------------------------------------- #
# Offline tests (fake DB)
# --------------------------------------------------------------------------- #
def test_db_dispatch_when_url_set() -> None:
    store: list = []
    restore = _install_fake_db(store)
    try:
        assert data_access._history_uses_db() is True
        data_access._append_history("MTBY", _assess(50.0))
        assert len(store) == 1, "append should have inserted one row"
        out = data_access.load_history("MTBY")
        assert [e["score"] for e in out] == [50.0]
        assert out[0]["band"] == "MODERATE"
        # "at" must be an ISO string, as the renderer parses it.
        datetime.fromisoformat(out[0]["at"])
    finally:
        restore()


def test_db_rate_limits_like_local() -> None:
    store: list = []
    restore = _install_fake_db(store)
    try:
        data_access._append_history("R", _assess(40.0))
        data_access._append_history("R", _assess(41.0))  # immediate → skipped
        assert len(store) == 1, "a second point within 30 min must be skipped"
        # Backdate the stored point so the next append is allowed.
        store[0]["at"] = datetime.now(timezone.utc) - timedelta(hours=1)
        data_access._append_history("R", _assess(42.0))
        assert [r["score"] for r in store] == [40.0, 42.0]
    finally:
        restore()


def test_db_prunes_past_retention() -> None:
    store: list = []
    restore = _install_fake_db(store)
    try:
        old = datetime.now(timezone.utc) - timedelta(
            days=data_access.HISTORY_RETENTION_DAYS + 5
        )
        store.append(dict(code="R", at=old, score=10.0, band="LOW", confidence=0.5))
        data_access._append_history("R", _assess(55.0))
        scores = [r["score"] for r in store]
        assert 10.0 not in scores, "a row past retention should be pruned on write"
        assert 55.0 in scores
    finally:
        restore()


def test_db_reconnects_on_dropped_connection() -> None:
    store: list = []
    restore = _install_fake_db(store, fail_first_connect=True)
    try:
        # First execute raises (simulated suspend); _pg_run must reconnect + retry.
        data_access._append_history("R", _assess(60.0))
        assert [r["score"] for r in store] == [60.0]
    finally:
        restore()


def test_missing_driver_disables_db_and_falls_back_to_local() -> None:
    """DATABASE_URL set but psycopg absent is a permanent config error: it must
    disable the DB path once (no per-render tracebacks) and fall back to local
    history, not drop the panel."""
    import tempfile
    from pathlib import Path

    saved_mod = sys.modules.get("psycopg")
    saved_url = data_access.DATABASE_URL
    saved_disabled = data_access._pg_disabled
    saved_cache = data_access.CACHE_DIR
    # Force `import psycopg` to fail.
    sys.modules["psycopg"] = None  # type: ignore[assignment]
    data_access.DATABASE_URL = "postgresql://fake/db"
    data_access._pg_disabled = False
    data_access.CACHE_DIR = Path(tempfile.mkdtemp())
    try:
        assert data_access._history_uses_db() is True
        data_access._append_history("MTBY", _assess(50.0))
        assert data_access._pg_disabled is True, "missing driver should disable DB"
        assert data_access._history_uses_db() is False
        # The write should have landed locally as a fallback.
        out = data_access.load_history("MTBY")
        assert [e["score"] for e in out] == [50.0], "did not fall back to local"
    finally:
        if saved_mod is not None:
            sys.modules["psycopg"] = saved_mod
        else:
            sys.modules.pop("psycopg", None)
        data_access.DATABASE_URL = saved_url
        data_access._pg_disabled = saved_disabled
        data_access.CACHE_DIR = saved_cache


def test_db_read_failure_degrades_to_empty() -> None:
    """History I/O is best-effort: a hard DB failure returns [] rather than
    raising into the page."""
    saved_mod = sys.modules.get("psycopg")
    saved_url = data_access.DATABASE_URL
    fake = types.ModuleType("psycopg")

    def boom(url, **kw):
        raise RuntimeError("db unreachable")

    fake.connect = boom
    sys.modules["psycopg"] = fake
    data_access.DATABASE_URL = "postgresql://fake/db"
    data_access._pg_conn = None
    try:
        assert data_access.load_history("R") == []
    finally:
        if saved_mod is not None:
            sys.modules["psycopg"] = saved_mod
        else:
            sys.modules.pop("psycopg", None)
        data_access.DATABASE_URL = saved_url
        data_access._pg_conn = None


# --------------------------------------------------------------------------- #
# Live test (real Postgres) — gated
# --------------------------------------------------------------------------- #
def test_live_postgres_roundtrip() -> None:
    url = os.getenv("MEHI_HISTORY_DB")
    if not url:
        print("      (skipped — set MEHI_HISTORY_DB to a throwaway Postgres URL)")
        return
    saved_url = data_access.DATABASE_URL
    data_access.DATABASE_URL = url
    data_access._pg_conn = None
    code = f"TEST_{int(datetime.now().timestamp())}"
    try:
        assert data_access.load_history(code) == []
        data_access._append_history(code, _assess(48.0))
        out = data_access.load_history(code)
        assert [e["score"] for e in out] == [48.0]
        datetime.fromisoformat(out[0]["at"])
        # Clean up the test rows.
        data_access._pg_run(
            lambda c: c.execute("DELETE FROM score_history WHERE code = %s", (code,))
        )
    finally:
        data_access.DATABASE_URL = saved_url
        data_access._pg_conn = None


ALL = [
    test_db_dispatch_when_url_set,
    test_db_rate_limits_like_local,
    test_db_prunes_past_retention,
    test_db_reconnects_on_dropped_connection,
    test_missing_driver_disables_db_and_falls_back_to_local,
    test_db_read_failure_degrades_to_empty,
    test_live_postgres_roundtrip,
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
