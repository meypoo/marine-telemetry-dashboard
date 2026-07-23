"""Design tokens, stylesheet and rendering primitives.

Implements the Marine Ecosystem Health Dashboard handoff: a fixed 1920px
terminal artifact on the portfolio palette, Space Mono throughout with Space
Grotesk reserved for the single hero number, and sharp corners everywhere.

Palette notes from the handoff, worth preserving:

* There is **no alert-red hue**. Every warning, anomaly and error uses the one
  amber accent (``SIGNAL``), keeping the palette to two accents total.
* No glow, pulse or transition effects. An earlier draft had a pulsing live
  dot; it was removed deliberately and must not come back.
* Nothing is rounded — every border, chip, button and bar is 0 radius, even
  where a framework default would round it.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Any, Literal

import altair as alt
import streamlit as st

__all__ = [
    "INK", "PAPER", "PAPER_DIM", "MIST", "LINE", "BORDER_STRONG", "SIGNAL", "CANOPY",
    "SOURCE_COLOURS", "TerminalConfig",
    "inject_base_css", "inject_terminal_css",
    "metric_box", "panel_title", "kv_rows", "fmt", "style_chart", "command_bar",
    "safe_page_link", "stress_accent", "esc",
]

# --------------------------------------------------------------------------- #
# Tokens
# --------------------------------------------------------------------------- #
INK = "#0C1512"            # page background
PAPER = "#EDF2EC"          # primary text and numbers
PAPER_DIM = "#C6D3CA"      # secondary values, tick labels
MIST = "#82A896"           # dimmest labels, live indicator, secondary chart line
LINE = "#22362B"           # hairline dividers
BORDER_STRONG = "#2f4a3a"  # pills, dashed reference line
SIGNAL = "#E4B34A"         # amber: label chips, alerts, warnings
CANOPY = "#3E8F62"         # primary accent: chart line, bars, in-situ legend

#: Per-API colouring for the transport log. NOAA ERDDAP rows are amber per the
#: handoff; the rest fall back across the remaining palette.
SOURCE_COLOURS: dict[str, str] = {
    "NOAA-ERDDAP": SIGNAL,
    "OBIS": CANOPY,
    "OPEN-METEO": MIST,
    "OVERPASS": PAPER_DIM,
}

FONT_IMPORT = (
    "@import url('https://fonts.googleapis.com/css2?"
    "family=Space+Mono:wght@400;700&family=Space+Grotesk:wght@500;700&display=swap');"
)
MONO = "'Space Mono', 'Consolas', 'DejaVu Sans Mono', monospace"
GROTESK = "'Space Grotesk', 'Space Mono', monospace"


@dataclass(frozen=True)
class TerminalConfig:
    """Dev/config flags from the handoff, driven by query parameters.

    ``?density=compact`` tightens the two padding values; ``?console=0`` hides
    the transport log; ``?width=fluid`` releases the fixed 1920px frame for
    screens narrower than the design width.
    """

    density: Literal["comfortable", "compact"] = "comfortable"
    show_console: bool = True
    fluid_width: bool = False
    #: Seconds between automatic refreshes. Timed reruns only fire while a
    #: browser session is connected, which is the intended overnight setup.
    refresh_seconds: int = 720

    @property
    def pad_outer(self) -> int:
        return 16 if self.density == "compact" else 22

    @property
    def pad_section(self) -> int:
        return 16 if self.density == "compact" else 24

    @classmethod
    def from_query_params(cls) -> "TerminalConfig":
        try:
            params = st.query_params
        except Exception:  # outside a script run context
            return cls()

        def flag(name: str, default: bool) -> bool:
            raw = params.get(name)
            if raw is None:
                return default
            return str(raw).strip().lower() not in {"0", "false", "no", "off"}

        density = str(params.get("density", "comfortable")).strip().lower()

        refresh = 720
        try:
            raw = params.get("refresh")
            if raw is not None:
                # Floor at 60s: anything faster hammers the upstream APIs for
                # data that only changes on a ten-minute cache cycle anyway.
                refresh = max(60, int(float(str(raw))))
        except (TypeError, ValueError):
            pass

        return cls(
            density="compact" if density == "compact" else "comfortable",
            show_console=flag("console", True),
            fluid_width=str(params.get("width", "")).strip().lower() == "fluid",
            refresh_seconds=refresh,
        )


def esc(value: Any) -> str:
    return html.escape(str(value))


# --------------------------------------------------------------------------- #
# Stylesheets
# --------------------------------------------------------------------------- #
def _base_css(config: TerminalConfig) -> str:
    return f"""
