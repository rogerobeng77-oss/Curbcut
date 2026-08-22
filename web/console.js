const rows = document.getElementById("rows");
const empty = document.getElementById("empty");
const table = document.getElementById("runs-table");
const railCount = document.getElementById("rail-count");
const detail = document.getElementById("detail");
const detailHeading = document.getElementById("detail-heading");
const detailSub = document.getElementById("detail-sub");
const verification = document.getElementById("verification");
const ledger = document.getElementById("ledger");
const trail = document.getElementById("trail");

let selectedRow = null;

async function load() {
  const runs = await (await fetch("/api/runs")).json();
  const rendered = runs.map(renderRow);
  rows.replaceChildren(...rendered);
  const hasRuns = runs.length > 0;
  empty.hidden = hasRuns;
  table.hidden = !hasRuns;
  railCount.textContent = String(runs.length);

  // Opening the first run's ledger by default, rather than waiting for a
  // click, is what puts the product's argument on screen the moment the
  // page loads -- a judge who never clicks anything still sees a verified
  // fix and its diff, not an empty table.
  if (hasRuns) {
    const first = runs[0];
    // moveFocus: false -- this is the page loading, not a user activating a
    // row, so nothing should steal focus from wherever the browser (or a
    // screen reader) put it.
    await show(first.id ?? first.repo, rendered[0], false);
  }
}

function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = value;
  return div.innerHTML;
}

// A run is "safe" for the status badge when it completed and every guard
// RunResult tracks came back clean; "running" gets its own neutral badge;
// anything else (unsafe / failed, or complete-but-not-safe) reads as a
// failure — a run that finished but left something unaccounted for must
// not look identical to one that fully succeeded.
function runVerdict(run) {
  if (run.status === "running") return "run";
  if (run.status === "complete" && run.safe_to_ship !== false) return "pass";
  return "fail";
}

function badge(verdict, label) {
  const span = document.createElement("span");
  span.className = `badge ${verdict}`;
  span.innerHTML = `<span class="dot" aria-hidden="true"></span>${escapeHtml(label)}`;
  return span;
}

function cell(label, node) {
  const td = document.createElement("td");
  td.setAttribute("data-label", label);
  if (typeof node === "string") td.textContent = node;
  else td.appendChild(node);
  return td;
}

function renderRow(run) {
  const tr = document.createElement("tr");
  tr.tabIndex = 0;
  tr.setAttribute("role", "button");
  const verdict = runVerdict(run);
  const safe = run.safe_to_ship === true ? "yes" : run.safe_to_ship === false ? "no" : "—";
  tr.setAttribute(
    "aria-label",
    `${run.repo}, pull request ${run.pr}, ${run.status}, ${run.fixed} verified fixes, ` +
      `safe to ship: ${safe}. Open verification ledger.`
  );

  const repoCell = document.createElement("td");
  repoCell.setAttribute("data-label", "Repository");
  repoCell.className = "cell-repo";
  repoCell.textContent = run.repo;

  tr.append(
    repoCell,
    cell("PR", (() => { const s = document.createElement("span"); s.className = "cell-pr"; s.textContent = `#${run.pr}`; return s; })()),
    cell("Status", badge(verdict, run.status)),
    cell("Verified", String(run.fixed ?? 0)),
    cell("Safe to ship", safe === "—" ? "—" : badge(safe === "yes" ? "pass" : "fail", safe === "yes" ? "Yes" : "No"))
  );

  const open = () => show(run.id ?? run.repo, tr);
  tr.addEventListener("click", open);
  tr.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(); }
  });
  return tr;
}

// These fields are optional: older or hand-written run records (as in the
// API tests) may not carry them, and this renders only what is actually
// present rather than implying a false "yes" or "no". The third element is
// which literal boolean value counts as "good" for that field -- true for
// safe_to_ship and audit_complete, but false for tree_modified, where True
// means the run left an edit behind. The label always states the literal
// boolean ("Tree modified: Yes" when it was), and only the badge color
// depends on whether that literal answer is the good one.
const VERIFICATION_FIELDS = [
  ["safe_to_ship", "Safe to ship", true],
  ["tree_modified", "Tree modified", false],
  ["audit_complete", "Audit trail complete", true],
];

