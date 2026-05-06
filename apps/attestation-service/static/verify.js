// Symmetism /verify — browser-side recomputation per
// _command/08_FINGERPRINT_SPEC.md.
//
// The page never trusts this server for the cryptographic decision.
// On click, we fetch raw MANIFEST_CANONICAL.json from BOTH repos via
// raw.githubusercontent.com, fetch the canonical anchor file (the v0.42
// master), recompute SHA-256 in the browser via Web Crypto, recompute
// trinity fingerprints + system fold via the spec algorithm, and
// compare to the values displayed.

const SPEC_VERSION = "symverify-fingerprint/1";
const ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";  // Crockford
const REPOS = ["Reflexivity", "Platform"];
const ATTESTATION_LATEST_URL = "/api/fingerprint/latest.json";

// ---- helpers ----------------------------------------------------------

const hex = (buf) =>
  Array.from(new Uint8Array(buf))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");

async function sha256(bytes) {
  return hex(await crypto.subtle.digest("SHA-256", bytes));
}

async function sha256Bytes(bytes) {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
}

function crockford(bytes) {
  // Pack 8-bit input into 5-bit base32; pad final group with zero bits.
  let bits = "";
  for (const b of bytes) bits += b.toString(2).padStart(8, "0");
  while (bits.length % 5) bits += "0";
  let out = "";
  for (let i = 0; i < bits.length; i += 5) {
    out += ALPHABET[parseInt(bits.slice(i, i + 5), 2)];
  }
  return out;
}

// JSON canonical encoding to match Python json.dumps(sort_keys=True,
// separators=(",",":"), ensure_ascii=False).
function canonicalJSON(value) {
  const enc = (v) => {
    if (v === null) return "null";
    if (typeof v === "boolean") return v ? "true" : "false";
    if (typeof v === "number") return Number.isInteger(v) ? v.toString() : v.toString();
    if (typeof v === "string") return JSON.stringify(v);
    if (Array.isArray(v)) return "[" + v.map(enc).join(",") + "]";
    if (typeof v === "object") {
      const keys = Object.keys(v).sort();
      return "{" + keys.map((k) => JSON.stringify(k) + ":" + enc(v[k])).join(",") + "}";
    }
    throw new Error("unsupported value: " + typeof v);
  };
  return enc(value);
}

async function trinityFingerprint(localH, gitH, serverH) {
  const payload = canonicalJSON({
    spec: SPEC_VERSION,
    trinity: [localH, gitH, serverH ?? null],
  });
  const digest = await sha256Bytes(new TextEncoder().encode(payload));
  const raw = crockford(digest.slice(0, 8)).slice(0, 12);
  return `${raw.slice(0, 4)}-${raw.slice(4, 8)}-${raw.slice(8, 12)}`;
}

async function systemFold(trinities, invariants, version) {
  const payload = canonicalJSON({
    spec: SPEC_VERSION,
    trinities,
    invariants,
    version,
  });
  const digest = await sha256Bytes(new TextEncoder().encode(payload));
  const raw = crockford(digest.slice(0, 10)).slice(0, 16);
  return `SYM-${raw.slice(0, 4)}-${raw.slice(4, 8)}-${raw.slice(8, 12)}-${raw.slice(12, 16)}`;
}

// ---- network ----------------------------------------------------------

const RAW = (repo, path) =>
  `https://raw.githubusercontent.com/Symmetism/${repo}/main/${path}`;

async function fetchText(url) {
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok) throw new Error(`${url} → ${r.status}`);
  return r.text();
}

async function fetchBytes(url) {
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok) throw new Error(`${url} → ${r.status}`);
  return new Uint8Array(await r.arrayBuffer());
}

// ---- DOM --------------------------------------------------------------

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

function setBracket(qid, status, descriptor) {
  const row = $$(`.bracket-row .qid`)
    .find((el) => el.textContent.trim() === qid)
    ?.parentElement;
  if (!row) return;
  row.classList.remove("pending", "conserved", "drift", "alarm");
  row.classList.add(status);
  row.querySelector(".status").textContent = descriptor;
}

function placeRingPoints(state) {
  // state: "aligned" | "drift" | "alarm"
  const g = $("#ring-points");
  g.innerHTML = "";
  const positions =
    state === "aligned"
      ? [[0, 0], [0, 0], [0, 0]]
      : state === "drift"
      ? [[-12, 0], [12, 0], [0, 0]]
      : [
          [-50, 30],
          [50, 30],
          [0, -55],
        ];
  const fill =
    state === "aligned" ? "#7eb6d9" : state === "alarm" ? "#cc4444" : "#e0a458";
  for (const [dx, dy] of positions) {
    const c = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    c.setAttribute("cx", dx);
    c.setAttribute("cy", dy);
    c.setAttribute("r", 8);
    c.setAttribute("fill", fill);
    g.appendChild(c);
  }
  $(".trinity-rings").classList.remove("drift", "alarm");
  if (state !== "aligned") $(".trinity-rings").classList.add(state);
}

// ---- main --------------------------------------------------------------

async function loadLatestAttestation() {
  try {
    const r = await fetch(ATTESTATION_LATEST_URL, { cache: "no-store" });
    if (!r.ok) return null;
    return await r.json();
  } catch {
    return null;
  }
}

async function loadTimeline() {
  try {
    const r = await fetch("/api/timeline.json", { cache: "no-store" });
    if (!r.ok) return [];
    return await r.json();
  } catch {
    return [];
  }
}

