"""SymVerify daemon (Phase J).

Three triggers, one audit pipeline:
  J1  filesystem watcher (watchdog) — debounced 500 ms; incremental
      audits on file changes inside either repo.
  J2  wake-from-sleep heuristic — heartbeat thread observes wall-clock
      gaps; >5 min jump triggers a full rescan.
  J3  hourly heartbeat (apscheduler) — full rescan including canonical
      anchor re-verification.

A single TriggerWorker serializes audit invocations through a queue
so concurrent triggers collapse into one audit batch.

Each completed audit:
  - inserts a snapshot row into ~/.symmetism/state/symverify.db
  - atomically rewrites ~/.symmetism/state/status.json
  - logs an event (kind ∈ {filesystem, wake, hourly, manual})
  - on overall-status transitions clean↔drift↔lockdown, generates
    a narrative via OpenAI (rate-limited per-status)

═════════════════════════════════════════════════════════════════════
CLAUDE ORIENTATION
═════════════════════════════════════════════════════════════════════
What runs where:
  ┌──────────────────────────────────────────────────────────────┐
  │  Windows Scheduled Task: pythonw.exe -m symverify daemon     │
  │  (DEFAULT_TASK_NAME, AtLogOn trigger, current user, Limited) │
  └──────────────────────────────────────────────────────────────┘
                                ↓
            spawned process imports this module ONCE
                                ↓
  ┌──────────────────────────────────────────────────────────────┐
  │ TriggerWorker thread     ← queue ←  fs/wake/hourly callbacks │
  │     │                                                        │
  │     ↓  run_audit_cycle() — ~5s on hot cache, ~30s cold       │
  │  state_collect.build_state(repos, servers)                   │
  │  registry.audit(state)                                       │
  │  atomic_write_json(status.json)                              │
  │  db.insert_snapshot + insert_event                           │
  │  _maybe_narrate_transition(...)                              │
  └──────────────────────────────────────────────────────────────┘

Critical pythonw gotchas (caused multi-day bugs in earlier sessions):
  1. sys.stdout / sys.stderr are None under pythonw; logging
     StreamHandler crashes silently. cli.py daemon_cmd reroutes
     stderr+stdout to ~/.symmetism/state/daemon.log + uses
     RotatingFileHandler. Don't bypass.
  2. subprocess (git via git_ops) needs stdin=DEVNULL +
     creationflags=CREATE_NO_WINDOW. Without these, git.exe blocks
     allocating a console under pythonw and the audit hangs forever.
     git_ops.py sets these globally — don't strip them.

Restart required when YOU CHANGE this file or any module imported
here. The running pythonw process is frozen at the import-time state
of every module it pulled. To pick up a change:
  Stop-ScheduledTask "Symmetism SymVerify Daemon"
  Get-Process pythonw | Stop-Process -Force
  Start-ScheduledTask "Symmetism SymVerify Daemon"

Source-of-truth files this daemon reads/writes:
  ~/.symmetism/config/repos.toml          (input: which repos)
  ~/.symmetism/config/servers.toml        (input: which /__manifest urls)
  ~/.symmetism/secrets/symverify.<X>.token (input: per-server tokens)
  ~/.symmetism/secrets/openai.key          (input: narrative API)
  _command/STABILIZER_REGISTRY.json        (input: charge definitions)
  _command/MANIFEST_CANONICAL.json         (input: immutable anchors)
  ~/.symmetism/state/status.json           (output: latest snapshot — what
                                            the GUI + verify page read)
  ~/.symmetism/state/symverify.db          (output: SQLite — sym log/timeline)
  ~/.symmetism/state/daemon.log            (output: rotating log)
  ~/.symmetism/state/manifest_cache.json   (output: SHA-256 cache; safe
                                            to delete to force a fresh full
                                            rehash next cycle)
"""

from __future__ import annotations

import json
import logging
import os
import queue
import signal
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from symverify import __version__, config, db, narrative, state_collect
from symverify.stabilizer import (
    Registry,
    STATUS_CONSERVED,
    STATUS_DRIFT_ALARM,
    STATUS_PENDING,
)

log = logging.getLogger("symverify.daemon")

_DEBOUNCE_SEC = 0.5
_WAKE_INTERVAL_SEC = 30
_WAKE_THRESHOLD_SEC = 300  # 5 minutes
_HOURLY_INTERVAL_SEC = 3600


# ---------------------------------------------------------------------------
# Atomic status.json writer (J4)
# ---------------------------------------------------------------------------


def atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON atomically: temp file in same dir, then os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def status_path() -> Path:
    return config.state_dir() / "status.json"


# ---------------------------------------------------------------------------
# Trigger event
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TriggerEvent:
    """One reason to run an audit."""

    kind: str  # 'filesystem' | 'wake' | 'hourly' | 'manual' | 'startup'
    detail: str = ""
    at: str = field(default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))


# ---------------------------------------------------------------------------
# Audit cycle (one tick)
# ---------------------------------------------------------------------------


# Module-level state used to detect status transitions across audits.
# Single-threaded access via the worker thread, no lock needed.
_LAST_OVERALL_STATUS: str | None = None
# Hold the last narrate timestamp per overall status to rate-limit:
# even if the system flaps between clean and drift repeatedly, we
# don't want to spam the OpenAI API. One narrative per status-change
# is enough.
_LAST_NARRATIVE_AT: dict[str, float] = {}
_NARRATIVE_RATELIMIT_SEC = 600  # 10 min between narratives for same status


def _maybe_narrate_transition(
    overall: str,
    snap_for_narr: dict,
    snapshot_id: int | None,
) -> None:
    """Generate + persist a narrative when overall transitions away from
    'clean' (or back to it). Best-effort: any error is logged and
    swallowed so the audit cycle keeps running."""
    global _LAST_OVERALL_STATUS

    prev = _LAST_OVERALL_STATUS
    _LAST_OVERALL_STATUS = overall

    if prev is None or prev == overall:
        return  # no transition

    # Worth narrating: clean→drift, clean→lockdown, drift→clean,
    # drift→lockdown, lockdown→clean. Skip drift→drift_expected etc.
    interesting = {"clean", "drift", "lockdown"}
    if overall not in interesting and prev not in interesting:
        return

    now_ts = time.time()
    last_at = _LAST_NARRATIVE_AT.get(overall, 0.0)
    if now_ts - last_at < _NARRATIVE_RATELIMIT_SEC:
        log.info("skipping narrative for %s→%s (rate-limited)", prev, overall)
        return
    _LAST_NARRATIVE_AT[overall] = now_ts

    log.info("transition %s → %s — generating narrative", prev, overall)
    try:
        text = narrative.narrate(
            snap_for_narr,
            trigger=f"daemon-transition-{prev}-to-{overall}",
        )
        if text is None:
            text = narrative.unavailable_text(
                "OpenAI client unavailable or API error"
            )
        with db.connect() as conn:
            db.insert_narrative(
                conn,
                trigger=f"transition-{prev}-to-{overall}",
                text=text,
                snapshot_id=snapshot_id,
            )
        log.info("narrative persisted (%d chars)", len(text))
    except Exception as e:
        log.warning("narrative generation failed: %s", e)


def run_audit_cycle(event: TriggerEvent) -> dict:
    """Run a complete audit cycle. Returns the status dict that was
    written to status.json. Safe to call from worker thread."""
    log.info("audit cycle starting (kind=%s)", event.kind)
    repos = config.load_repos()
    servers = config.load_servers()
    log.info("loaded %d repo(s), %d server(s)", len(repos), len(servers or {}))

    log.info("building state (manifests, server polls)...")
    state, meta = state_collect.build_state(repos, servers)
    log.info("state built; meta keys: %s", list(meta.keys()))

    cmd_dir = config.command_dir()
    registry_path = cmd_dir / "STABILIZER_REGISTRY.json"
    if not registry_path.is_file():
        log.warning("registry missing at %s — daemon idling", registry_path)
        return {"updated_at": event.at, "error": "registry missing"}

    registry = Registry.load(registry_path)
    report = registry.audit(state)

    # Derive overall status flag.
    if report.alarm:
        overall = "lockdown"
    elif any(
        b.status not in (STATUS_CONSERVED, STATUS_PENDING)
        for b in report.brackets
    ):
        overall = "drift"
    else:
        overall = "clean"

    fold = state_collect.system_fold(state, meta)

    # Build status payload (Guideline §6 / §7 shape).
    status_payload = {
        "spec": "symverify-fingerprint/1",
        "updated_at": event.at,
        "trigger_kind": event.kind,
        "trigger_detail": event.detail,
        "system_fold": fold,
        "trinity": {
            name: m.get("trinity") for name, m in meta.items() if m.get("trinity")
        },
        "status": overall,
        "alarm": report.alarm,
        "brackets": {b.charge_id: b.to_json() for b in report.brackets},
        "version": __version__,
    }

    log.info("audit cycle: %s fold=%s — writing status.json", overall, fold)
    atomic_write_json(status_path(), status_payload)
    log.info("audit cycle: status.json written")

    # Persist snapshot + event to SQLite.
    snap_id: int | None = None
    try:
        with db.connect() as conn:
            snap_id = db.insert_snapshot(
                conn,
                trinity_r=meta.get("reflexivity", {}).get("trinity"),
                trinity_p=meta.get("platform", {}).get("trinity"),
                system_fold=fold,
                brackets={b.charge_id: b.to_json() for b in report.brackets},
                status=overall,
            )
            db.insert_event(
                conn,
                kind=event.kind,
                repo=None,
                detail={
                    "trigger_detail": event.detail,
                    "snapshot_id": snap_id,
                    "alarm": report.alarm,
                    "overall": overall,
                },
            )
    except Exception as e:
        log.exception("snapshot/event persist failed: %s", e)

    # AI plan: narrate on overall status transitions (clean↔drift↔lockdown).
    # Rate-limited so a flapping system can't blow the OpenAI budget.
    snap_for_narr = {
        "status": overall,
        "trinity_r": meta.get("reflexivity", {}).get("trinity"),
        "trinity_p": meta.get("platform", {}).get("trinity"),
        "system_fold": fold,
        "brackets": status_payload["brackets"],
    }
    _maybe_narrate_transition(overall, snap_for_narr, snap_id)

    return status_payload


