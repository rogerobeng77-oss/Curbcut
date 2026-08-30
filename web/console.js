/* Curbcut console.
 *
 * One job: let a person decide whether to trust a run. Everything on screen
 * answers either "did this patch hold up?" or "why did this one not ship?".
 */

const listEl = document.getElementById("run-list");
const tallyEl = document.getElementById("tally");
const detailEl = document.getElementById("detail-body");
const rowTpl = document.getElementById("tpl-run-row");

const STAGES = ["locate", "propose", "apply", "verify"];

// Why a violation did not produce a shipped patch. Phrased as what happened,
// not as an apology, and never vague about which stage stopped it.
const REASONS = {
  unsupported_rule: "No patch attempted. This rule is outside the set Curbcut edits.",
  not_located:      "Not patched. The element could not be traced to a source line.",
  no_patch:         "Not patched. The model did not return a usable edit.",
  apply_failed:     "Not patched. The proposed edit would not apply to the file.",
  unresolved:       "Reverted. The re-scan still reported the violation.",
  regressed:        "Reverted. The patch introduced a different violation.",
  reverted:         "Reverted. The patch did not survive verification.",
  unreverted:       "Left on disk. The revert failed, so this run shipped nothing.",
  resolved:         "Verified. The re-scan found this clear.",
};

/* The scanned markup, rendered. `sandbox` with no allow-* tokens is doing real
 * work: this is third-party HTML from whatever repository was scanned, so it
 * gets no scripts, no forms, no same-origin access, and no navigation. The
 * srcdoc carries a minimal reset only, deliberately not the page's stylesheet
 * — we never captured it, and faking one would misrepresent the fix. */
/* A patch to a stylesheet is a CSS rule, not an element. Putting
 * `.heading-sec__sub { color: #555 }` in an iframe renders the literal text
 * and tells a reader nothing, so those rows show the diff alone. */
function isMarkup(source) {
  return String(source || "").trim().startsWith("<");
}

