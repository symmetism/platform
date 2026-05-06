"""SVG badge generation.

Two badges, both ~150-180 × 20 px, two-compartment, hand-rolled SVG
(no shields.io dep — we control look entirely; offline-capable).

Color mirrors Design SoT D2:
  Stable   #7eb6d9  fingerprint badge background
  Drift    #e0a458  timestamp badge when last-verified > 24h ago
  Alarm    #cc4444  reserved for canonical lockdown — not used here
  Hairline #1a1f28  left compartment background
  Bone     #e8e6df  text on dark
"""

from __future__ import annotations

from datetime import datetime, timezone

LEFT_BG = "#1a1f28"
STABLE = "#7eb6d9"
DRIFT = "#e0a458"
TEXT = "#e8e6df"

FONT = (
    "'JetBrainsMono Nerd Font','JetBrains Mono','SFMono-Regular',"
    "Menlo,Monaco,Consolas,monospace"
)


def _escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _badge_svg(
    label: str, value: str, *, value_bg: str, label_w: int, value_w: int
) -> str:
    total = label_w + value_w
    safe_label = _escape(label)
    safe_value = _escape(value)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{total}" height="20" '
        f'viewBox="0 0 {total} 20" role="img" aria-label="{safe_label}: {safe_value}">'
        f'<rect width="{label_w}" height="20" fill="{LEFT_BG}"/>'
        f'<rect x="{label_w}" width="{value_w}" height="20" fill="{value_bg}"/>'
        f'<g fill="{TEXT}" font-family="{FONT}" font-size="11" '
        f'text-anchor="middle">'
        f'<text x="{label_w / 2:.0f}" y="14">{safe_label}</text>'
        f'<text x="{label_w + value_w / 2:.0f}" y="14">{safe_value}</text>'
        f"</g></svg>"
    )


def fingerprint_badge(fingerprint: str) -> str:
    """`symmetism | SYM-XXXX-XXXX-XXXX-XXXX`."""
    return _badge_svg(
        "symmetism",
        fingerprint,
        value_bg=STABLE,
        label_w=78,
        value_w=170,
    )


def timestamp_badge(last_verified_utc: str) -> str:
    """`verified | YYYY-MM-DD HH:MM UTC`. Amber if > 24h ago."""
    try:
        ts = datetime.strptime(last_verified_utc, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
        age_hr = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
        bg = DRIFT if age_hr > 24 else STABLE
        pretty = ts.strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, TypeError):
        bg = DRIFT
        pretty = last_verified_utc[:16] if last_verified_utc else "unknown"
    return _badge_svg(
        "verified",
        pretty,
        value_bg=bg,
        label_w=64,
        value_w=148,
    )
