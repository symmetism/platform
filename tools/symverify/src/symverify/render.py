"""Rich rendering of trinity rings, status table, and stabilizer audit.

Color discipline (Design SoT D2):
  Stable   #7eb6d9 — default; rings when aligned; '✓' marks
  Verified #5fcc7d — brief flash on success (we don't animate; static '✓')
  Drift    #e0a458 — expected non-zero brackets, missing-server-yet, etc.
  Alarm    #cc4444 — canonical anchor failure ONLY
  Muted    #5a6470 — hashes, timestamps
  Hairline #1a1f28 — borders (we don't render borders explicitly here)
"""

from __future__ import annotations

from typing import Iterable

from rich.console import Group
from rich.padding import Padding
from rich.table import Table
from rich.text import Text

from symverify.stabilizer import (
    AuditReport,
    Bracket,
    STATUS_CONSERVED,
    STATUS_DRIFT_ALARM,
    STATUS_DRIFT_EXPECTED,
    STATUS_PENDING,
)

STABLE = "#7eb6d9"
VERIFIED = "#5fcc7d"
DRIFT = "#e0a458"
ALARM = "#cc4444"
MUTED = "#5a6470"


# ---------------------------------------------------------------------------
# Trinity rings (Design SoT D5)
# ---------------------------------------------------------------------------


def _state_pattern(local: str | None, git: str | None, server: str | None) -> str:
    """Return one of: 'aligned', 'one_axis', 'three_axis'.

    Two-axis drift isn't a logical possibility — if two pairs match
    transitively the third must too, and we're back to aligned. So the
    only states are: all three equal (3 matches), exactly two equal
    (1 match), or all three different (0 matches). None is treated as
    a unique sentinel that matches no other value.
    """
    pair_matches = sum(
        1
        for a, b in ((local, git), (local, server), (git, server))
        if a is not None and a == b
    )
    if pair_matches == 3:
        return "aligned"
    if pair_matches == 1:
        return "one_axis"
    return "three_axis"


def render_rings(
    local: str | None,
    git: str | None,
    server: str | None,
    alarm: bool = False,
) -> Text:
    """Return a rich.Text rendering of the trinity rings.

    Color: alarm → red; any drift → amber; aligned → blue.
    """
    pattern = _state_pattern(local, git, server)
    if alarm:
        color = ALARM
    elif pattern == "aligned":
        color = STABLE
    else:
        color = DRIFT

    inner = {
        "aligned":     "  ◉◉◉  ",
        "one_axis":    "  ◉◉ ◉ ",
        "three_axis":  " ◉ ◉ ◉ ",
    }[pattern]

    art = (
        "       ╭─────╮       \n"
        "      ╱       ╲      \n"
        f"     │ {inner} │     \n"
        "      ╲       ╱      \n"
        "       ╰─────╯       "
    )
    return Text(art, style=color)


# ---------------------------------------------------------------------------
# Stabilizer audit table
# ---------------------------------------------------------------------------


def _bracket_status_mark(b: Bracket) -> Text:
    if b.status == STATUS_CONSERVED:
        return Text("✓", style=STABLE)
    if b.status == STATUS_DRIFT_EXPECTED:
        return Text("⚠", style=DRIFT)
    if b.status == STATUS_DRIFT_ALARM:
        return Text("✗", style=ALARM)
    return Text("⊘", style=MUTED)  # pending_implementation


def _bracket_value_short(b: Bracket) -> Text:
    """One-line summary for the table."""
    if b.status == STATUS_CONSERVED:
        return Text(b.descriptor or "{Q, H_S} = 0", style=MUTED)
    if b.status == STATUS_PENDING:
        return Text(b.descriptor or "(pending implementation)", style=MUTED)
    style = ALARM if b.status == STATUS_DRIFT_ALARM else DRIFT
    desc = b.descriptor or str(b.value)
    return Text(desc, style=style)


def render_audit_table(report: AuditReport) -> Table:
    """Compact table: one row per Q_A with status + descriptor."""
    table = Table(
        show_header=True,
        header_style=f"bold {STABLE}",
        border_style=MUTED,
        padding=(0, 1),
    )
    table.add_column(" ", width=2)
    table.add_column("Charge", style=MUTED, no_wrap=True)
    table.add_column("Bracket", overflow="fold")
    for b in report.brackets:
        table.add_row(
            _bracket_status_mark(b),
            Text(b.charge_id),
            _bracket_value_short(b),
        )
    return table


def render_audit_header(report: AuditReport) -> Text:
    """Top line: '{Q_A, H_S} = 0  ∀ A ∈ {1..N}  ✓ | drift | ALARM'"""
    n = len(report.brackets)
    if report.alarm:
        marker = Text(" ALARM", style=f"bold {ALARM}")
    elif any(
        b.status in (STATUS_DRIFT_EXPECTED,) for b in report.brackets
    ):
        marker = Text(" drift", style=DRIFT)
    elif any(b.status == STATUS_PENDING for b in report.brackets):
        marker = Text(" partial", style=MUTED)
    else:
        marker = Text(" ✓", style=STABLE)
    head = Text("Stabilizer audit  {Q_A, H_S} = 0  ", style="bold")
    head.append(f"∀ A ∈ {{1..{n}}}", style=MUTED)
    head.append(marker)
    return head


# ---------------------------------------------------------------------------
# Repo status row
# ---------------------------------------------------------------------------


def render_repo_row(
    repo_name: str,
    local: str | None,
    git: str | None,
    server: str | None,
    short_sha: str | None = None,
) -> Text:
    """One line per repo: 'Reflexivity:  local ✓ git ✓ server ⊘ (commit)'."""

    def mark(present: bool | None, ok: bool) -> Text:
        if present is None:
            return Text("⊘", style=MUTED)
        return Text("✓", style=STABLE) if ok else Text("✗", style=DRIFT)

    line = Text(f"{repo_name:14s}", style=MUTED)
    line.append("local ", style="default")
    line.append(mark(local is not None, local == git))
    line.append(" git ", style="default")
    line.append(mark(git is not None, git is not None and (server is None or git == server)))
    line.append(" server ", style="default")
    line.append(mark(server is not None, server is not None and server == git))
    if short_sha:
        line.append(f"  ({short_sha})", style=MUTED)
    return line


# ---------------------------------------------------------------------------
# Top-level status panel composition
# ---------------------------------------------------------------------------


def render_full_status(
    fingerprint: str,
    rings: Text,
    repo_rows: Iterable[Text],
    audit_header: Text,
    audit_table: Table,
    timestamp_iso: str,
) -> Group:
    """Compose the full `sym status` output."""
    title = Text("Symmetism Coherence: ", style="bold")
    title.append(fingerprint, style=f"bold {STABLE}")
    return Group(
        title,
        Text(""),
        rings,
        Text(""),
        *repo_rows,
        Text(""),
        audit_header,
        Padding(audit_table, (0, 0, 0, 2)),
        Text(""),
        Text(f"Updated: {timestamp_iso}", style=MUTED),
    )