# ---------------------------------------------------------------------------
# Trigger worker (one queue, one audit at a time)
# ---------------------------------------------------------------------------


class TriggerWorker:
    """Serialize triggers through one worker thread; collapse bursts."""

    def __init__(self, audit_fn=run_audit_cycle):
        self.queue: "queue.Queue[TriggerEvent | None]" = queue.Queue()
        self.audit_fn = audit_fn
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_status: dict | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop, name="symverify-audit", daemon=True
        )
        self._thread.start()

    def trigger(self, event: TriggerEvent) -> None:
        self.queue.put(event)

    def stop(self) -> None:
        self._stop.set()
        self.queue.put(None)
        if self._thread is not None:
            self._thread.join(timeout=10)

    def _loop(self) -> None:
        while not self._stop.is_set():
            ev = self.queue.get()
            if ev is None:
                return
            # Drain so a burst of events collapses into one audit.
            extras = 0
            try:
                while True:
                    nxt = self.queue.get_nowait()
                    if nxt is None:
                        return
                    extras += 1
            except queue.Empty:
                pass
            if extras:
                log.info("worker drained %d extra events for %s", extras, ev.kind)
            try:
                self.last_status = self.audit_fn(ev)
            except Exception as e:
                log.exception("audit cycle failed: %s", e)


# ---------------------------------------------------------------------------
# Wake-from-sleep heuristic (J2)
# ---------------------------------------------------------------------------


class WakeChecker:
    """Heartbeat thread; treats >threshold wall-clock gap as a wake event."""

    def __init__(
        self,
        on_wake,
        *,
        interval_sec: int = _WAKE_INTERVAL_SEC,
        threshold_sec: int = _WAKE_THRESHOLD_SEC,
    ):
        self.on_wake = on_wake
        self.interval_sec = interval_sec
        self.threshold_sec = threshold_sec
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop, name="symverify-wake", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        last = time.time()
        while not self._stop.wait(self.interval_sec):
            now = time.time()
            elapsed = now - last
            if elapsed > self.threshold_sec:
                try:
                    self.on_wake(elapsed)
                except Exception as e:
                    log.exception("wake callback failed: %s", e)
            last = now


# ---------------------------------------------------------------------------
# Hourly heartbeat (J3)
# ---------------------------------------------------------------------------


