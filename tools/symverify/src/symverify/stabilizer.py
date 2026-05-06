"""Stabilizer registry, bracket computation, six base conserved charges.

Implements `{Q_A, H_S} = 0` as the project's coherence law (Rule R4 /
`_command/07_EQUATION_INTEGRATION.md`). Every state-changing operation
runs `Registry.audit(state)` and refuses to proceed if any non-expected
bracket is non-zero.

Compute-function dispatch is via `compute_func` strings stored in the
registry JSON; we resolve them to Python callables via a static map so
the JSON remains language-agnostic and we don't import-eval arbitrary
strings at runtime.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from symverify import git_ops


# ---------------------------------------------------------------------------
# Status enumeration (matches what's serialized into BRACKETS.json)
# ---------------------------------------------------------------------------

STATUS_CONSERVED = "conserved"
STATUS_DRIFT_EXPECTED = "drift_expected"
STATUS_DRIFT_ALARM = "drift_alarm"
STATUS_PENDING = "pending_implementation"

STRUCTURE_PATHS = (
    ".githooks/pre-commit",
    ".github/workflows/verify-canonical.yml",
    ".github/CODEOWNERS",
    "MANIFEST_CANONICAL.json",
    ".gitattributes",
)

# Conservative, high-precision secret-pattern set. False positives are a
# bigger problem here than false negatives — Q_secrets goes alarm on any
# match, halting all forward operations. If a real leak happens it'll
# usually be caught by the pre-push gitleaks scan; this is a defense in
# depth, not the primary detector.
SECRET_PATTERNS = [
    re.compile(rb"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(rb"ghp_[A-Za-z0-9]{36}"),
    re.compile(rb"gho_[A-Za-z0-9]{36}"),
    re.compile(rb"ghs_[A-Za-z0-9]{36}"),
    re.compile(rb"ghu_[A-Za-z0-9]{36}"),
    re.compile(rb"ghr_[A-Za-z0-9]{36}"),
    re.compile(rb"sk-proj-[A-Za-z0-9_-]{50,}"),
    re.compile(rb"sk-(?!proj)[A-Za-z0-9]{40,}"),  # OpenAI keys (non-proj)
    re.compile(rb"AKIA[0-9A-Z]{16}"),  # AWS access key
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----"),
]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Bracket:
    """A single charge's bracket value at audit time."""

    charge_id: str
    value: Any  # 0 (int) for conserved; descriptor str/dict for drift
    status: str  # one of STATUS_*
    descriptor: str | None = None
    matched_pattern: str | None = None  # for drift_expected, which pattern

    def to_json(self) -> dict:
        out: dict[str, Any] = {"value": self.value, "status": self.status}
        if self.descriptor:
            out["descriptor"] = self.descriptor
        if self.matched_pattern:
            out["matched_pattern"] = self.matched_pattern
        return out


@dataclass(frozen=True, slots=True)
class ChargeSpec:
    """One row in STABILIZER_REGISTRY.json's `charges` array."""

    id: str
    ordinal: int
    description: str
    compute_func: str
    expected_value: Any
    expected_value_source: str
    expected_nonzero: list[dict]
    alarm_on_nonzero: bool
    added_at: str
    added_in_step: str

    @classmethod
    def from_dict(cls, d: dict) -> "ChargeSpec":
        return cls(
            id=d["id"],
            ordinal=d["ordinal"],
            description=d["description"],
            compute_func=d["compute_func"],
            expected_value=d.get("expected_value"),
            expected_value_source=d.get("expected_value_source", ""),
            expected_nonzero=d.get("expected_nonzero", []),
            alarm_on_nonzero=d.get("alarm_on_nonzero", True),
            added_at=d.get("added_at", ""),
            added_in_step=d.get("added_in_step", ""),
        )


@dataclass(slots=True)
class State:
    """Observed system state — input to each compute_func.

    Optional fields default to None / empty, so callers can populate
    only what's relevant for the charges they want to audit. Charges
    whose required state is missing return a `pending_implementation`
    bracket rather than crashing.
    """

    reflexivity_path: Path | None = None
    platform_path: Path | None = None

    # Trinity inputs (root_hash hex strings)
    reflexivity_local_hash: str | None = None
    reflexivity_git_hash: str | None = None
    reflexivity_server_hash: str | None = None
    platform_local_hash: str | None = None
    platform_git_hash: str | None = None
    platform_server_hash: str | None = None

    # Cross-repo invariants pre-computed by the caller
    invariants_R: dict[str, str] = field(default_factory=dict)
    invariants_P: dict[str, str] = field(default_factory=dict)

    # SymVerify version (typically symverify.__version__)
    symverify_version: str = "0.1.0"