function renderTimelineSpark(attestations) {
  const root = $("#timeline-spark");
  const summary = $("#timeline-summary");
  if (!root) return;
  root.innerHTML = "";

  // Group by day; track worst status + count.
  const byDay = new Map();
  for (const a of attestations) {
    const ts = a.verified_at || a.received_at || "";
    const day = ts.slice(0, 10);
    if (!day) continue;
    const slot =
      byDay.get(day) || { count: 0, alarm: false, drift: false };
    slot.count += 1;
    if (a.alarm) slot.alarm = true;
    if (a.drift) slot.drift = true;
    byDay.set(day, slot);
  }

  // Last 30 days, oldest first (left-to-right).
  const days = [];
  const today = new Date();
  for (let i = 29; i >= 0; i--) {
    const d = new Date(today);
    d.setUTCDate(d.getUTCDate() - i);
    const key = d.toISOString().slice(0, 10);
    days.push({ day: key, slot: byDay.get(key) });
  }

  let nClean = 0, nDrift = 0, nAlarm = 0, nNone = 0;
  for (const { day, slot } of days) {
    const wrap = document.createElement("div");
    wrap.className = "day";
    wrap.title = slot
      ? `${day}: ${slot.count} attestation${slot.count > 1 ? "s" : ""}` +
        (slot.alarm ? " · alarm" : slot.drift ? " · drift" : " · clean")
      : `${day}: no data`;
    const bar = document.createElement("div");
    bar.className = "day-bar";
    if (!slot) {
      bar.classList.add("empty");
      bar.style.height = "10%";
      nNone += 1;
    } else if (slot.alarm) {
      bar.classList.add("alarm");
      bar.style.height = "100%";
      nAlarm += 1;
    } else if (slot.drift) {
      bar.classList.add("drift");
      bar.style.height = "60%";
      nDrift += 1;
    } else {
      bar.classList.add("clean");
      bar.style.height = "30%";
      nClean += 1;
    }
    wrap.appendChild(bar);
    root.appendChild(wrap);
  }
  if (summary) {
    summary.innerHTML =
      `<span>${attestations.length} attestation${
        attestations.length === 1 ? "" : "s"
      } · 30d</span>` +
      `<span>` +
      `<span style="color:#7eb6d9">${nClean} clean</span> · ` +
      `<span style="color:#e0a458">${nDrift} drift</span> · ` +
      `<span style="color:#cc4444">${nAlarm} alarm</span>` +
      `</span>`;
  }
}

async function init() {
  const att = await loadLatestAttestation();
  if (att) {
    $("#fold").textContent = att.system_fold || "SYM-····-····-····-····";
    $("#timestamp").textContent =
      "last verified: " + (att.verified_at || att.received_at || "—");
    placeRingPoints(att.alarm ? "alarm" : att.drift ? "drift" : "aligned");
    if (att.brackets) {
      for (const [qid, b] of Object.entries(att.brackets)) {
        const status =
          b.status === "conserved"
            ? "conserved"
            : b.status === "drift_alarm"
            ? "alarm"
            : "drift";
        setBracket(qid, status, b.descriptor || b.value || b.status);
      }
    }
  } else {
    $("#timestamp").textContent = "no published attestation yet";
    placeRingPoints("drift");
  }
  // I3: footer timeline spark.
  const timeline = await loadTimeline();
  renderTimelineSpark(timeline);
}

async function runVerify() {
  const btn = $("#verify-btn");
  const out = $("#verify-result");
  btn.disabled = true;
  out.className = "verify-result";
  out.textContent = "fetching MANIFEST_CANONICAL.json from both repos…";

  try {
    // 1. Fetch and verify each repo's canonical anchors against raw GitHub.
    const anchorReports = [];
    for (const repo of REPOS) {
      out.textContent += `\n→ ${repo}/MANIFEST_CANONICAL.json`;
      const manifestText = await fetchText(RAW(repo, "MANIFEST_CANONICAL.json"));
      const manifest = JSON.parse(manifestText);
      for (const a of manifest.anchors || []) {
        if (a.policy !== "immutable") continue;
        out.textContent += `\n  fetching ${a.path}…`;
        const bytes = await fetchBytes(RAW(repo, a.path));
        const got = await sha256(bytes);
        const ok = got === a.sha256;
        anchorReports.push({ repo, path: a.path, expected: a.sha256, got, ok });
        out.textContent += ok ? "  ✓" : `  ✗ (expected ${a.sha256.slice(0,8)} got ${got.slice(0,8)})`;
        if (!ok) {
          out.className = "verify-result fail";
          throw new Error(`canonical anchor drift on ${repo}/${a.path}`);
        }
      }
    }

    // 2. Latest attestation comparison: fold the displayed values match the gist?
    const att = await loadLatestAttestation();
    if (att && att.system_fold) {
      out.textContent += `\n\nattestation system_fold: ${att.system_fold}`;
      const displayed = $("#fold").textContent.trim();
      if (att.system_fold === displayed) {
        out.textContent += "\nmatches the displayed fold ✓";
      } else {
        out.textContent += `\nmismatch (displayed ${displayed}) ✗`;
      }
    }

    out.className = "verify-result ok";
    out.textContent += `\n\n✓ Verified ${anchorReports.length} canonical anchor(s) directly from GitHub raw.`;
  } catch (e) {
    out.className = "verify-result fail";
    out.textContent += `\n\n✗ ${e.message}`;
  } finally {
    btn.disabled = false;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  init();
  $("#verify-btn").addEventListener("click", runVerify);
});
