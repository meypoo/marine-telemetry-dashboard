"""Marine Ecosystem Health Dashboard — entry point.

Sets up the terminal shell and dispatches to the two pages:

* **LIVE INDEX** (``page_live.py``) — the Ecological Stress Score for a selected
  region, computed from live OBIS / Open-Meteo / NOAA ERDDAP / Overpass
  responses, rendered as a fixed-width terminal artifact.
* **DATA LAB** (``page_lab.py``) — upload your own observations and have their
  trends, cycles, regime shifts and anomalies analysed.

Navigation is hidden rather than rendered into Streamlit's sidebar: the Live
Index is a fixed 1920px design with its own 360px sidebar, and a second
framework sidebar beside it would break the layout. Page links live in the top
bar instead.

Run with::

    streamlit run app.py

Query parameters (per the design handoff's dev flags):
    ?density=compact   tighten section padding (22/24px -> 16/16px)
    ?console=0         hide the API transport log
    ?width=fluid       release the fixed 1920px frame
"""

from __future__ import annotations

import streamlit as st

st.set_page_config(
    page_title="Marine Ecosystem Health Index",
    page_icon="~",
    layout="wide",
    initial_sidebar_state="collapsed",
)

from ui import TerminalConfig, inject_base_css  # noqa: E402  (must follow set_page_config)

inject_base_css(TerminalConfig.from_query_params())

# The default page is served at "/" (Streamlit does not also serve it at a
# custom url_path — navigating to "/live" would raise a "page not found"
# dialog), so the Live Index deliberately has no url_path: it lives at "/".
# The Data Lab is at "/lab".
navigation = st.navigation(
    [
        st.Page("page_live.py", title="LIVE INDEX", default=True),
        st.Page("page_lab.py", title="DATA LAB", url_path="lab"),
    ],
    position="hidden",
)
navigation.run()
