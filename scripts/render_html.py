#!/usr/bin/env python3
"""Render the full HTML report from $DATA_DIR/report_data.json to $OUT_FILE."""

import json, os, html, sys
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/tmp/_data"))
OUT_FILE = Path(os.environ.get("OUT_FILE", str(Path.home() / "projects/leaderboard/out/contributors.html")))
PAGE_TITLE = os.environ.get("PAGE_TITLE", "agent-orchestrator — Contributor Impact")
PAGE_SUBTITLE = os.environ.get("PAGE_SUBTITLE", "ComposioHQ/agent-orchestrator · main branch")

data = json.load(open(DATA_DIR / "report_data.json"))
data_json = json.dumps(data, indent=None, separators=(",", ":"))

HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>__PAGE_TITLE__</title>
<style>
  :root {
    --bg: #0d1117; --panel: #161b22; --panel-2: #1c2128; --border: #30363d;
    --text: #e6edf3; --muted: #8b949e; --accent: #58a6ff;
    --c-commit: #58a6ff; --c-added: #3fb950; --c-deleted: #f85149;
    --c-pr: #d2a8ff; --c-review: #ffa657; --c-weighted: #79c0ff;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 28px; background: var(--bg); color: var(--text);
    font: 14px/1.5 -apple-system, system-ui, "Segoe UI", sans-serif;
    max-width: 1440px; margin-inline: auto;
  }
  h1 { font-size: 26px; margin: 0 0 4px; }
  h2 { font-size: 18px; margin: 28px 0 10px; padding-bottom: 6px;
       border-bottom: 1px solid var(--border); }
  h3 { font-size: 13px; margin: 16px 0 8px; color: var(--muted);
       text-transform: uppercase; letter-spacing: 0.05em; }
  .subtitle { color: var(--muted); margin-bottom: 20px; font-size: 13px; }
  .controls { display: flex; gap: 16px; align-items: center; margin: 20px 0;
              padding: 12px 16px; background: var(--panel); border: 1px solid var(--border);
              border-radius: 8px; flex-wrap: wrap; }
  .toggle-group { display: flex; gap: 0; border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }
  .toggle-group button {
    background: transparent; color: var(--muted); border: none; padding: 6px 14px;
    cursor: pointer; font: inherit; font-size: 12px; transition: all 0.1s;
  }
  .toggle-group button:hover { color: var(--text); }
  .toggle-group button.active { background: var(--accent); color: var(--bg); font-weight: 500; }
  .control-label { color: var(--muted); font-size: 12px; text-transform: uppercase;
                   letter-spacing: 0.05em; }
  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
           gap: 12px; margin: 16px 0; }
  .stat { background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
          padding: 12px 16px; }
  .stat-val { font-size: 22px; font-weight: 600; color: var(--accent); font-variant-numeric: tabular-nums; }
  .stat-label { font-size: 11px; color: var(--muted); text-transform: uppercase;
                letter-spacing: 0.05em; }

  table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
  th, td { padding: 7px 6px; text-align: left; border-bottom: 1px solid var(--border); }
  th {
    font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.05em;
    color: var(--muted); position: sticky; top: 0; background: var(--bg);
    cursor: pointer; user-select: none; white-space: nowrap;
  }
  th:hover { color: var(--text); }
  th.sorted-asc::after { content: " ▲"; color: var(--accent); }
  th.sorted-desc::after { content: " ▼"; color: var(--accent); }
  td.name { font-weight: 500; white-space: nowrap; }
  td.name a { color: var(--text); text-decoration: none; }
  td.name a:hover { color: var(--accent); }
  td.num { text-align: right; font-variant-numeric: tabular-nums; color: var(--text);
           padding-right: 10px; min-width: 56px; }
  td.muted-num { color: var(--muted); }
  .bar-cell { width: 80px; padding-right: 8px; }
  .bar { height: 14px; border-radius: 3px; min-width: 1px; }
  .bar-c { background: var(--c-commit); }
  .bar-a { background: var(--c-added); }
  .bar-d { background: var(--c-deleted); }
  .bar-p { background: var(--c-pr); }
  .bar-r { background: var(--c-review); }
  .bar-w { background: var(--c-weighted); }
  .bar-net-pos { background: var(--c-added); }
  .bar-net-neg { background: var(--c-deleted); }

  .sparkline { display: inline-block; vertical-align: middle; }

  details.row-detail { background: var(--panel-2); border: 1px solid var(--border);
                       border-radius: 6px; padding: 0; margin: 0 0 4px; }
  details.row-detail > summary { display: none; }
  details.row-detail[open] { padding: 12px 16px 14px; }
  .detail-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-top: 8px; }
  .detail-col h4 { font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em;
                   color: var(--muted); margin: 0 0 6px; }
  .detail-row { display: flex; justify-content: space-between; padding: 2px 0;
                font-size: 12px; gap: 10px; }
  .detail-row .label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
                       color: var(--text); font-family: ui-monospace, "SFMono-Regular", Menlo, monospace; font-size: 11px; }
  .detail-row .value { color: var(--muted); font-variant-numeric: tabular-nums;
                       white-space: nowrap; }
  .expand-btn { background: transparent; border: none; color: var(--muted);
                cursor: pointer; padding: 0 6px; font-size: 14px; }
  .expand-btn:hover { color: var(--accent); }

  .legend { display: flex; gap: 14px; font-size: 11px; color: var(--muted);
            margin-top: 6px; flex-wrap: wrap; }
  .legend span { display: inline-flex; align-items: center; gap: 4px; }
  .legend i { width: 10px; height: 10px; border-radius: 2px; display: inline-block; }

  .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
  .panel { background: var(--panel); border: 1px solid var(--border);
           border-radius: 8px; padding: 16px 20px; }
  .reasoning { background: var(--panel); border: 1px solid var(--border);
               border-radius: 8px; padding: 20px 24px; margin: 20px 0; }
  .reasoning ul { margin: 6px 0; padding-left: 22px; }
  .reasoning li { margin: 4px 0; }
  .reasoning code { background: rgba(255,255,255,0.06); padding: 1px 5px;
                    border-radius: 3px; font-size: 12px; font-family: ui-monospace, Menlo, monospace; }
  .callout { border-left: 3px solid var(--accent); padding: 4px 0 4px 14px;
             margin: 10px 0; color: var(--muted); font-size: 13px; }

  .top-cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
               gap: 14px; margin: 14px 0; }
  .top-card { background: var(--panel); border: 1px solid var(--border);
              border-radius: 8px; padding: 14px 16px; }
  .top-card-name { font-size: 15px; font-weight: 600; color: var(--accent); }
  .top-card-tag { font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.05em;
                  color: var(--muted); margin: 2px 0 6px; }
  .top-card-body { font-size: 12.5px; }

  .pkg-bar { display: flex; height: 14px; border-radius: 3px; overflow: hidden;
             background: rgba(255,255,255,0.04); }
  .pkg-bar > div { height: 100%; }
  .pkg-legend { font-size: 10.5px; color: var(--muted); margin-top: 4px; }

  footer { margin-top: 36px; padding-top: 16px; border-top: 1px solid var(--border);
           color: var(--muted); font-size: 12px; }
