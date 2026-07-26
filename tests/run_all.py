"""Run the whole test suite without needing pytest.

    python tests/run_all.py            # everything (offline + live)
    python tests/run_all.py --offline  # skip network/AppTest suites

Offline suites are deterministic and fast. The live suite (`test_app.py`) hits
the real APIs and drives the Streamlit runtime, so it is slower and needs a
network connection.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

OFFLINE = [
    "test_ml_analysis", "test_regions", "test_geocoding", "test_data_access",
    "test_analyzer", "test_transport", "test_render", "test_layout",
    "test_history_db",
]
LIVE = ["test_app"]


def _run_module(name: str) -> int:
    print(f"\n{'=' * 70}\n{name}\n{'=' * 70}")
    module = importlib.import_module(name)
    if hasattr(module, "run"):
        return int(module.run())
    failures = 0
    for fn in getattr(module, "ALL", []):
        try:
            fn()
            print(f"[ OK ] {fn.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"[FAIL] {fn.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"[ERR ] {fn.__name__}: {type(exc).__name__}: {exc}")
    return 1 if failures else 0


def main() -> int:
    offline_only = "--offline" in sys.argv
    modules = OFFLINE + ([] if offline_only else LIVE)

    # Ensure fixtures exist (deterministic; committed, but regenerate if missing).
    if not (Path(__file__).resolve().parent / "fixtures" / "fixture_known.csv").exists():
        import make_fixtures
        make_fixtures.build()

    results: dict[str, int] = {}
    for name in modules:
        results[name] = _run_module(name)

    print(f"\n{'=' * 70}\nSUMMARY\n{'=' * 70}")
    for name, code in results.items():
        print(f"  {'PASS' if code == 0 else 'FAIL'}  {name}")
    total = sum(results.values())
    print(f"\n{'ALL PASS' if total == 0 else 'FAILURES PRESENT'}")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