class HourlyHeartbeat:
    """APScheduler-based hourly tick. Falls back to a plain Timer if
    apscheduler import fails (it's an optional-feel dep)."""

    def __init__(self, on_tick, *, interval_sec: int = _HOURLY_INTERVAL_SEC):
        self.on_tick = on_tick
        self.interval_sec = interval_sec
        self._scheduler = None
        self._timer: threading.Timer | None = None

    def start(self) -> None:
        try:
            from apscheduler.schedulers.background import BackgroundScheduler

            sched = BackgroundScheduler(daemon=True, timezone="UTC")
            sched.add_job(
                self._tick,
                "interval",
                seconds=self.interval_sec,
                id="hourly_heartbeat",
                next_run_time=datetime.now(timezone.utc),  # tick immediately at start
            )
            sched.start()
            self._scheduler = sched
        except Exception:
            log.warning("apscheduler unavailable; falling back to Timer")
            self._schedule_timer()

    def _schedule_timer(self) -> None:
        self._timer = threading.Timer(self.interval_sec, self._tick_timer)
        self._timer.daemon = True
        self._timer.start()

    def _tick_timer(self) -> None:
        self._tick()
        self._schedule_timer()

    def _tick(self) -> None:
        try:
            self.on_tick()
        except Exception as e:
            log.exception("hourly tick failed: %s", e)

    def stop(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
        if self._timer is not None:
            self._timer.cancel()


# ---------------------------------------------------------------------------
# Filesystem watcher (J1)
# ---------------------------------------------------------------------------


class _DebouncedFsHandler:
    """Translates raw watchdog events into debounced trigger events."""

    def __init__(self, on_change, debounce_sec: float = _DEBOUNCE_SEC):
        self.on_change = on_change
        self.debounce_sec = debounce_sec
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self._last_path = ""

    # watchdog calls these directly via its dispatch
    def dispatch(self, event):
        try:
            if getattr(event, "is_directory", False):
                return
            src = getattr(event, "src_path", "") or ""
            # Skip noisy paths
            for skip in (
                ".git" + os.sep,
                "__pycache__",
                ".pytest_cache",
                "node_modules",
                ".lake" + os.sep,
            ):
                if skip in src:
                    return
            with self._lock:
                self._last_path = src
                if self._timer:
                    self._timer.cancel()
                self._timer = threading.Timer(
                    self.debounce_sec, self._fire
                )
                self._timer.daemon = True
                self._timer.start()
        except Exception as e:
            log.info("fs handler error: %s", e)

    def _fire(self) -> None:
        with self._lock:
            path = self._last_path
        try:
            self.on_change(path)
        except Exception as e:
            log.exception("fs callback failed: %s", e)


class FilesystemWatcher:
    """Watch one or more repo paths; trigger callback on debounced changes."""

    def __init__(self, paths: list[Path], on_change):
        self.paths = paths
        self.on_change = on_change
        self._observer = None
        self._handler = _DebouncedFsHandler(on_change)

    def start(self) -> None:
        try:
            from watchdog.observers import Observer
        except ImportError:
            log.warning("watchdog not installed; filesystem watcher disabled")
            return
        self._observer = Observer()
        for p in self.paths:
            if p.is_dir():
                self._observer.schedule(
                    self._handler, str(p), recursive=True
                )
        self._observer.start()

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)


# ---------------------------------------------------------------------------
# Daemon orchestration
# ---------------------------------------------------------------------------


class Daemon:
    """Compose worker + watchers + heartbeats into a runnable daemon."""

    def __init__(self):
        self.worker = TriggerWorker()
        self.fs = FilesystemWatcher(
            paths=self._repo_paths(),
            on_change=self._on_fs_change,
        )
        self.wake = WakeChecker(on_wake=self._on_wake)
        self.hourly = HourlyHeartbeat(on_tick=self._on_hourly)
        self._stop = threading.Event()

    @staticmethod
    def _repo_paths() -> list[Path]:
        repos = config.load_repos()
        return [rc.path for rc in repos.values() if rc.path.is_dir()]

    # --- callbacks (fire-and-forget triggers) -----------------------------

    def _on_fs_change(self, src_path: str) -> None:
        self.worker.trigger(
            TriggerEvent(kind="filesystem", detail=src_path)
        )

    def _on_wake(self, elapsed_sec: float) -> None:
        self.worker.trigger(
            TriggerEvent(kind="wake", detail=f"clock_jump_{int(elapsed_sec)}s")
        )

    def _on_hourly(self) -> None:
        self.worker.trigger(TriggerEvent(kind="hourly"))

    # --- lifecycle --------------------------------------------------------

    def run(self) -> None:
        log.info("symverify daemon starting (pid %d, version %s)", os.getpid(), __version__)
        self.worker.start()
        self.worker.trigger(TriggerEvent(kind="startup"))
        self.fs.start()
        self.wake.start()
        self.hourly.start()

        # Install signal handlers (best-effort on Windows).
        for sig_name in ("SIGTERM", "SIGINT", "SIGBREAK"):
            sig = getattr(signal, sig_name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, self._sig_handler)
            except (ValueError, OSError):
                # Not on main thread / unsupported on this platform
                pass

        try:
            while not self._stop.is_set():
                self._stop.wait(60)
        finally:
            self.shutdown()

    def _sig_handler(self, signum, _frame) -> None:
        log.info("daemon received signal %d", signum)
        self._stop.set()

    def shutdown(self) -> None:
        log.info("daemon shutting down")
        self.fs.stop()
        self.wake.stop()
        self.hourly.stop()
        self.worker.stop()