@dataclass(frozen=True, slots=True)
class AuditReport:
    """Result of running every charge's compute_func against the state."""

    audit_run_at: str  # ISO-8601 UTC
    brackets: list[Bracket]
    alarm: bool

    def to_json(self) -> dict:
        return {
            "audit_run_at": self.audit_run_at,
            "alarm": self.alarm,
            "brackets": {b.charge_id: b.to_json() for b in self.brackets},
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _expected_pattern_matches(
    descriptor: str, patterns: list[dict]
) -> str | None:
    """Return matched pattern name if descriptor matches an expected_nonzero entry."""
    for p in patterns:
        if p.get("pattern") and p["pattern"] in descriptor:
            return p["pattern"]
    return None


def _classify_drift(
    charge: ChargeSpec, descriptor: str
) -> tuple[str, str | None]:
    """Determine status (drift_expected vs drift_alarm) for a non-zero bracket."""
    matched = _expected_pattern_matches(descriptor, charge.expected_nonzero)
    if matched is not None:
        return STATUS_DRIFT_EXPECTED, matched
    return (
        STATUS_DRIFT_ALARM if charge.alarm_on_nonzero else STATUS_DRIFT_EXPECTED,
        None,
    )


# ---------------------------------------------------------------------------
# Compute functions — six base charges
# ---------------------------------------------------------------------------


def compute_q_canonical(state: State, charge: ChargeSpec) -> Bracket:
    """Q_canonical: SHA-256(concat(path-sorted immutable file SHA-256s)).

    Per `_command/07_EQUATION_INTEGRATION.md` §3. We hash the BLOB
    bytes (via `git cat-file`), not the working tree — the canonical
    pin is over the immutable state, not the operator's potentially
    autocrlf-mangled local copy.
    """
    repos: list[Path] = [
        p for p in (state.reflexivity_path, state.platform_path) if p is not None
    ]
    if not repos:
        return Bracket(
            charge_id=charge.id,
            value=None,
            status=STATUS_PENDING,
            descriptor="no repo paths supplied",
        )

    # Collect all immutable anchors across both repos.
    digests: list[tuple[str, str]] = []  # (path, sha256)
    for repo in repos:
        manifest_path = repo / "MANIFEST_CANONICAL.json"
        if not manifest_path.is_file():
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for anchor in manifest.get("anchors", []):
            if anchor.get("policy") != "immutable":
                continue
            rel = anchor["path"]
            try:
                blob = git_ops.run_git_bytes(repo, "show", f"HEAD:{rel}")
            except git_ops.GitError as e:
                return Bracket(
                    charge_id=charge.id,
                    value=str(e),
                    status=STATUS_DRIFT_ALARM,
                    descriptor=f"failed to read blob for {rel}: {e}",
                )
            sha = hashlib.sha256(blob).hexdigest()
            digests.append((rel, sha))

    if not digests:
        # No immutable anchors anywhere — vacuously conserved.
        return Bracket(
            charge_id=charge.id,
            value=0,
            status=STATUS_CONSERVED,
            descriptor="no immutable anchors registered",
        )

    digests.sort(key=lambda pair: pair[0].encode("utf-8"))
    concat = "".join(sha for _, sha in digests).encode("utf-8")
    computed = hashlib.sha256(concat).hexdigest()
    if computed == charge.expected_value:
        return Bracket(
            charge_id=charge.id,
            value=0,
            status=STATUS_CONSERVED,
            descriptor=f"sha256(concat({len(digests)} sorted hex digests)) = {computed[:16]}…",
        )
    return Bracket(
        charge_id=charge.id,
        value=computed,
        status=STATUS_DRIFT_ALARM,
        descriptor=f"computed {computed} ≠ expected {charge.expected_value}",
    )


def compute_q_structure(state: State, charge: ChargeSpec) -> Bracket:
    """Q_structure: SHA-256 of canonical-JSON of {path: present?} entries.

    We enumerate the structural-skeleton paths in both repos and
    record presence (boolean). The hash of this canonical JSON IS the
    structural value. Drift = some path is missing.
    """
    repos = {
        "reflexivity": state.reflexivity_path,
        "platform": state.platform_path,
    }
    if not any(repos.values()):
        return Bracket(
            charge_id=charge.id,
            value=None,
            status=STATUS_PENDING,
            descriptor="no repo paths supplied",
        )

    presence: dict[str, dict[str, bool]] = {}
    missing: list[str] = []
    for repo_name, repo_path in repos.items():
        if repo_path is None:
            continue
        presence[repo_name] = {}
        for sp in STRUCTURE_PATHS:
            exists = (repo_path / sp).is_file()
            presence[repo_name][sp] = exists
            if not exists:
                missing.append(f"{repo_name}/{sp}")

    payload = json.dumps(
        presence, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    computed = hashlib.sha256(payload).hexdigest()

    if (
        charge.expected_value
        and isinstance(charge.expected_value, str)
        and not charge.expected_value.startswith("<")
        and computed == charge.expected_value
    ):
        return Bracket(
            charge_id=charge.id,
            value=0,
            status=STATUS_CONSERVED,
            descriptor=f"all {len(STRUCTURE_PATHS)} paths present in both repos; hash {computed[:16]}…",
        )

    if missing:
        return Bracket(
            charge_id=charge.id,
            value=computed,
            status=STATUS_DRIFT_ALARM,
            descriptor=f"missing structural paths: {', '.join(missing)}",
        )

    # All present; expected_value unset (placeholder) — first-compute
    # state, callers should populate expected_value via Registry.update.
    return Bracket(
        charge_id=charge.id,
        value=computed,
        status=STATUS_PENDING,
        descriptor=(
            f"all paths present; computed {computed[:16]}… but registry "
            f"expected_value is placeholder ({charge.expected_value!r})"
        ),
    )


def _trinity_bracket(
    charge: ChargeSpec,
    local: str | None,
    git: str | None,
    server: str | None,
) -> Bracket:
    """Shared trinity logic for Q_trinity_R / Q_trinity_P."""
    if local is None and git is None:
        return Bracket(
            charge_id=charge.id,
            value=None,
            status=STATUS_PENDING,
            descriptor="trinity inputs not provided",
        )

    parts: list[str] = []
    if local is not None and git is not None and local != git:
        parts.append("local≠git")
    if server is None:
        parts.append("no_server_yet")
    else:
        if git is not None and git != server:
            parts.append("git≠server")
        # History rewrite detector: local agrees with server, but git
        # diverges from both. Should be impossible under branch protection
        # — it's the signature of someone rewriting committed history.
        if (
            local is not None
            and git is not None
            and local == server
            and git != local
        ):
            parts.append("history_rewrite")

    if not parts:
        return Bracket(
            charge_id=charge.id,
            value=0,
            status=STATUS_CONSERVED,
            descriptor=(
                f"local={local[:8] if local else 'None'}…  "
                f"git={git[:8] if git else 'None'}…  "
                f"server={server[:8] if server else 'None'}"
            ),
        )

    descriptor = "; ".join(parts)
    if "history_rewrite" in parts:
        return Bracket(
            charge_id=charge.id,
            value=descriptor,
            status=STATUS_DRIFT_ALARM,
            descriptor=descriptor,
        )

    status, matched = _classify_drift(charge, descriptor)
    return Bracket(
        charge_id=charge.id,
        value=descriptor,
        status=status,
        descriptor=descriptor,
        matched_pattern=matched,
    )


def compute_q_trinity_R(state: State, charge: ChargeSpec) -> Bracket:
    return _trinity_bracket(
        charge,
        state.reflexivity_local_hash,
        state.reflexivity_git_hash,
        state.reflexivity_server_hash,
    )


def compute_q_trinity_P(state: State, charge: ChargeSpec) -> Bracket:
    return _trinity_bracket(
        charge,
        state.platform_local_hash,
        state.platform_git_hash,
        state.platform_server_hash,
    )


def compute_q_cross_repo(state: State, charge: ChargeSpec) -> Bracket:
    """Q_cross_repo: hash of canonical-JSON of cross-repo invariants.

    The invariants are populated by the caller and must agree across
    repos. Drift = mismatch on any invariant (license SHA, schema
    version, etc.).
    """
    if not state.invariants_R and not state.invariants_P:
        return Bracket(
            charge_id=charge.id,
            value=None,
            status=STATUS_PENDING,
            descriptor="cross-repo invariants not provided",
        )

    keys = sorted(set(state.invariants_R) | set(state.invariants_P))
    mismatches: list[str] = []
    aligned: dict[str, str] = {}
    for k in keys:
        rv = state.invariants_R.get(k)
        pv = state.invariants_P.get(k)
        if rv is not None and pv is not None and rv != pv:
            mismatches.append(f"{k}: R={rv} P={pv}")
            continue
        aligned[k] = rv if rv is not None else (pv or "")

    payload = json.dumps(aligned, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    computed = hashlib.sha256(payload).hexdigest()

    if mismatches:
        return Bracket(
            charge_id=charge.id,
            value=computed,
            status=STATUS_DRIFT_ALARM,
            descriptor=f"cross-repo invariant mismatch: {'; '.join(mismatches)}",
        )

    if (
        charge.expected_value
        and isinstance(charge.expected_value, str)
        and not charge.expected_value.startswith("<")
    ):
        if computed == charge.expected_value:
            return Bracket(
                charge_id=charge.id,
                value=0,
                status=STATUS_CONSERVED,
                descriptor=f"{len(aligned)} invariants aligned; hash {computed[:16]}…",
            )
        return Bracket(
            charge_id=charge.id,
            value=computed,
            status=STATUS_DRIFT_ALARM,
            descriptor=f"computed {computed[:16]}… ≠ expected {charge.expected_value[:16]}…",
        )

    return Bracket(
        charge_id=charge.id,
        value=computed,
        status=STATUS_PENDING,
        descriptor=(
            f"{len(aligned)} invariants aligned; computed {computed[:16]}… "
            f"but expected_value is placeholder"
        ),
    )


def compute_q_secrets(state: State, charge: ChargeSpec) -> Bracket:
    """Q_secrets: 0 iff no secret-pattern matches in tracked files of either repo.

    We delegate enumeration to `git ls-files` (tracked files only;
    secret material in ignored / untracked paths is by definition not
    in the repository's history). For each tracked file we read the
    BLOB bytes, not the working-tree — symmetric with how Q_canonical
    treats provenance.
    """
    repos = [
        p for p in (state.reflexivity_path, state.platform_path) if p is not None
    ]
    if not repos:
        return Bracket(
            charge_id=charge.id,
            value=None,
            status=STATUS_PENDING,
            descriptor="no repo paths supplied",
        )

    matches: list[str] = []
    for repo in repos:
        for mode, blob_sha, path in git_ops.git_ls_tree(repo):
            try:
                blob = git_ops.git_cat_file_blob(repo, blob_sha)
            except git_ops.GitError:
                continue
            for pat in SECRET_PATTERNS:
                if pat.search(blob):
                    matches.append(f"{repo.name}:{path} ({pat.pattern!r})")
                    break  # one match per file is enough to flag

    if not matches:
        return Bracket(
            charge_id=charge.id,
            value=0,
            status=STATUS_CONSERVED,
            descriptor=f"scanned {sum(1 for _ in repos)} repos; 0 matches",
        )

    return Bracket(
        charge_id=charge.id,
        value=len(matches),
        status=STATUS_DRIFT_ALARM,
        descriptor=f"{len(matches)} secret-pattern match(es): {'; '.join(matches[:5])}",
    )


# ---------------------------------------------------------------------------
# Dispatch table — string -> callable
# ---------------------------------------------------------------------------

COMPUTE_FUNCS: dict[str, Callable[[State, ChargeSpec], Bracket]] = {
    "symverify.stabilizer.compute_q_canonical": compute_q_canonical,
    "symverify.stabilizer.compute_q_structure": compute_q_structure,
    "symverify.stabilizer.compute_q_trinity_R": compute_q_trinity_R,
    "symverify.stabilizer.compute_q_trinity_P": compute_q_trinity_P,
    "symverify.stabilizer.compute_q_cross_repo": compute_q_cross_repo,
    "symverify.stabilizer.compute_q_secrets": compute_q_secrets,
}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Registry:
    """Loaded view of `_command/STABILIZER_REGISTRY.json`."""

    spec_version: str
    framework_dim_stab: int
    charges: list[ChargeSpec]
    raw: dict  # original parsed JSON, for round-trip preservation

    @classmethod
    def load(cls, path: str | Path) -> "Registry":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            spec_version=raw.get("spec_version", "stabilizer-registry/1"),
            framework_dim_stab=raw.get("framework_dim_stab", 6),
            charges=[ChargeSpec.from_dict(c) for c in raw.get("charges", [])],
            raw=raw,
        )

    def save(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(self.raw, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def audit(self, state: State) -> AuditReport:
        brackets: list[Bracket] = []
        alarm = False
        for charge in self.charges:
            fn = COMPUTE_FUNCS.get(charge.compute_func)
            if fn is None:
                brackets.append(
                    Bracket(
                        charge_id=charge.id,
                        value=None,
                        status=STATUS_PENDING,
                        descriptor=f"compute_func {charge.compute_func!r} not registered",
                    )
                )
                continue
            try:
                b = fn(state, charge)
            except Exception as e:  # pragma: no cover — defensive
                b = Bracket(
                    charge_id=charge.id,
                    value=str(e),
                    status=STATUS_DRIFT_ALARM,
                    descriptor=f"compute exception: {type(e).__name__}: {e}",
                )
            brackets.append(b)
            if b.status == STATUS_DRIFT_ALARM:
                alarm = True
        return AuditReport(
            audit_run_at=_utc_now(),
            brackets=brackets,
            alarm=alarm,
        )