function frame(html) {
  const doc = `<!doctype html><meta charset="utf-8">
    <style>body{margin:0;padding:10px;font:14px/1.5 system-ui,sans-serif;
      background:#fff;color:#16191d}img{max-width:100%%}</style>${html}`;
  return `<iframe class="mini" sandbox loading="lazy" title="Rendered markup"
            srcdoc="${esc(doc)}"></iframe>`;
}

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function json(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${res.status} ${url}`);
  return res.json();
}

/* ---- ledger ------------------------------------------------------------ */

/* The audit trail is a flat, seq-ordered log, and a rule can appear several
 * times in one run: the demo fixture trips link-name twice and image-alt
 * twice. Grouping by rule would collapse those into a single row, which is
 * how an earlier version of this file managed to label a violation "Verified"
 * while drawing the failed track of a different violation with the same rule
 * id. So walk the log in order instead: every `locate` opens a new violation,
 * and the stages that follow attach to the most recent open one for that rule.
 */
function violationsFromAudit(entries) {
  const out = [];
  const open = new Map(); // rule -> the violation still collecting stages

  for (const entry of [...entries].sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0))) {
    if (!entry.rule) continue;
    if (entry.step === "locate" || !open.has(entry.rule)) {
      const violation = { rule: entry.rule, stages: {} };
      open.set(entry.rule, violation);
      out.push(violation);
    }
    const violation = open.get(entry.rule);
    violation.stages[entry.step] = entry;
    // verify is terminal; the next entry for this rule starts a new violation
    if (entry.step === "verify") open.delete(entry.rule);
  }
  return out;
}

function verdictOf(stages) {
  const { locate, propose, apply, verify } = stages;
  if (verify) {
    return verify.verdict === "resolved"
      ? { verdict: "pass", reason: "resolved" }
      : { verdict: "fail", reason: verify.verdict || "reverted" };
  }
  if (apply && apply.ok === false) return { verdict: "fail", reason: "apply_failed" };
  if (propose && propose.proposed === false) return { verdict: "fail", reason: "no_patch" };
  if (locate && locate.found === false) return { verdict: "fail", reason: "not_located" };
  return { verdict: "fail", reason: "unsupported_rule" };
}

function trackOf(stages) {
  return STAGES.map((step) => {
    const entry = stages[step];
    if (!entry) return { step, state: "none" };
    const failed =
      entry.found === false || entry.proposed === false ||
      entry.ok === false || (entry.verdict && entry.verdict !== "resolved");
    return { step, state: failed ? "failed" : "done" };
  });
}

/* What the four stages must have been, given how this violation ended.
 *
 * The earlier version paired the nth verified patch with the nth audit entry
 * for the same rule. That works only while every violation of a rule ends the
 * same way. On a run with 33 violations, mostly color-contrast, some verified
 * and some triaged, the ordinals slide apart and rows start showing another
 * violation's track: "Verified" beside a red segment, or beside no segments at
 * all. The reason is already on the row, and it determines the stages exactly,
 * so derive them instead of matching positions.
 */
function trackFor(verdict, reason) {
  const done = (n) => STAGES.map((step, i) =>
    ({ step, state: i < n ? "done" : "none" }));
  const stopAt = (n) => done(n).map((seg, i) =>
    (i === n ? { ...seg, state: "failed" } : seg));

  if (verdict === "pass") return done(4);
  switch (reason) {
    case "not_located":      return stopAt(0);
    case "unsupported_rule": return stopAt(0);
    case "no_patch":         return stopAt(1);
    case "apply_failed":     return stopAt(2);
    default:                 return stopAt(3);  // reached verify and failed it
  }
}

/* Prefer the run record: it carries the diff and the model's reasoning, which
 * the audit trail never stored. Fall back to the trail for runs written before
 * that schema existed, which render every row minus the diff. */
function ledgerOf(run, entries) {
  const verified = run.verified_patches || [];
  const triaged = run.triaged_items || [];

  if (verified.length || triaged.length) {
    return [
      ...verified.map((p) => ({
        rule: p.rule, verdict: "pass", reason: "resolved", patch: p,
      })),
      ...triaged.map((t) => ({
        rule: t.rule, verdict: "fail", reason: t.reason || "reverted", patch: null,
      })),
    ];
  }

  return violationsFromAudit(entries).map((v) => ({
    rule: v.rule, patch: null, ...verdictOf(v.stages),
  }));
}

/* ---- render ------------------------------------------------------------ */

function renderRunList(runs, selectedId, onPick) {
  listEl.textContent = "";
  tallyEl.textContent = runs.length === 1 ? "1 run" : `${runs.length} runs`;

  runs.forEach((run) => {
    const node = rowTpl.content.cloneNode(true);
    const li = node.querySelector(".run-row");
    const btn = node.querySelector(".run-btn");
    const shipped = run.safe_to_ship !== false;

    node.querySelector(".run-repo").textContent = run.repo;
    node.querySelector(".run-pr").textContent = `#${run.pr}`;

    const verdict = node.querySelector(".run-verdict");
    verdict.textContent = shipped ? "shipped" : "held";
    verdict.dataset.ok = String(shipped);

    node.querySelector(".run-counts").textContent =
      `${run.fixed ?? 0} verified · ${run.triaged ?? 0} triaged`;

    if (run.id === selectedId) li.setAttribute("aria-current", "true");
    btn.addEventListener("click", () => onPick(run.id));
    listEl.append(node);
  });
}

