const rows = document.getElementById("rows");
const empty = document.getElementById("empty");
const detail = document.getElementById("detail");
const detailHeading = document.getElementById("detail-heading");
const verification = document.getElementById("verification");
const trail = document.getElementById("trail");

async function load() {
  const runs = await (await fetch("/api/runs")).json();
  rows.replaceChildren(...runs.map(render));
  empty.hidden = runs.length > 0;
}

function render(run) {
  const tr = document.createElement("tr");
  tr.tabIndex = 0;
  tr.setAttribute("role", "button");
  tr.setAttribute(
    "aria-label",
    `${run.repo}, pull request ${run.pr}, ${run.status}, ${run.fixed} verified fixes. Open reasoning chain.`
  );
  tr.innerHTML =
    `<td>${escapeHtml(run.repo)}</td><td>#${escapeHtml(String(run.pr))}</td>` +
    `<td><span class="chip ${run.status}">${escapeHtml(run.status)}</span></td>` +
    `<td>${escapeHtml(String(run.fixed))}</td>`;
  const open = () => show(run.id ?? run.repo);
  tr.addEventListener("click", open);
  tr.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(); }
  });
  return tr;
}

function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = value;
  return div.innerHTML;
}

// A run that leaves the tree modified or the audit trail incomplete must say
// so where a human sees it — ruling 3 in the batch brief. These fields are
// optional: older or hand-written run records (as in the API tests) may not
// carry them, and this renders only what is actually present rather than
// implying a false "yes" or "no".
const VERIFICATION_FIELDS = [
  ["safe_to_ship", "Safe to ship", "yes", "no"],
  ["tree_modified", "Tree modified", "no", "yes"],
  ["audit_complete", "Audit trail complete", "yes", "no"],
];

function renderVerification(run) {
  const entries = [];
  for (const [key, label, whenTrue, whenFalse] of VERIFICATION_FIELDS) {
    if (typeof run[key] !== "boolean") continue;
    const value = run[key] ? whenTrue : whenFalse;
    entries.push([label, value, run[key]]);
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
      const chip = document.createElement("span");
      chip.className = `chip ${ok ? "yes" : "no"}`;
      chip.textContent = value;
      dd.appendChild(chip);
      return [dt, dd];
    })
  );
}

async function show(runId) {
  const [run, entries] = await Promise.all([
    fetch(`/api/runs/${runId}`).then((r) => (r.ok ? r.json() : null)),
    fetch(`/api/runs/${runId}/audit`).then((r) => r.json()),
  ]);

  if (run) renderVerification(run);
  else verification.replaceChildren();

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
  detailHeading.focus();
}

load();