<style>
  {FONT_IMPORT}

  html, body, [class*="css"], .stMarkdown, .stSelectbox, .stButton, .stFileUploader,
  .stMultiSelect, .stSlider, .stDataFrame, input, select, textarea, button {{
      font-family: {MONO} !important;
  }}
  *, *::before, *::after {{ border-radius: 0 !important; }}

  .stApp, body {{ background: {INK}; color: {PAPER}; }}
  #MainMenu, footer, [data-testid="stStatusWidget"] {{ visibility: hidden; height: 0; }}
  header[data-testid="stHeader"] {{ background: transparent; height: 0; }}

  /* Consistent, tight page frame across pages. Streamlit's default block
     container adds a large top gap (to clear the now-hidden header) and wide
     side padding; the Data Lab uses this base sheet, so normalise it here. The
     Live terminal overrides these to zero in its own sheet. */
  .block-container {{ max-width: 1600px !important; margin: 0 auto;
      padding: 16px 28px 48px 28px !important; }}

  div[data-testid="stVerticalBlock"] {{ gap: 0.35rem !important; }}
  div[data-testid="stHorizontalBlock"] {{ gap: 0.35rem !important; }}
  hr {{ border-color: {LINE}; margin: 0.35rem 0; }}

  /* --- widgets restyled as terminal controls (sharp, bordered) --- */
  .stButton > button {{
      border: 1px solid {SIGNAL}; background: transparent; color: {SIGNAL};
      font-size: 12px; font-weight: 700; letter-spacing: 0.05em;
      padding: 9px 12px; width: 100%; white-space: nowrap;
  }}
  /* Streamlit gives the button label an aggressive word-break; stop it breaking
     "REFRESH" into vertical fragments in a narrow column. */
  .stButton > button * {{ white-space: nowrap !important; word-break: keep-all !important;
      overflow-wrap: normal !important; }}
  .stButton > button:hover {{ background: rgba(228,179,74,0.12); color: {SIGNAL}; }}
  .stButton > button:focus {{ box-shadow: none; color: {SIGNAL}; }}

  /* Inputs fill their column. */
  [data-testid="stTextInput"] input {{ width: 100%; }}

  div[data-baseweb="select"] > div {{
      background: transparent !important; border: 1px solid {BORDER_STRONG} !important;
      color: {PAPER} !important; font-size: 12.5px !important; min-height: 0 !important;
  }}
  div[data-baseweb="select"] div {{ color: {PAPER} !important; }}
  div[data-baseweb="popover"] li {{
      background: {INK} !important; color: {PAPER} !important; font-size: 12.5px !important;
  }}
  div[data-baseweb="popover"] li:hover {{ background: {LINE} !important; }}

  a[data-testid="stPageLink-NavLink"] {{
      color: {MIST} !important; font-size: 11.5px; letter-spacing: 0.05em;
      padding: 4px 0 !important;
  }}
  a[data-testid="stPageLink-NavLink"]:hover {{ color: {SIGNAL} !important; }}

  section[data-testid="stFileUploaderDropzone"] {{
      border: 1px dashed {CANOPY}; background: #16231b; padding: 8px;
  }}
  section[data-testid="stFileUploaderDropzone"] button,
  section[data-testid="stFileUploaderDropzone"] button * {{
      white-space: nowrap !important; word-break: keep-all !important;
  }}
  .stDataFrame {{ border: 1px solid {LINE}; }}

  /* --- shared primitives --- */
  .mx-bar {{
      border: 1px solid {BORDER_STRONG}; background: #16231b;
      padding: 6px 10px; margin-bottom: 6px;
      display: flex; justify-content: space-between; align-items: center;
      letter-spacing: 0.10em;
  }}
  .mx-bar .t {{ color: {SIGNAL}; font-weight: 700; font-size: 13px; }}
  .mx-bar .r {{ color: {MIST}; font-size: 10.5px; }}

  .mx-box {{
      border: 1px solid {LINE}; background: #16231b; padding: 8px 10px; height: 100%;
      display: flex; flex-direction: column; justify-content: space-between;
  }}
  .mx-label {{
      color: {MIST}; font-size: 10px; letter-spacing: 0.12em; text-transform: uppercase;
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }}
  .mx-value {{ font-size: 19px; font-weight: 700; line-height: 1.2; margin: 2px 0; }}
  .mx-sub {{ color: {MIST}; font-size: 10.5px; }}

  .mx-panel-title {{
      color: {SIGNAL}; font-size: 10px; letter-spacing: 0.14em; text-transform: uppercase;
      border-bottom: 1px solid {LINE}; padding: 4px 0 3px 0; margin: 6px 0 4px 0;
  }}
  .mx-kv {{ font-size: 11.5px; line-height: 1.55; }}
  .mx-kv .k {{ color: {MIST}; display: inline-block; min-width: 190px; }}
  .mx-kv .v {{ color: {PAPER}; }}
  .mx-kv .w, .mx-kv .e {{ color: {SIGNAL}; }}
  .mx-kv .c {{ color: {CANOPY}; }}