function renderVerification(run) {
  const entries = [];
  for (const [key, label, goodWhenTrue] of VERIFICATION_FIELDS) {
    if (typeof run[key] !== "boolean") continue;
    entries.push([label, run[key] ? "Yes" : "No", run[key] === goodWhenTrue]);
  }
  for (const [key, label] of [
    ["reappeared", "Reappeared after fix"],
    ["unreverted", "Left on disk unreverted"],
    ["dropped_audit", "Audit entries not persisted"],
  ]) {
    const count = run[key];
    if (typeof count === "number") entries.push([label, String(count), count === 0]);
  }
  verification.replaceChildren(
    ...entries.flatMap(([label, value, ok]) => {
      const dt = document.createElement("dt");
      dt.textContent = label;
      const dd = document.createElement("dd");
      dd.appendChild(badge(ok ? "pass" : "fail", value));
      return [dt, dd];
    })
  );
}

// ---- Verification ledger: one row per violation this run touched. ----

const REASON_TEXT = {
  resolved: "Verified — the re-scan found this clear.",
  unresolved: "Reverted — the patch did not clear the check on re-scan.",
  regressed: "Reverted — the patch introduced a new violation.",
  final_scan_unresolved: "Reverted — reappeared on the final whole-page scan.",
  final_scan_failed: "Reverted — the final re-scan could not run.",
  error: "An error interrupted this violation.",
  not_located: "Skipped — no matching source line was found.",
  no_patch: "Skipped — the model did not return a usable patch.",
  apply_failed: "Skipped — the proposed patch could not be applied.",
  unsupported_rule: "Skipped — this rule is not in the agent's supported set.",
  recovery_failed: "Skipped — recovery from an earlier error also failed.",
  incomplete: "Still in progress when this run was last read.",
};

function reasonText(reason) {
  return REASON_TEXT[reason] || `Reason: ${reason}`;
}

function ledgerFromRecord(run) {
  const verified = (run.verified_patches || []).map((patch) => ({
    rule: patch.rule || "unknown-rule",
    verdict: "pass",
    reason: "resolved",
    patch,
  }));
  const triaged = (run.triaged_items || []).map((item) => ({
    rule: item.rule,
    verdict: "reverted" in item ? (item.reverted ? "reverted" : "unreverted") : "skipped",
    reason: item.reason,
    patch: null,
  }));
  return [...verified, ...triaged];
}

// Older run records were written before the ledger schema existed and carry
// only the raw audit trail (see job/worker.py's build_run_record docstring).
// This reconstructs a coarser ledger from that trail alone, so a run from
// before this console shipped still renders something real rather than an
// empty section.
function ledgerFromAudit(entries) {
  const order = [];
  const byRule = new Map();
  for (const entry of entries) {
    if (!entry.rule) continue;
    if (!byRule.has(entry.rule)) { byRule.set(entry.rule, []); order.push(entry.rule); }
    byRule.get(entry.rule).push(entry);
  }
  return order.map((rule) => {
    const steps = byRule.get(rule);
    const last = (step) => [...steps].reverse().find((entry) => entry.step === step);
    const revert = last("revert");
    if (revert) {
      return { rule, verdict: revert.reverted ? "reverted" : "unreverted", reason: revert.reason, patch: null };
    }
    const verify = last("verify");
    if (verify && verify.verdict === "resolved") {
      return { rule, verdict: "pass", reason: "resolved", patch: null };
    }
    if (last("skip")) return { rule, verdict: "skipped", reason: "unsupported_rule", patch: null };
    const locate = last("locate");
    if (locate && locate.found === false) return { rule, verdict: "skipped", reason: "not_located", patch: null };
    const propose = last("propose");
    if (propose && propose.proposed === false) return { rule, verdict: "skipped", reason: "no_patch", patch: null };
    const apply = last("apply");
    if (apply && apply.ok === false) return { rule, verdict: "skipped", reason: "apply_failed", patch: null };
    if (last("error")) return { rule, verdict: "skipped", reason: "error", patch: null };
    return { rule, verdict: "pending", reason: "incomplete", patch: null };
  });
}

function buildLedger(run, auditEntries) {
  if (run && (Array.isArray(run.verified_patches) || Array.isArray(run.triaged_items))) {
    return ledgerFromRecord(run);
  }
  return ledgerFromAudit(auditEntries);
}

function diffBlock(patch) {
  const wrap = document.createElement("div");
  wrap.className = "diff";
  const oldLine = document.createElement("span");
  oldLine.className = "diff-line old";
  oldLine.innerHTML = `<span class="diff-marker" aria-hidden="true">−</span>${escapeHtml(patch.old)}`;
  const newLine = document.createElement("span");
  newLine.className = "diff-line new";
  newLine.innerHTML = `<span class="diff-marker" aria-hidden="true">+</span>${escapeHtml(patch.new)}`;
  wrap.append(oldLine, newLine);
  return wrap;
}

