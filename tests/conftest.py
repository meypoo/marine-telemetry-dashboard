"""Shared test setup: put the project root on sys.path.

Works for both pytest (which imports this automatically) and the standalone
runners (which import it explicitly). `streamlit run` puts the app directory on
the path; nothing else does, so tests must.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