</style>
</head>
<body>

<h1>__PAGE_TITLE__</h1>
<div class="subtitle">
  __PAGE_SUBTITLE__ ·
  Generated <span id="genTime"></span> ·
  Repo started __REPO_FIRST_COMMIT__
  <span id="creditDeltaBadge" style="display:none; margin-left:8px; padding:2px 8px;
        border:1px solid var(--accent); border-radius:4px; font-size:11px;
        color:var(--accent);"></span>
</div>

<div class="controls">
  <span class="control-label">Time slice:</span>
  <div class="toggle-group" id="sliceToggle">
    <button data-slice="all" class="active">All-time</button>
    <button data-slice="d30">Last 30 days</button>
    <button data-slice="d7">Last 7 days</button>
  </div>
  <span class="control-label" style="margin-left: 16px;">Sort by:</span>
  <span style="font-size: 12px; color: var(--muted);">click any column header</span>
</div>

<div class="stats" id="statsBar"></div>

<h2>Combined Leaderboard</h2>
<div class="legend">
  <span><i style="background: var(--c-weighted)"></i> Weighted Lines (hotspot-multiplied)</span>
  <span><i style="background: var(--c-commit)"></i> Commits</span>
  <span><i style="background: var(--c-added)"></i> Net Lines</span>
  <span><i style="background: var(--c-pr)"></i> PRs</span>
  <span><i style="background: var(--c-review)"></i> Reviews</span>