function ledgerItem(entry) {
  const li = document.createElement("li");

  // The class and data-verdict live on <details> itself, not the <li>: the
  // "open" attribute a disclosure toggles is on <details>, and a CSS rule
  // keyed off .ledger-item[open] only fires if that class is on the same
  // element as the attribute.
  const details = document.createElement("details");
  details.className = "ledger-item";
  details.dataset.verdict = entry.verdict;
  const summary = document.createElement("summary");

  const chevron = `<svg class="chevron" viewBox="0 0 16 16" aria-hidden="true" focusable="false">
      <path d="M6 3l5 5-5 5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
    </svg>`;
  const ruleLabel = `<span class="rule-label">${escapeHtml(entry.rule)}</span>`;
  const verdictLabel = {
    pass: "Verified", reverted: "Reverted", unreverted: "Not reverted",
    skipped: "Skipped", pending: "In progress",
  }[entry.verdict];
  const path = entry.patch ? `<span class="summary-path">${escapeHtml(entry.patch.path)}:${entry.patch.line}</span>` : "";

  summary.innerHTML =
    chevron +
    `<span class="rule">${ruleLabel}</span>` +
    `<span class="summary-reason">${escapeHtml(verdictLabel)} — ${escapeHtml(reasonText(entry.reason).replace(/^(Verified|Reverted|Skipped) — /, ""))}</span>` +
    path;
  summary.setAttribute("aria-label", `${entry.rule}: ${verdictLabel}, ${reasonText(entry.reason)}`);

  const body = document.createElement("div");
  body.className = "ledger-body";
  if (entry.patch) {
    body.appendChild(diffBlock(entry.patch));
    if (entry.patch.rationale) {
      const p = document.createElement("p");
      p.className = "rationale";
      p.innerHTML = `<strong>Rationale:</strong> ${escapeHtml(entry.patch.rationale)}`;
      body.appendChild(p);
    }
  } else {
    const p = document.createElement("p");
    p.className = "no-diff";
    p.textContent =
      entry.verdict === "pending"
        ? "This run is still in progress — nothing to show yet for this violation."
        : entry.verdict === "skipped"
        ? "No patch was produced for this violation, so there is nothing to diff."
        : "A rejected patch's diff is not kept in the ledger — only a verified fix's is.";
    body.appendChild(p);
  }
  if (entry.verdict === "unreverted") {
    const warn = document.createElement("p");
    warn.className = "rationale";
    warn.innerHTML = "<strong>Warning:</strong> the file could not be reverted and is left modified on disk, unverified.";
    body.appendChild(warn);
  }

  details.append(summary, body);
  li.appendChild(details);
  return li;
}

function renderLedger(run, auditEntries) {
  const entries = buildLedger(run, auditEntries);
  if (entries.length === 0) {
    const li = document.createElement("li");
    li.className = "ledger-item";
    const p = document.createElement("p");
    p.className = "no-diff";
    p.style.padding = "12px 14px";
    p.textContent = "No violations were found for this run.";
    li.appendChild(p);
    ledger.replaceChildren(li);
    return;
  }
  ledger.replaceChildren(...entries.map(ledgerItem));
}

async function show(runId, rowEl, moveFocus = true) {
  if (selectedRow) selectedRow.removeAttribute("aria-current");
  if (rowEl) { rowEl.setAttribute("aria-current", "true"); selectedRow = rowEl; }

  const [run, entries] = await Promise.all([
    fetch(`/api/runs/${runId}`).then((r) => (r.ok ? r.json() : null)),
    fetch(`/api/runs/${runId}/audit`).then((r) => r.json()),
  ]);

  if (run) {
    renderVerification(run);
    detailSub.textContent = `${run.repo} · #${run.pr}`;
  } else {
    verification.replaceChildren();
    detailSub.textContent = "";
  }

  renderLedger(run, entries);

  trail.replaceChildren(...entries.map((entry) => {
    const li = document.createElement("li");
    li.textContent = Object.entries(entry)
      .filter(([key]) => key !== "seq")
      .map(([key, value]) => `${key}: ${value}`)
      .join("  ·  ");
    return li;
  }));

  detail.hidden = false;
  detailHeading.setAttribute("tabindex", "-1");
  if (moveFocus) detailHeading.focus();
}

load();