function gateHtml(run) {
  const shipped = run.safe_to_ship !== false;
  const facts = [
    ["Working tree is only verified patches", run.tree_modified === false, run.tree_modified ? "modified" : "clean"],
    ["Audit trail written in full", run.audit_complete !== false, run.audit_complete === false ? "incomplete" : "complete"],
    ["Violations that came back after a fix", run.reappeared === 0, String(run.reappeared ?? 0)],
    ["Rejected patches left on disk", run.unreverted === 0, String(run.unreverted ?? 0)],
    ["Audit entries that failed to persist", run.dropped_audit === 0, String(run.dropped_audit ?? 0)],
  ];
  return `
    <section class="gate" aria-labelledby="gate-h">
      <div class="gate-head">
        <span id="gate-h">Ship gate</span>
        <span class="gate-verdict" data-ok="${shipped}">
          ${shipped ? "Passed — the pull request opened" : "Held — nothing was committed"}
        </span>
      </div>
      <dl>
        ${facts.map(([label, ok, value]) => `
          <dt>${esc(label)}</dt>
          <dd data-state="${ok ? "ok" : "bad"}">${esc(value)}</dd>`).join("")}
      </dl>
    </section>`;
}

function rowHtml(item, track, index) {
  const passed = item.verdict === "pass";
  const p = item.patch;
  const note = REASONS[item.reason] || (passed ? REASONS.resolved : REASONS.reverted);

  return `
  <li class="row" data-verdict="${passed ? "pass" : "fail"}" data-open="false">
    <button type="button" class="row-btn" aria-expanded="false" aria-controls="p-${index}">
      <svg class="chev" viewBox="0 0 16 16" aria-hidden="true" fill="currentColor">
        <path d="M6 3.5 10.5 8 6 12.5V3.5Z"/>
      </svg>
      <code class="rule-id">${esc(item.rule)}</code>
      <span class="row-note">${esc(note)}</span>
      <span class="track" role="img" aria-label="${
        track.map((t) => `${t.step}: ${t.state === "done" ? "passed" : t.state === "failed" ? "stopped here" : "not reached"}`).join(", ")
      }">${track.map((t) => `<span class="seg" data-state="${t.state}"></span>`).join("")}</span>
    </button>
    <div class="panel" id="p-${index}" hidden>
      ${p ? `<p class="where">${esc(p.path)}:${esc(p.line)}</p>
      ${isMarkup(p.old) ? `<div class="render">
        <figure><figcaption>Before</figcaption>${frame(p.old)}</figure>
        <figure><figcaption>After</figcaption>${frame(p.new)}</figure>
      </div>
      <p class="render-note">Rendered without the page's own stylesheet, so this
      shows the element as markup, not as it appears in context.</p>` : ""}
      <div class="diff">
        <pre class="del">- ${esc(p.old)}</pre>
        <pre class="add">+ ${esc(p.new)}</pre>
      </div>
      <p class="why"><b>Why:</b> ${esc(p.rationale)}</p>` :
      `<p class="why">${esc(note)}${
        passed
          // A verified row always shipped a change. Runs written before the
          // record carried patch detail simply cannot show it, and saying
          // "no source change was committed" here would be a lie about a
          // change that is sitting in the pull request.
          ? " The diff is in the pull request; this run predates the console storing it."
          : " No source change was committed for this violation."
      }</p>`}
      <ul class="stages">
        ${track.map((t) => `<li data-state="${t.state}">${t.step}</li>`).join("")}
      </ul>
    </div>
  </li>`;
}