</div>
<table id="leaderboard">
  <thead><tr>
    <th data-sort="name">Contributor</th>
    <th data-sort="weighted_lines" class="sorted-desc">W. Lines</th>
    <th></th>
    <th data-sort="commits">Commits</th>
    <th></th>
    <th data-sort="commits_per_pr">C/PR</th>
    <th data-sort="add">Lines+</th>
    <th data-sort="del">Lines−</th>
    <th data-sort="net">Net</th>
    <th></th>
    <th data-sort="prs">PRs</th>
    <th></th>
    <th data-sort="reviews">Reviews</th>
    <th></th>
    <th data-sort="iteration_rate" data-feature="iteration">Iteration</th>
    <th data-sort="files">Files</th>
    <th>Activity (per week)</th>
  </tr></thead>
  <tbody id="leaderboardBody"></tbody>
</table>

<h2>Per-Package Distribution (all-time)</h2>
<div class="legend" id="pkgLegend"></div>
<table id="pkgTable">
  <thead><tr>
    <th>Contributor</th>
    <th>Lines added by package</th>
    <th>Top file</th>
    <th>Top file lines</th>
  </tr></thead>
  <tbody id="pkgBody"></tbody>
</table>

<h2>Standouts</h2>
<div class="top-cards" id="standouts"></div>

<h2>Hotspot Files (top 15 by churn)</h2>
<div class="reasoning" style="padding: 16px 20px;">
<table>
  <thead><tr><th>File</th><th>Commits</th><th>Weight</th></tr></thead>
  <tbody id="hotspotsBody"></tbody>
</table>
</div>

<details style="margin-top: 24px;" data-feature="similarity">
  <summary style="cursor: pointer; font-size: 18px; padding: 8px 0; border-bottom: 1px solid var(--border); color: var(--text);">
    Possible re-implementation pairs
    <span id="similarityCount" style="color: var(--muted); font-size: 13px;"></span>
  </summary>
  <div class="reasoning" style="margin-top: 12px;">
    <p>Pairs where a <strong>closed</strong> PR by author A and a <strong>merged</strong>
    PR by author B (B ≠ A) touch a heavily overlapping file set within ±90 days.
    File-level Jaccard similarity ≥ 50%, both PRs touching ≥ 5 files. Sorted by overlap.</p>
    <div class="callout">
      <strong>Heuristic only — not a verdict.</strong> Two people both touching the
      auth module won't be re-implementations; two people fixing the same hot file
      won't be either. Use this list to surface cases worth a human look. The
      column does not affect any leaderboard number.
    </div>
    <table id="similarityTable">
      <thead><tr>
        <th>Closed PR</th><th>By</th>
        <th>Merged PR</th><th>By</th>
        <th>File overlap</th><th>Shared</th><th>Δ days</th>
      </tr></thead>
      <tbody id="similarityBody"></tbody>
    </table>
  </div>
</details>

