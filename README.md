[![System Fingerprint](https://symmetism.com/api/fingerprint/badge.svg)](https://symmetism.com/verify)
[![Last Verified](https://symmetism.com/api/fingerprint/timestamp.svg)](https://symmetism.com/verify)
[Verify in your browser →](https://symmetism.com/verify)

---

# Platform

The broader Symmetism platform — apps, shared libraries, and tooling
that aren't specifically *about* the Reflexivity physics framework.

## Layout

```
apps/                       general-purpose apps under symmetism.com
  attestation-service/      public attestation surface (publishes deploy fingerprints to a Gist; serves /verify)
docs/                       project documentation
shared/                     libraries shared across apps
tools/                      CLIs and tooling
  symverify/                the Symmetism verifier — manifest, trinity fingerprint, stabilizer audit
server/                     compose layout deployed to the VPS (caddy + watchtower + per-app composes)
```

## Companion repo

[`Symmetism/Reflexivity`](https://github.com/Symmetism/Reflexivity) — the physics framework
itself, plus apps that compute against it (`reflexivity-webapp`, etc.).

## The discipline

Every state-changing operation runs the Stabilizer audit
(`{Q_A, H_S} = 0` per the
[Reflexivity framework](https://github.com/Symmetism/Reflexivity)) over a
registry of conserved charges. The system refuses to advance when any
unexpected bracket is non-zero. See
`tools/symverify/README.md` for the verifier's CLI surface and
`_command/` (operator-local meta) for the build's source-of-truth files.
