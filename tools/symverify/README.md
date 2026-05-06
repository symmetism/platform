# SymVerify

The Symmetism verifier. Computes deterministic manifests of the working
tree, the git HEAD, and (after Phase F) the deployed server. Derives
the **trinity fingerprint** when all three agree; the **system fold**
across both repos. Runs the **Stabilizer audit** that enforces
`{Q_A, H_S} = 0` over a registry of conserved charges as the project's
coherence law.

```
sym init               # register both repos, populate registry
sym status             # full audit; trinity rings; bracket table
sym verify-canonical   # re-hash all immutable anchors
sym registry list      # show active Q_A's
sym fold               # cross-repo system fingerprint (Phase G)
sym push <scope> -m    # one-command commit+push+verify (Phase F)
sym log                # chronological journal (Phase E)
sym timeline           # 30-day coherence strip (Phase I)
```

## Install

```bash
# from C:\Symmetism\Platform\tools\symverify
uv tool install -e .
sym --help
```

Requires Python ≥ 3.11. See `_command/03_SOT_STACK.md` for the full
dependency list and `_command/08_FINGERPRINT_SPEC.md` for the
byte-exact algorithm.