<h2>Reasoning &amp; Methodology</h2>
<div class="reasoning">
  <h3>Time slicing</h3>
  <p>Every metric is computed per time slice (all-time / last 30 days / last 7 days).
  Switch slices via the toggle at the top — the table, stats, and standouts re-render in place.
  This separates "currently active" from "historically prolific".</p>

  <h3>Hotspot-weighted lines</h3>
  <p>A 100-line change to a frequently-edited core file is structurally more important than
  a 5,000-line change to a generated snapshot or a one-off doc. Each file is assigned a weight
  (0.5×–2.0×) based on its all-time commit count percentile; high-churn files (the ones the
  project actually edits often) weight more. The "W. Lines" column is the resulting
  hotspot-weighted sum.</p>

  <h3>Reviews as a 4th dimension</h3>
  <p>Reviews <em>given</em> are a separate signal from reviews <em>received</em>. This column counts
  reviews on other people's PRs, excluding self-comments and the bots/AI-reviewers configured
  in the bots list. Senior contributors often shift toward review and away from authoring;
  without this column, that work is invisible.</p>

  <h3>Commits and merge strategy (the C/PR column)</h3>
  <p>The "Commits" column counts post-merge git authors. Squash-merging
  collapses every PR into one commit (low ratio); rebase- or regular-merging
  preserves every commit from the PR (high ratio). On the same team for the
  same kind of work the C/PR column can range from ~0.1 (always squash) to
  ~10 (rebase or regular merge). High C/PR doesn't mean more work shipped,
  just a different merge style — read the two columns together. If your team
  squashes uniformly, "Commits" approximates "PRs" and C/PR sits near 1.0
  for everyone.</p>

  <h3>PRs are credit-weighted</h3>
  <p>Each merged PR is split across its commit authors by share of additions.
  A PR whose lines are 80% authored by A and 20% by B counts as 0.8 PRs for A
  and 0.2 for B. This means the column can show fractional values (e.g. 12.4)
  when contributors finish each other's branches or pair on a single PR. The
  goal: opening someone else's branch as your own PR doesn't inflate your number,
  and contributing meaningfully to someone else's PR shows up in yours.</p>

  <div data-feature="similarity">
  <h3>Optional: similarity-based credit delta</h3>
  <p>Off by default. When the build is run with <code>SIMILARITY_CREDIT_DELTA=1</code>,
  a fraction of each merged PR's credit is shifted to the author of any
  earlier closed PR it overlaps with (see "Possible re-implementation pairs"
  above). The shift equals the file-set Jaccard, capped at
  <code>SIMILARITY_DELTA_CAP</code> (default 0.4) per merged PR. Iteration
  attribution does <em>not</em> shift — fixes stay with whoever wrote the
  merged code.</p>
  <div class="callout">
    Auto-shift propagates the false-positive risk of similarity flagging into
    leaderboard numbers. Use sparingly. The badge in the header indicates
    whether deltas were applied for the current page.
  </div>
  </div>

  <div data-feature="iteration">
  <h3>Iteration rate</h3>
  <p>Counts merged PRs that <em>another author</em> later iterated on. A PR is
  iterated-on if either:</p>
  <ul>
    <li>A later merged PR titled <code>Revert</code>/<code>revert</code> points
    at it (an explicit revert), <strong>or</strong></li>
    <li>A later merged PR titled <code>fix:</code> by a different author,
    within 30 days, touches at least 2 of its files.</li>
  </ul>
  <p>The rate uses credit-weighted PR counts on both sides (a PR with shared
  authorship splits the iteration the same way as the credit). Rates are
  hidden for contributors with fewer than 5 credit-weighted PRs.</p>
  <p><strong>Revert chains are unwound.</strong> If the iterating PR was
  itself reverted (or its revert was reverted), the parity is followed back
  to whether the change actually landed in HEAD. Iterated only counts when
  the iterating change still exists in main. Caveat: this only catches
  explicit <code>Revert</code> markers — a hand-rewrite that happens to
  restore the original code doesn't leave a git-detectable trail.</p>
  <div class="callout">
    <strong>This is not a defect rate.</strong> It includes legitimate
    iteration: hot files get follow-up fixes regardless of whether the
    original PR had bugs. People working on <code>Dashboard.tsx</code> or
    <code>session-manager.ts</code> will see higher iteration rates than
    people working on rarely-edited code, even at identical skill. Read
    this column as "how often does the code area you ship in get touched
    again soon," not "how often is your work wrong."
  </div>
  </div>

  <h3>Per-author identity dedup via <code>.mailmap</code></h3>
  <p>Author identity is normalised through <code>git log --use-mailmap</code>, which reads a
  <code>.mailmap</code> file at the repo root. An optional identities file additionally maps
  display names to GitHub logins so commits and PRs/reviews join correctly.</p>

  <h3>What's still missing</h3>
  <div class="callout">
    These numbers measure what's <em>visible in git/GitHub</em>. Architecture decisions, design docs,
    incident response, and Slack/chat-driven work are still invisible.
  </div>
  <ul>
    <li><strong>Intra-PR review depth</strong> — a "LGTM" review and a 30-comment review both count as 1.</li>
    <li><strong>Code criticality beyond churn</strong> — frequency-of-change is a proxy. Files that
    rarely change but are load-bearing (e.g. a stable parser) are underweighted.</li>
  </ul>
</div>

<footer>
  <div>Refresh by re-running <code>refresh.sh</code> in the leaderboard repo.</div>
  <div>Data sources: <code>git log --use-mailmap</code>, <code>gh api graphql</code> (PRs + reviews).</div>
</footer>

<script>
const DATA = __DATA_JSON__;

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const fmt = (n) => n == null ? "—" : n.toLocaleString();

let currentSlice = "all";
let currentSort = { col: "weighted_lines", dir: "desc" };

document.getElementById("genTime").textContent = new Date(DATA.generated_at).toLocaleString();

// Hide markup for disabled features. Each gated element is tagged with
// data-feature="<name>"; we strip them before any render runs.
function hideDisabledFeatures() {
  const features = {
    iteration:  DATA.show_iteration,
    similarity: DATA.show_similarity,
  };
  for (const [name, enabled] of Object.entries(features)) {
    if (enabled) continue;
    document.querySelectorAll(`[data-feature="${name}"]`).forEach(el => el.remove());
  }
}
hideDisabledFeatures();

if (DATA.credit_delta_enabled) {
  const badge = document.getElementById("creditDeltaBadge");
  const n = (DATA.credit_delta_log || []).length;
  badge.textContent = `Credit-delta ON · ${n} pair${n===1?"":"s"} applied`;
  badge.style.display = "inline-block";
}