# ---------------------------------------------------------------------------
# Service install / uninstall (Windows Task Scheduler)
# ---------------------------------------------------------------------------

DEFAULT_TASK_NAME = "Symmetism SymVerify Daemon"


def _find_pythonw() -> str:
    """Path to pythonw.exe so the task runs without a console window."""
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        candidate = exe[:-len("python.exe")] + "pythonw.exe"
        if Path(candidate).is_file():
            return candidate
    # Fallback: just use python.exe (may flash a console).
    return exe


def _ps_quote(s: str) -> str:
    """Wrap a string in PowerShell single quotes, doubling embedded single
    quotes (PS literal-string escape rules)."""
    return "'" + s.replace("'", "''") + "'"


def _run_powershell(script: str) -> tuple[int, str, str]:
    """Run a PowerShell command via powershell.exe; return (rc, stdout, stderr)."""
    import subprocess

    res = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        capture_output=True,
        text=True,
    )
    return res.returncode, res.stdout, res.stderr


def install_windows_service(task_name: str = DEFAULT_TASK_NAME) -> None:
    """Create/replace a Scheduled Task that runs `sym daemon` at logon.

    Uses the PowerShell ScheduledTasks cmdlets (Register-ScheduledTask)
    rather than schtasks.exe because Windows 11 frequently rejects
    schtasks /create from non-elevated shells with "Access is denied",
    while Register-ScheduledTask succeeds for the current user.
    """
    import os as _os

    pythonw = _find_pythonw()
    user = f"{_os.environ.get('COMPUTERNAME', '')}\\{_os.environ.get('USERNAME', '')}".strip("\\")
    if not user:
        raise RuntimeError("could not determine current user (COMPUTERNAME/USERNAME unset)")

    script = f"""
$ErrorActionPreference = 'Stop'
$action = New-ScheduledTaskAction -Execute {_ps_quote(pythonw)} -Argument '-m symverify daemon'
$trigger = New-ScheduledTaskTrigger -AtLogOn -User {_ps_quote(user)}
$principal = New-ScheduledTaskPrincipal -UserId {_ps_quote(user)} -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Days 0) -Priority 7
Register-ScheduledTask -TaskName {_ps_quote(task_name)} -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
""".strip()
    rc, out, err = _run_powershell(script)
    if rc != 0:
        raise RuntimeError(
            f"Register-ScheduledTask failed: {(err or out).strip()}"
        )


def uninstall_windows_service(task_name: str = DEFAULT_TASK_NAME) -> None:
    """Delete the scheduled task. Idempotent (no error if absent)."""
    script = (
        "if (Get-ScheduledTask -TaskName "
        f"{_ps_quote(task_name)} -ErrorAction SilentlyContinue) {{ "
        f"Unregister-ScheduledTask -TaskName {_ps_quote(task_name)} -Confirm:$false }}"
    )
    rc, out, err = _run_powershell(script)
    if rc != 0:
        raise RuntimeError(
            f"Unregister-ScheduledTask failed: {(err or out).strip()}"
        )


def query_windows_service(task_name: str = DEFAULT_TASK_NAME) -> dict | None:
    """Return basic info about the scheduled task, or None if not installed."""
    script = f"""
$t = Get-ScheduledTask -TaskName {_ps_quote(task_name)} -ErrorAction SilentlyContinue
if ($null -eq $t) {{ exit 2 }}
$i = Get-ScheduledTaskInfo -TaskName {_ps_quote(task_name)}
'TaskName: ' + $t.TaskName
'Status: '   + $t.State
'Last Run Time: ' + $i.LastRunTime
'Next Run Time: ' + $i.NextRunTime
'Last Result: '   + ('0x{{0:X8}}' -f $i.LastTaskResult)
""".strip()
    rc, out, err = _run_powershell(script)
    if rc != 0:
        return None
    info: dict[str, str] = {}
    for line in out.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            info[k.strip()] = v.strip()
    return info or None