</style>
"""


def _terminal_css(config: TerminalConfig) -> str:
    """Live Index frame. Injected after the base sheet so it wins."""
    # The design width is 1920px, applied as a *max-width* (not a fixed/min
    # width): at >=1920 it renders at full design size, and on a smaller screen
    # it shrinks to fit instead of forcing the right-hand controls off-screen.
    # Fluid mode removes the cap entirely.
    width_rule = (
        "max-width: 100% !important;"
        if config.fluid_width
        else "max-width: 1920px !important;"
    )
    return f"""
<style>
  .block-container {{ {width_rule} width: 100% !important; margin: 0 auto;
      padding: 0 !important; }}
  div[data-testid="stVerticalBlock"] {{ gap: 0 !important; }}
  div[data-testid="stHorizontalBlock"] {{ gap: 14px !important; align-items: center;
      flex-wrap: nowrap; }}
  /* Keep the terminal usable down to a sensible desktop min before scrolling. */
  section.main > div.block-container {{ min-width: 0; }}

  .tm-root {{ width: 100%; background: {INK}; color: {PAPER};
      font-family: {MONO}; font-size: 12px; }}

  /* --- top bar --- */
  .tm-topbar {{ padding: {config.pad_outer}px 32px; border-bottom: 1px solid {LINE}; }}
  .tm-live {{ width: 6px; height: 6px; background: {MIST}; display: inline-block;
      margin-right: 9px; vertical-align: middle; }}
  .tm-title {{ font-size: 20px; font-weight: 700; letter-spacing: 0.03em; color: {PAPER}; }}
  .tm-subtitle {{ font-size: 12px; color: {MIST}; margin-top: 4px; }}
  .tm-stamp {{ font-size: 12.5px; color: {PAPER_DIM}; }}
  .tm-pill {{ border: 1px solid {BORDER_STRONG}; padding: 8px 16px; font-size: 12.5px;
      color: {PAPER}; display: inline-block; }}

  /* --- body --- */
  .tm-body {{ display: flex; align-items: stretch; }}
  .tm-side {{ width: 360px; min-width: 360px; border-right: 1px solid {LINE};
      padding: {config.pad_section}px 24px; display: flex; flex-direction: column; gap: 20px; }}
  .tm-main {{ flex: 1; padding: {config.pad_section}px 28px; display: flex;
      flex-direction: column; gap: 22px; min-width: 0; }}

  /* --- chips and section headers --- */
  .tm-chip {{ background: {SIGNAL}; color: {INK}; font-size: 10px; font-weight: 700;
      padding: 3px 8px; display: inline-block; letter-spacing: 0.04em; }}
  .tm-chip.lg {{ font-size: 11.5px; }}
  .tm-chip.bar {{ display: block; }}
  .tm-seclabel {{ color: {SIGNAL}; font-size: 10px; font-weight: 700; letter-spacing: 0.12em;
      text-transform: uppercase; margin-bottom: 6px; }}
  .tm-divider {{ border-top: 1px solid {LINE}; padding-top: 16px; }}

  /* --- hero --- */
  .tm-hero-num {{ font-family: {GROTESK}; font-size: 60px; font-weight: 700;
      letter-spacing: -0.02em; color: {PAPER}; line-height: 1.05; margin: 10px 0 8px 0; }}

  /* --- key/value rows --- */
  .tm-row {{ display: flex; justify-content: space-between; align-items: baseline;
      padding: 6px 0; border-bottom: 1px solid {LINE}; gap: 12px; }}
  .tm-row:last-child {{ border-bottom: none; }}
  .tm-row .k {{ color: {MIST}; font-size: 12px; }}
  .tm-row .k.sm {{ font-size: 11.5px; }}
  .tm-row .v {{ font-size: 12px; font-weight: 600; text-align: right; }}
  .tm-row .v.md {{ font-size: 12.5px; }}

  /* --- panels --- */
  .tm-legend {{ display: flex; gap: 18px; align-items: center; font-size: 11.5px; }}
  .tm-panelhead {{ display: flex; justify-content: space-between; align-items: center;
      gap: 16px; margin-bottom: 10px; }}
  .tm-yaxis {{ display: flex; flex-direction: column; justify-content: space-between;
      font-size: 10.5px; color: {MIST}; text-align: right; width: 30px;
      padding-right: 4px; }}
  .tm-xaxis {{ display: flex; justify-content: space-between; font-size: 10px;
      color: {MIST}; padding-left: 34px; margin-top: 6px; }}
  .tm-chartrow {{ display: flex; align-items: stretch; }}

  /* --- bar chart --- */
  .tm-bars {{ position: relative; display: flex; align-items: flex-end; gap: 22px;
      height: 130px; flex: 1; }}
  .tm-bar {{ flex: 1; background: {CANOPY}; }}
  .tm-threshold {{ position: absolute; left: 0; right: 0; height: 2px;
      background: {SIGNAL}; }}

  /* --- horizontal bar rows --- */
  .tm-hrow {{ display: flex; align-items: center; gap: 10px; padding: 3.5px 0; }}
  .tm-hlabel {{ font-size: 11.5px; color: {MIST}; text-align: right; }}
  .tm-htrack {{ flex: 1; background: #16231b; height: 15px; }}
  .tm-hfill {{ background: {CANOPY}; height: 15px; }}
  .tm-hvalue {{ width: 40px; font-size: 11px; color: {PAPER_DIM}; text-align: right; }}

  /* --- stats strip --- */
  .tm-stats {{ display: flex; gap: 24px; border-top: 1px solid {LINE}; padding-top: 16px; }}
  .tm-statcol {{ flex: 1; }}
  .tm-statcol + .tm-statcol {{ border-left: 1px solid {LINE}; padding-left: 24px; }}
  .tm-statrow {{ display: flex; justify-content: space-between; padding: 6px 0;
      font-size: 12px; gap: 12px; }}
  .tm-statrow .k {{ color: {MIST}; }}
  .tm-statrow .v {{ color: {PAPER}; font-weight: 500; text-align: right; }}

  /* --- console --- */
  .tm-console {{ border-top: 1px solid {LINE}; padding-top: 16px; overflow-x: auto; }}
  .tm-tiles {{ display: grid; grid-template-columns: repeat(6, 1fr); gap: 14px;
      border-bottom: 1px solid {LINE}; padding-bottom: 14px; margin-bottom: 14px; }}
  .tm-tile .l {{ color: {SIGNAL}; font-size: 10px; letter-spacing: 0.10em; }}
  .tm-tile .v {{ font-size: 19px; font-weight: 700; color: {PAPER}; margin: 3px 0; }}
  .tm-tile .c {{ color: {MIST}; font-size: 10.5px; }}
  .tm-log {{ display: grid; grid-template-columns: 140px 140px 160px 70px 80px 80px 30px;
      gap: 14px; font-size: 11.5px; padding: 3px 0; }}
  .tm-log.head {{ font-size: 10px; color: {MIST}; letter-spacing: 0.08em;
      border-bottom: 1px solid {LINE}; padding-bottom: 6px; margin-bottom: 6px; }}

  /* --- footer --- */
  .tm-footer {{ border-top: 1px solid {LINE}; padding: 16px 32px; font-size: 10.5px;
      color: {MIST}; }}

  .tm-amber {{ color: {SIGNAL}; }}
  .tm-canopy {{ color: {CANOPY}; }}
  .tm-mist {{ color: {MIST}; }}
  .tm-paper {{ color: {PAPER}; }}
  .tm-dim {{ color: {PAPER_DIM}; }}
</style>
"""


def inject_base_css(config: TerminalConfig | None = None) -> None:
    """Apply fonts, palette and widget styling. Call once after set_page_config."""
    st.markdown(_base_css(config or TerminalConfig()), unsafe_allow_html=True)


def inject_terminal_css(config: TerminalConfig) -> None:
    """Apply the Live Index 1920px frame. Call from the live page only."""
    st.markdown(_terminal_css(config), unsafe_allow_html=True)


# --------------------------------------------------------------------------- #
# Data Lab primitives
# --------------------------------------------------------------------------- #
def metric_box(
    label: str,
    value: str,
    sub: str = "",
    *,
    accent: str = PAPER,
    border: str = LINE,
    hero: bool = False,
) -> str:
    """One sharp-cornered metric tile with a thin border."""
    return (
        f'<div class="mx-box" style="border-color:{border}">'
        f'<div class="mx-label">{esc(label)}</div>'
        f'<div class="mx-value" style="color:{accent}">{esc(value)}</div>'
        f'<div class="mx-sub">{esc(sub)}</div>'
        "</div>"
    )


def panel_title(text: str) -> None:
    st.markdown(f'<div class="mx-panel-title">{esc(text)}</div>', unsafe_allow_html=True)


def kv_rows(rows: list[tuple[str, str, str]]) -> str:
    """Key/value block. Each row is (key, value, css_class)."""
    out = ['<div class="mx-kv">']
    for key, value, cls in rows:
        out.append(
            f'<div><span class="k">{esc(key)}</span>'
            f'<span class="{cls}">{esc(value)}</span></div>'
        )
    out.append("</div>")
    return "".join(out)


def fmt(value: Any, spec: str = ".2f", suffix: str = "", dash: str = "--") -> str:
    if value is None:
        return dash
    if isinstance(value, float) and value != value:  # NaN
        return dash
    if isinstance(value, (int, float)):
        return f"{value:{spec}}{suffix}"
    return f"{value}{suffix}"


def stress_accent(score: float | None) -> str:
    """Accent colour for a stress score, on the portfolio palette.

    This is a display decision and lives in the UI layer, not the analyzer:
    high stress reads amber (the sole alert accent), low stress reads canopy
    green, mid-range stays neutral paper. The 70/30 thresholds match the
    handoff's ELEVATED / GUARDED bands.
    """
    if score is None:
        return MIST
    if score >= 70:
        return SIGNAL
    if score < 30:
        return CANOPY
    return PAPER


def safe_page_link(target: str, label: str) -> None:
    """Render a page link, tolerating the absence of a navigation context.

    Pages are also run directly (by the test harness, and by
    ``streamlit run page_live.py``), where no multi-page context exists and
    ``st.page_link`` raises. The link is decoration, so its absence must not
    take the page down.
    """
    try:
        st.page_link(target, label=label)
    except Exception:
        st.markdown(
            f'<div style="color:{MIST};font-size:11.5px">{esc(label)}</div>',
            unsafe_allow_html=True,
        )


def command_bar(title: str, right: str) -> None:
    st.markdown(
        f'<div class="mx-bar"><span class="t">{esc(title)}</span>'
        f'<span class="r">{esc(right)}</span></div>',
        unsafe_allow_html=True,
    )


def style_chart(chart: alt.Chart) -> alt.Chart:
    """Apply the palette to an Altair chart (Data Lab only)."""
    return (
        chart.configure(background=INK)
        .configure_view(strokeWidth=0)
        .configure_axis(
            labelColor=PAPER_DIM, titleColor=MIST, gridColor="#16231b",
            domainColor=LINE, tickColor=LINE, labelFontSize=9, titleFontSize=9,
            labelFont="Space Mono, monospace", titleFont="Space Mono, monospace",
        )
        .configure_legend(
            labelColor=PAPER_DIM, titleColor=MIST, labelFontSize=9, titleFontSize=9,
            labelFont="Space Mono, monospace", titleFont="Space Mono, monospace",
            orient="top", offset=2, padding=0,
        )
    )