// Package color palette
const PKG_COLORS = ["#58a6ff","#3fb950","#d2a8ff","#ffa657","#f85149","#79c0ff",
                    "#ff7b72","#a371f7","#56d364","#e3b341","#bc8cff","#7ee787"];
const pkgColor = (() => {
  const map = {};
  let i = 0;
  return (pkg) => map[pkg] ?? (map[pkg] = PKG_COLORS[i++ % PKG_COLORS.length]);
})();

function renderStats() {
  const t = DATA.totals[currentSlice];
  $("#statsBar").innerHTML = `
    <div class="stat"><div class="stat-val">${t.people}</div><div class="stat-label">Active Contributors</div></div>
    <div class="stat"><div class="stat-val">${fmt(t.commits)}</div><div class="stat-label">Commits</div></div>
    <div class="stat"><div class="stat-val">${fmt(t.add)}</div><div class="stat-label">Lines Added</div></div>
    <div class="stat"><div class="stat-val">${fmt(t.prs)}</div><div class="stat-label">Merged PRs</div></div>
    <div class="stat"><div class="stat-val">${fmt(t.reviews)}</div><div class="stat-label">Human Reviews</div></div>
  `;
}

function getRows() {
  return Object.entries(DATA.contributors).map(([name, r]) => {
    const s = r.slices[currentSlice];
    return {
      name,
      ghLogin: r.ghLogin,
      weighted_lines: r.weighted_lines,
      commits: s.commits,
      commits_per_pr: s.commits_per_pr,
      add: s.add,
      del: s.del,
      net: s.net,
      prs: s.prs,
      reviews: s.reviews,
      iteration_count: s.iteration_count ?? 0,
      iteration_rate:  s.iteration_rate,    // null when prs < threshold
      files: s.files,
      packages: r.packages,
      top_files: r.top_files,
      sparkline: r.sparkline,
    };
  }).filter(r => r.commits || r.add || r.prs || r.reviews);
}

function sortRows(rows) {
  const { col, dir } = currentSort;
  const mult = dir === "asc" ? 1 : -1;
  return rows.sort((a, b) => {
    let av = a[col], bv = b[col];
    if (av == null) av = -Infinity;
    if (bv == null) bv = -Infinity;
    if (typeof av === "string") return av.localeCompare(bv) * mult;
    return (av - bv) * mult;
  });
}

function sparklineSVG(values, w=120, h=22) {
  if (!values.length) return "";
  const max = Math.max(...values, 1);
  const stepX = w / Math.max(values.length - 1, 1);
  const points = values.map((v, i) => `${(i * stepX).toFixed(1)},${(h - (v / max) * (h - 2) - 1).toFixed(1)}`).join(" ");
  const bars = values.map((v, i) => {
    const x = i * stepX - 1;
    const bh = (v / max) * (h - 2);
    return `<rect x="${x.toFixed(1)}" y="${(h - bh).toFixed(1)}" width="2" height="${bh.toFixed(1)}" fill="var(--c-commit)" opacity="0.4"/>`;
  }).join("");
  return `<svg class="sparkline" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}">
    ${bars}
    <polyline points="${points}" fill="none" stroke="var(--c-commit)" stroke-width="1.2"/>
  </svg>`;
}

function bar(val, max, cls) {
  if (!max || !val) return "";
  return `<div class="bar ${cls}" style="width: ${Math.max(2, val/max*100).toFixed(1)}%"></div>`;
}

function escape(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
}

function detailHTML(r) {
  // Top files
  const topFiles = (r.top_files || []).map(([p, n]) =>
    `<div class="detail-row"><span class="label" title="${escape(p)}">${escape(p)}</span><span class="value">${fmt(n)}</span></div>`
  ).join("") || "<div class='detail-row'><span class='label'>(no file data)</span></div>";

  // Packages stacked bar
  const pkgs = Object.entries(r.packages || {}).sort((a,b) => b[1] - a[1]);
  const totalPkg = pkgs.reduce((s, [_, v]) => s + v, 0) || 1;
  const pkgBar = pkgs.map(([pkg, v]) =>
    `<div title="${escape(pkg)}: ${fmt(v)} lines" style="width: ${(v/totalPkg*100).toFixed(2)}%; background: ${pkgColor(pkg)};"></div>`
  ).join("");
  const pkgLegend = pkgs.slice(0, 6).map(([pkg, v]) =>
    `<span><i style="background: ${pkgColor(pkg)}; width:8px; height:8px; border-radius:2px; display:inline-block;"></i> ${escape(pkg)} (${fmt(v)})</span>`
  ).join(" ");

  return `
    <div class="detail-grid">
      <div class="detail-col">
        <h4>Top Files Edited</h4>
        ${topFiles}
      </div>
      <div class="detail-col">
        <h4>Lines by Package</h4>
        <div class="pkg-bar">${pkgBar}</div>
        <div class="pkg-legend">${pkgLegend}</div>
      </div>
    </div>
  `;
}

