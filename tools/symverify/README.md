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
sym scaffold <repo>/X  # generate a Symmetism-tracked app skeleton
sym log                # chronological journal (Phase E)
sym timeline           # 30-day coherence strip (Phase I)
sym daemon             # filesystem + wake + hourly heartbeat (Phase J)
sym install-service    # register the daemon as a Windows Scheduled Task
sym gui                # launch the CustomTkinter GUI window
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

## GUI (`sym gui` / standalone Symmetism.exe)

The optional GUI is a single-window CustomTkinter app that reads
`~/.symmetism/state/status.json` and renders the trinity, bracket
table, recent events, and last narrative live (5 s refresh). Buttons
trigger a manual audit, ask the OpenAI narrator to explain the
current snapshot, open the verify page, open the daemon log file,
and install/uninstall the daemon Scheduled Task.

```bash
pip install -e .[gui]      # adds customtkinter
sym gui
```

To produce a standalone double-clickable `Symmetism.exe` (no Python
needed on the target box):

```bash
pip install -e .[exe]      # adds customtkinter + pyinstaller
python build_exe.py        # output: dist/Symmetism.exe (~40 MB)
```

The bundled exe runs the same GUI code as `sym gui` and shares the
same `~/.symmetism/{config,state,secrets}` directories. Pin to the
taskbar; double-click to open.