function renderDetail(run, entries) {
  const items = ledgerOf(run, entries);
  const scan = entries.find((e) => e.step === "scan");
  const final = entries.find((e) => e.step === "final_scan");

  detailEl.innerHTML = `
    <h1 class="run-title">${esc(run.repo)}</h1>
    <p class="run-sub">
      Pull request #${esc(run.pr)} ·
      ${scan ? `${esc(scan.found)} violations found` : "scan pending"}${
        final ? `, ${esc(final.found)} left after patching` : ""}
      ${run.pr_url ? ` · <a href="${esc(run.pr_url)}">View the pull request it opened</a>` : ""}
    </p>
    ${gateHtml(run)}
    <div class="ledger-head">
      <h2>Verification ledger</h2>
      <p>One row per violation. Open a row for the change and the reasoning.</p>
    </div>
    <div class="filters" role="group" aria-label="Filter the ledger">
      <button type="button" class="chip" data-filter="all" aria-pressed="true">
        All <span class="n">${items.length}</span></button>
      <button type="button" class="chip" data-filter="pass" aria-pressed="false">
        Verified <span class="n">${items.filter((i) => i.verdict === "pass").length}</span></button>
      <button type="button" class="chip" data-filter="fail" aria-pressed="false">
        Triaged <span class="n">${items.filter((i) => i.verdict !== "pass").length}</span></button>
    </div>
    <p class="sr-live" id="filter-status" role="status"></p>
    <ol class="ledger">
      ${items.map((item, i) => rowHtml(item, trackFor(item.verdict, item.reason), i)).join("")}
    </ol>`;

  const rows = [...detailEl.querySelectorAll(".row")];

  detailEl.querySelectorAll(".chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      const want = chip.dataset.filter;
      detailEl.querySelectorAll(".chip").forEach((c) =>
        c.setAttribute("aria-pressed", String(c === chip)));
      let shown = 0;
      rows.forEach((row) => {
        const match = want === "all" || row.dataset.verdict === want;
        row.hidden = !match;
        if (match) shown += 1;
      });
      // Announce it: a filter that silently changes the list is invisible to
      // anyone not watching the rows move.
      document.getElementById("filter-status").textContent =
        `Showing ${shown} of ${rows.length} violations.`;
    });
  });

  /* j and k move between rows the way they do in a code review; Enter and
   * Space already open a row because each one is a real button. */
  detailEl.addEventListener("keydown", (event) => {
    if (event.key !== "j" && event.key !== "k") return;
    if (event.target.tagName === "INPUT") return;
    const visible = rows.filter((r) => !r.hidden).map((r) => r.querySelector(".row-btn"));
    const at = visible.indexOf(document.activeElement);
    const next = event.key === "j" ? at + 1 : at - 1;
    if (visible[next]) {
      visible[next].focus();
      event.preventDefault();
    }
  });

  detailEl.querySelectorAll(".row-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const row = btn.closest(".row");
      const panel = row.querySelector(".panel");
      const open = btn.getAttribute("aria-expanded") === "true";
      btn.setAttribute("aria-expanded", String(!open));
      row.dataset.open = String(!open);
      panel.hidden = open;
    });
  });
}

/* ---- boot -------------------------------------------------------------- */

async function select(runId, runs) {
  renderRunList(runs, runId, (id) => select(id, runs));
  const [run, entries] = await Promise.all([
    json(`/api/runs/${encodeURIComponent(runId)}`),
    json(`/api/runs/${encodeURIComponent(runId)}/audit`),
  ]);
  renderDetail(run, entries);
}

async function boot() {
  // Cloud Run cold-starts, so the first fetch can take seconds. Saying nothing
  // for that long reads as an empty product rather than a slow one.
  tallyEl.textContent = "loading";
  detailEl.innerHTML = `<div class="empty"><p>Loading runs\u2026</p></div>`;

  let runs = [];
  try {
    runs = await json("/api/runs");
  } catch (err) {
    detailEl.innerHTML = `<div class="empty"><h2>Cannot reach the run store</h2>
      <p>${esc(err.message)}</p></div>`;
    return;
  }

  if (!runs.length) {
    tallyEl.textContent = "0 runs";
    detailEl.innerHTML = `<div class="empty">
      <h2>No runs yet</h2>
      <p>Open or update a pull request on a watched repository. Curbcut renders
      the page, patches what it can, re-scans to check each patch held, and
      opens a pull request with only the changes that survived.</p></div>`;
    return;
  }

  // Open the newest run rather than an empty pane: the reason to be here is
  // to read a run, and one click of ceremony helps nobody.
  await select(runs[runs.length - 1].id, runs);
}

boot();