function renderTable() {
  const rows = sortRows(getRows());
  const maxW = Math.max(...rows.map(r => r.weighted_lines), 1);
  const maxC = Math.max(...rows.map(r => r.commits), 1);
  const maxA = Math.max(...rows.map(r => Math.abs(r.net)), 1);
  const maxP = Math.max(...rows.map(r => r.prs), 1);
  const maxR = Math.max(...rows.map(r => r.reviews), 1);

  const body = $("#leaderboardBody");
  body.innerHTML = "";
  rows.forEach(r => {
    const tr = document.createElement("tr");
    const ghLink = r.ghLogin && r.ghLogin !== r.name
      ? `<a href="https://github.com/${escape(r.ghLogin)}" target="_blank" rel="noopener">${escape(r.name)}</a>`
      : escape(r.name);
    const netClass = r.net >= 0 ? "bar-net-pos" : "bar-net-neg";
    const iterCell = (r.iteration_rate == null)
      ? `<span class="muted-num">—</span>`
      : `${r.iteration_rate}%<span class="muted-num"> ${r.iteration_count}/${r.prs}</span>`;
    const cprCell = (r.commits_per_pr == null)
      ? `<span class="muted-num">—</span>`
      : r.commits_per_pr.toFixed(1);
    const iterTd = DATA.show_iteration
      ? `<td class="num">${iterCell}</td>`
      : "";
    tr.innerHTML = `
      <td class="name">${ghLink} <button class="expand-btn" data-name="${escape(r.name)}">▸</button></td>
      <td class="num">${fmt(r.weighted_lines)}</td>
      <td class="bar-cell">${bar(r.weighted_lines, maxW, "bar-w")}</td>
      <td class="num">${r.commits}</td>
      <td class="bar-cell">${bar(r.commits, maxC, "bar-c")}</td>
      <td class="num muted-num">${cprCell}</td>
      <td class="num muted-num">${fmt(r.add)}</td>
      <td class="num muted-num">${fmt(r.del)}</td>
      <td class="num">${r.net >= 0 ? "+" : ""}${fmt(r.net)}</td>
      <td class="bar-cell">${bar(Math.abs(r.net), maxA, netClass)}</td>
      <td class="num">${r.prs}</td>
      <td class="bar-cell">${bar(r.prs, maxP, "bar-p")}</td>
      <td class="num">${r.reviews}</td>
      <td class="bar-cell">${bar(r.reviews, maxR, "bar-r")}</td>
      ${iterTd}
      <td class="num muted-num">${r.files}</td>
      <td>${sparklineSVG(r.sparkline)}</td>
    `;
    body.appendChild(tr);

    // Detail row (collapsible)
    const detail = document.createElement("tr");
    detail.style.display = "none";
    const colspan = DATA.show_iteration ? 17 : 16;
    detail.innerHTML = `<td colspan="${colspan}" style="padding: 0;"><div style="padding: 12px 32px;">${detailHTML(r)}</div></td>`;
    body.appendChild(detail);
  });

  // Wire up expand buttons
  body.querySelectorAll(".expand-btn").forEach(btn => {
    btn.addEventListener("click", (e) => {
      const tr = btn.closest("tr");
      const detail = tr.nextElementSibling;
      const open = detail.style.display !== "none";
      detail.style.display = open ? "none" : "table-row";
      btn.textContent = open ? "▸" : "▾";
    });
  });
}

function renderPackageTable() {
  // Show all-time package distribution regardless of current slice.
  const allPkgs = new Set();
  Object.values(DATA.contributors).forEach(r =>
    Object.keys(r.packages || {}).forEach(p => allPkgs.add(p))
  );

  // Color legend for top packages
  const pkgTotals = {};
  Object.values(DATA.contributors).forEach(r => {
    Object.entries(r.packages || {}).forEach(([p, v]) => pkgTotals[p] = (pkgTotals[p] || 0) + v);
  });
  const topPkgs = Object.entries(pkgTotals).sort((a,b) => b[1] - a[1]).slice(0, 12);
  $("#pkgLegend").innerHTML = topPkgs.map(([p, _]) =>
    `<span><i style="background: ${pkgColor(p)}"></i> ${escape(p)}</span>`
  ).join("");

  const rows = Object.entries(DATA.contributors)
    .map(([name, r]) => ({ name, ...r }))
    .filter(r => Object.keys(r.packages || {}).length)
    .sort((a, b) => b.weighted_lines - a.weighted_lines);

  const body = $("#pkgBody");
  body.innerHTML = "";
  rows.forEach(r => {
    const pkgs = Object.entries(r.packages).sort((a,b) => b[1] - a[1]);
    const total = pkgs.reduce((s, [_, v]) => s + v, 0) || 1;
    const bar = pkgs.map(([p, v]) =>
      `<div title="${escape(p)}: ${fmt(v)}" style="width: ${(v/total*100).toFixed(2)}%; background: ${pkgColor(p)};"></div>`
    ).join("");
    const topFile = (r.top_files || [])[0] || ["—", 0];
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class="name">${escape(r.name)}</td>
      <td><div class="pkg-bar" style="width: 360px;">${bar}</div></td>
      <td class="num muted-num" style="text-align: left; font-family: ui-monospace, Menlo, monospace; font-size: 11px;">${escape(topFile[0])}</td>
      <td class="num">${fmt(topFile[1])}</td>
    `;
    body.appendChild(tr);
  });
}

// Registry of available standout cards. Each entry picks one row from the
// current slice's data and renders one card. `requires` gates a card on a
// feature flag.
const STANDOUT_DEFS = {
  top_impact: {
    label: "Top Impact",
    pick:  rows => rows.slice().sort((a,b) => (b.weighted_lines||0) - (a.weighted_lines||0))[0],
    body:  r => `${fmt(r.weighted_lines)} hotspot-weighted lines across ${r.prs} PRs. Owns ${r.top_files && r.top_files[0] ? r.top_files[0][0] : 'core areas'}.`,
  },
  top_reviewer: {
    label: "Top Reviewer",
    pick:  rows => rows.slice().sort((a,b) => (b.reviews||0) - (a.reviews||0))[0],
    body:  r => `${r.reviews} reviews on others' PRs — quality gate for the team.`,
  },
  highest_output: {
    label: "Highest Output",
    pick:  rows => rows.slice().sort((a,b) => (b.commits||0) - (a.commits||0))[0],
    body:  r => `${r.commits} commits, ${r.prs} PRs shipped (note: merge-strategy biased — see C/PR).`,
  },
  most_prs: {
    label: "Most PRs Shipped",
    pick:  rows => rows.slice().sort((a,b) => (b.prs||0) - (a.prs||0))[0],
    body:  r => `${r.prs} merged PRs (credit-weighted).`,
  },
  most_prs_opened: {
    label: "Most PRs Opened",
    pick:  rows => rows.slice().sort((a,b) => (b.prs_raw||0) - (a.prs_raw||0))[0],
    body:  r => `${r.prs_raw} merged PRs by raw count.`,
  },
  highest_volume: {
    label: "Highest Volume",
    pick:  rows => rows.slice().sort((a,b) => (b.add||0) - (a.add||0))[0],
    body:  r => `${fmt(r.add)} lines added.`,
  },
  biggest_cleanup: {
    label: "Biggest Cleanup",
    pick:  rows => rows.slice().sort((a,b) => (b.del||0) - (a.del||0))[0],
    body:  r => `${fmt(r.del)} lines removed — the unsung refactor work.`,
  },
  broadest_reach: {
    label: "Broadest Reach",
    pick:  rows => rows.slice().sort((a,b) => (b.files||0) - (a.files||0))[0],
    body:  r => `Touched ${r.files} distinct files in this slice.`,
  },
  most_iterated: {
    label: "Most Iterated On",
    pick:  rows => rows.slice().sort((a,b) => (b.iteration_count||0) - (a.iteration_count||0))[0],
    body:  r => `${r.iteration_count} of their PRs were later touched by another author's fix.`,
    requires: "iteration",
  },
  cleanest_shipper: {
    label: "Cleanest Shipper",
    pick:  rows => rows.slice()
                       .filter(r => r.iteration_rate != null && r.prs >= 5)
                       .sort((a,b) => (a.iteration_rate||0) - (b.iteration_rate||0))[0],
    body:  r => `${r.iteration_rate}% iteration rate — lowest among contributors with ≥5 PRs.`,
    requires: "iteration",
  },
};

function renderStandouts() {
  const rows = getRows();
  const config = DATA.standouts_config && DATA.standouts_config.length
    ? DATA.standouts_config
    : ["top_impact","top_reviewer","highest_output","most_prs"];
  const seen  = new Set();
  const cards = [];
  for (const code of config) {
    const def = STANDOUT_DEFS[code];
    if (!def) continue;
    if (def.requires === "iteration" && !DATA.show_iteration) continue;
    const winner = def.pick(rows);
    if (!winner || seen.has(winner.name)) continue;
    seen.add(winner.name);
    cards.push({ name: winner.name, tag: def.label, body: def.body(winner) });
  }
  $("#standouts").innerHTML = cards.map(c => `
    <div class="top-card">
      <div class="top-card-name">${escape(c.name)}</div>
      <div class="top-card-tag">${escape(c.tag)}</div>
      <div class="top-card-body">${escape(c.body)}</div>
    </div>
  `).join("");
}

function renderHotspots() {
  $("#hotspotsBody").innerHTML = DATA.top_hotspots.map(h =>
    `<tr><td class="name" style="font-family: ui-monospace, Menlo, monospace; font-size: 11.5px;">${escape(h.path)}</td>
         <td class="num">${h.commits}</td>
         <td class="num muted-num">${h.weight.toFixed(2)}×</td></tr>`
  ).join("");
}

function renderSimilarity() {
  if (!DATA.show_similarity) return;       // panel was already removed by hideDisabledFeatures
  const pairs = DATA.similarity_pairs || [];
  const base  = DATA.repo_pr_url_base || "";
  const linkPR = (n) => base ? `<a href="${base}${n}" target="_blank" rel="noopener">#${n}</a>` : `#${n}`;
  const linkAuthor = (u) => `<a href="https://github.com/${escape(u)}" target="_blank" rel="noopener">${escape(u)}</a>`;
  $("#similarityCount").textContent = pairs.length
    ? `(${pairs.length} flagged)`
    : "(none flagged)";
  if (!pairs.length) {
    $("#similarityBody").innerHTML = `<tr><td colspan="7" class="muted-num" style="text-align:center;">No pairs above the threshold.</td></tr>`;
    return;
  }
  $("#similarityBody").innerHTML = pairs.map(p => `
    <tr>
      <td class="name">${linkPR(p.closed)} <span class="muted-num">${escape(p.closed_title || "")}</span></td>
      <td class="muted-num">${linkAuthor(p.closed_author)}</td>
      <td class="name">${linkPR(p.merged)} <span class="muted-num">${escape(p.merged_title || "")}</span></td>
      <td class="muted-num">${linkAuthor(p.merged_author)}</td>
      <td class="num">${Math.round(p.file_overlap * 100)}%</td>
      <td class="num muted-num">${p.shared_files}</td>
      <td class="num muted-num">${p.delta_days >= 0 ? "+" : ""}${p.delta_days}</td>
    </tr>
  `).join("");
}

// Wire up controls
$$("#sliceToggle button").forEach(b => {
  b.addEventListener("click", () => {
    $$("#sliceToggle button").forEach(x => x.classList.remove("active"));
    b.classList.add("active");
    currentSlice = b.dataset.slice;
    renderStats(); renderTable(); renderStandouts();
  });
});

$$("#leaderboard th[data-sort]").forEach(th => {
  th.addEventListener("click", () => {
    const col = th.dataset.sort;
    if (currentSort.col === col) {
      currentSort.dir = currentSort.dir === "asc" ? "desc" : "asc";
    } else {
      currentSort = { col, dir: col === "name" ? "asc" : "desc" };
    }
    $$("#leaderboard th").forEach(t => t.classList.remove("sorted-asc", "sorted-desc"));
    th.classList.add("sorted-" + currentSort.dir);
    renderTable();
  });
});

renderStats();
renderTable();
renderPackageTable();
renderStandouts();
renderHotspots();
renderSimilarity();
</script>
</body>
</html>
"""

OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
out_html = (HTML
            .replace("__PAGE_TITLE__", html.escape(PAGE_TITLE))
            .replace("__PAGE_SUBTITLE__", html.escape(PAGE_SUBTITLE))
            .replace("__REPO_FIRST_COMMIT__", html.escape(data.get("repo_first_commit", "—")))
            .replace("__DATA_JSON__", data_json))
OUT_FILE.write_text(out_html)
print(f"Wrote {OUT_FILE} ({OUT_FILE.stat().st_size:,} bytes)")
