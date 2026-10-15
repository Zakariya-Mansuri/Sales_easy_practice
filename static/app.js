// SalesEasy UI. Model output is untrusted (it can echo injected text), so
// every value is written with textContent — never innerHTML.

const $ = (id) => document.getElementById(id);

const els = {
  form: $("form"), update: $("update"), submit: $("submit"),
  samples: $("samples"), records: $("records"), col: $("result-col"),
  recordsState: $("records-state"), addRecord: $("add-record"), resetRecords: $("reset-records"),
  empty: $("state-empty"), loading: $("state-loading"),
  error: $("state-error"), result: $("state-result"),
};

const REQUEST_TIMEOUT_MS = 60000;
const MAX_RECORDS = 50;
let lastJson = "";
let defaults = [];
let records = [];

function el(tag, attrs = {}, text) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (text !== undefined) node.textContent = text;
  return node;
}

const humanize = (s) => String(s).replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());

function show(state) {
  for (const k of ["empty", "loading", "error", "result"]) els[k].hidden = k !== state;
  els.col.setAttribute("aria-busy", state === "loading" ? "true" : "false");
  // Single-column layout puts the result below the fold: without this, tapping
  // "Analyze" on a phone appears to do nothing.
  if (state !== "empty" && matchMedia("(max-width: 860px)").matches) {
    const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
    els.col.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "start" });
  }
}

// ---------- records (client-held; IDs are positional, matching the server) ----------
const isEdited = () => JSON.stringify(records) !== JSON.stringify(defaults);

function syncRecordsChrome() {
  const edited = isEdited();
  els.recordsState.textContent = edited ? "edited" : "defaults";
  els.recordsState.className = "tag" + (edited ? " tag--edited" : "");
  els.resetRecords.hidden = !edited;
  els.addRecord.disabled = records.length >= MAX_RECORDS;
}

function renderRecords() {
  els.records.replaceChildren();
  records.forEach((r, i) => {
    const tr = el("tr");
    const field = (key, attrs) => {
      const input = el("input", { "aria-label": `${key} for R${i + 1}`, ...attrs });
      input.value = r[key];
      input.addEventListener("input", () => {
        // Keep stock numeric when it parses, so retyping the default value doesn't count as an edit.
        const v = input.value;
        r[key] = key === "system_stock" && /^\d{1,7}$/.test(v.trim()) ? Number(v.trim()) : v;
        input.removeAttribute("aria-invalid");
        syncRecordsChrome();
      });
      const td = el("td"); td.append(input); return td;
    };
    const rm = el("button", { type: "button", class: "btn-remove", "aria-label": `Remove R${i + 1}`, title: "Remove" }, "×");
    rm.addEventListener("click", () => { records.splice(i, 1); renderRecords(); });
    const rmTd = el("td"); rmTd.append(rm);
    tr.append(
      el("td", { class: "id" }, `R${i + 1}`),
      field("outlet", { maxlength: "80" }),
      field("sku", { maxlength: "80" }),
      field("system_stock", { class: "num", inputmode: "numeric", maxlength: "7" }),
      rmTd,
    );
    els.records.append(tr);
  });
  syncRecordsChrome();
}

// Returns cleaned records or null (and marks the bad cells) if any row is invalid.
function validRecords() {
  const rows = [...els.records.querySelectorAll("tr")];
  const seen = new Set();
  let firstBad = null;
  const out = records.map((r, i) => {
    const inputs = rows[i].querySelectorAll("input");
    const outlet = String(r.outlet).trim(), sku = String(r.sku).trim();
    const stockStr = String(r.system_stock).trim();
    const stock = /^\d{1,7}$/.test(stockStr) ? Number(stockStr) : NaN;
    const key = outlet.toLowerCase() + "\u0000" + sku.toLowerCase();
    const bad = [!outlet, !sku || seen.has(key), !Number.isInteger(stock)];
    seen.add(key);
    bad.forEach((b, j) => { if (b) { inputs[j].setAttribute("aria-invalid", "true"); firstBad ??= inputs[j]; } });
    return { outlet, sku, system_stock: stock };
  });
  if (firstBad) { firstBad.focus(); return null; }
  return out;
}

els.addRecord.addEventListener("click", () => {
  records.push({ outlet: "", sku: "", system_stock: "" });
  renderRecords();
  els.records.querySelector("tr:last-child input")?.focus();
});
els.resetRecords.addEventListener("click", () => {
  records = structuredClone(defaults);
  renderRecords();
});

// ---------- bootstrap ----------
async function bootstrap() {
  try {
    const [ref, smp] = await Promise.all([
      fetch("/api/reference").then((r) => r.json()),
      fetch("/api/samples").then((r) => r.json()),
    ]);
    defaults = ref.records;
    records = structuredClone(defaults);
    renderRecords();
    for (const s of smp.samples) {
      const b = el("button", { type: "button", class: "sample", role: "listitem", title: s.text });
      b.append(el("span", { class: "sample__id" }, s.id), document.createTextNode(s.label));
      b.addEventListener("click", () => { els.update.value = s.text; els.update.focus(); });
      els.samples.append(b);
    }
  } catch {
    const tr = el("tr"); tr.append(el("td", { colspan: "5" }, "Could not load records."));
    els.records.append(tr);
  }
}

// ---------- render ----------
function render(data) {
  const a = data.analysis;
  const t = data.trace;

  $("verdict").className = "verdict " + (a.needs_human_review ? "verdict--review" : "verdict--clear");
  $("verdict-title").textContent = a.needs_human_review ? "Needs human review" : "No human review needed";
  // Several reasons → numbered list in priority order (the JSON field stays one string, per the brief).
  const reasonBox = $("verdict-reason");
  const parts = t.review_reasons || [];
  if (a.needs_human_review && parts.length > 1) {
    const ol = el("ol");
    parts.forEach((p) => ol.append(el("li", {}, p)));
    reasonBox.replaceChildren(ol);
  } else {
    reasonBox.textContent = a.needs_human_review
      ? (a.review_reason || "No reason given.")
      : "Safe to proceed with the recommended action.";
  }

  $("r-category").textContent = humanize(a.issue_category);
  const urg = $("r-urgency");
  urg.textContent = humanize(a.urgency);
  urg.className = "urg--" + a.urgency;

  $("r-action").textContent = a.recommended_next_action;

  const ev = $("r-evidence");
  ev.replaceChildren();
  if (!a.evidence.length) ev.append(el("li", { class: "none" }, "No evidence extracted."));
  for (const e of a.evidence) {
    const li = el("li", { class: "ev--" + e.status });
    const tag = [e.source === "system_record" ? "system" : "field", e.record_id, e.status].filter(Boolean).join(" · ");
    li.append(
      el("span", { class: "dot dot--" + e.status, "aria-label": e.status }),
      el("span", { class: "ev__text" }, e.statement),
      el("span", { class: "ev__tag" }, tag),
    );
    ev.append(li);
  }

  const miss = $("r-missing");
  miss.replaceChildren();
  if (!a.missing_information.length) miss.append(el("li", { class: "none" }, "Nothing flagged."));
  for (const m of a.missing_information) miss.append(el("li", {}, m));

  const tr = $("r-trace");
  tr.replaceChildren();
  const list = (items) => {
    if (!items.length) return "none";
    const ul = el("ul");
    for (const it of items) ul.append(typeof it === "string" ? el("li", {}, it) : it);
    return ul;
  };
  const checks = t.stock_checks.map((c) => {
    const li = el("li");
    li.append(
      document.createTextNode(`${c.record_id} ${c.outlet} / ${c.sku}: system ${c.system_stock}, rep says ${humanize(c.claimed_state).toLowerCase()} → `),
      el("span", { class: "res--" + c.result }, c.result.replace("_", " ")),
    );
    return li;
  });
  const rows = [
    ["engine", t.engine],
    ["model", t.model || "—"],
    ["latency", t.latency_ms != null ? `${t.latency_ms} ms` : "—"],
    ["attempts", String(t.attempts)],
    ["records", t.records_source === "request" ? "edited set (this request)" : "defaults"],
    ["stock checks", list(checks)],
    ["guardrails fired", list(t.guardrails)],
    ["notes", list(t.notes)],
  ];
  for (const [k, v] of rows) {
    const dd = el("dd");
    typeof v === "string" ? (dd.textContent = v) : dd.append(v);
    tr.append(el("dt", {}, k), dd);
  }

  lastJson = JSON.stringify(a, null, 2);
  $("r-json").textContent = lastJson;

  show("result");
}

function renderError(title, detail) {
  els.error.replaceChildren(el("strong", {}, title), el("p", {}, detail));
  show("error");
}

// ---------- submit ----------
async function submit() {
  const text = els.update.value.trim();
  if (!text) { renderError("Nothing to analyze.", "Enter a field update first."); els.update.focus(); return; }
  const recs = validRecords();
  if (!recs) { renderError("Fix the highlighted records.", "Each record needs an outlet, a SKU, a whole-number stock, and no duplicate outlet + SKU pairs."); return; }

  els.submit.disabled = true;
  show("loading");
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), REQUEST_TIMEOUT_MS);
  try {
    const body = { update: text };
    if (isEdited()) body.records = recs;
    const res = await fetch("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: ctrl.signal,
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      const detail = Array.isArray(err.detail) ? err.detail.map((d) => d.msg).join("; ") : err.detail;
      renderError(`Request failed (${res.status}).`, detail || "The server could not process this update.");
      return;
    }
    render(await res.json());
  } catch (err) {
    renderError(
      err.name === "AbortError" ? "Timed out." : "Network error.",
      "No assessment was produced. Treat this update as unreviewed and route it to a supervisor.",
    );
  } finally {
    clearTimeout(timer);
    els.submit.disabled = false;
  }
}

// ---------- evaluation sample ----------
function verdictLine(v) {
  if (v.rejected) return `rejected at input: ${v.rejected}`;
  if (v.review == null) return "no answer";
  return `${v.review ? "review" : "no review"} · ${v.urgency} · ${humanize(v.category).toLowerCase()}`;
}

const passTag = (ok) => el("span", { class: "pf " + (ok ? "pf--pass" : "pf--fail") }, ok ? "PASS" : "FAIL");

function evalCard(c) {
  const card = el("article", { class: "ecard" });
  const top = el("div", { class: "ecard__top" });
  top.append(
    el("span", { class: "ecard__id" }, c.id),
    el("span", { class: "ecard__fam" }, c.family.replace(/_/g, " ")),
    passTag(c.full.pass),
  );
  card.append(top, el("p", { class: "ecard__text" }, c.text), ...caseDetails(c));
  return card;
}

// Expected vs actual + "run live": shared by the 3-card sample and the full list.
function caseDetails(c) {
  const dl = el("dl", { class: "edetails" });
  const row = (k, v) => { const dd = el("dd"); typeof v === "string" ? (dd.textContent = v) : dd.append(v); dl.append(el("dt", {}, k), dd); };
  const ul = (items, cls) => { const u = el("ul", cls ? { class: cls } : {}); items.forEach((i) => u.append(el("li", {}, i))); return u; };
  row("expected", c.expected.length ? ul(c.expected) : "no specific assertion (invariants only)");
  row("pipeline", verdictLine(c.full));
  if (c.full.failures.length) row("failed", ul(c.full.failures, "fail-list"));
  row("model alone", (c.model_only.pass ? "pass" : "fail") + (c.model_only.failures.length ? ` — ${c.model_only.failures[0]}` : ""));
  const run = el("button", { type: "button", class: "btn-quiet" }, "Run this case live");
  run.addEventListener("click", () => {
    els.update.value = c.text;
    records = structuredClone(c.records);
    renderRecords();
    window.scrollTo({ top: 0, behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
    submit();
  });
  const foot = el("div", { class: "ecard__foot" }); foot.append(run);
  return [dl, foot];
}

// ---------- all cases ----------
let allCases = null;
let totalCases = null;

function filterAll() {
  const fam = $("eall-family").value, st = $("eall-status").value;
  const shown = allCases.filter((c) =>
    (fam === "all" || c.family === fam) &&
    (st === "all" ||
     (st === "pipeline_fail" && !c.full.pass) ||
     (st === "model_fail" && !c.model_only.pass) ||
     (st === "saved" && c.full.pass && !c.model_only.pass)));
  $("eall-count").textContent = `Showing ${shown.length} of ${allCases.length}`;
  const list = $("eall-list");
  list.replaceChildren(...shown.map((c) => {
    const row = el("details", { class: "erow" });
    const sum = el("summary");
    sum.append(
      el("span", { class: "ecard__id" }, c.id),
      el("span", { class: "erow__text" }, c.text),
      el("span", { class: "erow__verdict" }, verdictLine(c.full)),
      passTag(c.full.pass),
    );
    const body = el("div", { class: "erow__body" });
    body.append(el("p", { class: "ecard__fam" }, c.family.replace(/_/g, " ")), ...caseDetails(c));
    row.append(sum, body);
    return row;
  }));
  if (!shown.length) list.append(el("p", { class: "hint" }, "No cases match these filters."));
}

async function toggleAll() {
  const panel = $("eall"), btn = $("evals-all");
  const open = panel.hidden;
  panel.hidden = !open;
  btn.textContent = open ? "Hide all cases" : `Show all ${totalCases ?? ""}`.trim();
  btn.setAttribute("aria-expanded", String(open));
  if (!open || allCases) return;
  $("eall-count").textContent = "Loading…";
  try {
    const res = await fetch("/api/evals/all");
    if (!res.ok) throw new Error();
    allCases = (await res.json()).cases;
    const fams = [...new Set(allCases.map((c) => c.family))];
    $("eall-family").replaceChildren(
      el("option", { value: "all" }, `All families (${allCases.length})`),
      ...fams.map((f) => el("option", { value: f }, `${f.replace(/_/g, " ")} (${allCases.filter((c) => c.family === f).length})`)),
    );
    filterAll();
  } catch {
    $("eall-count").textContent = "Could not load evaluation cases.";
  }
}
$("evals-all").addEventListener("click", toggleAll);
$("eall-family").addEventListener("change", filterAll);
$("eall-status").addEventListener("change", filterAll);

async function loadEvals() {
  const box = $("evals-cards");
  try {
    const res = await fetch("/api/evals/sample?n=3");
    if (!res.ok) throw new Error();
    const d = await res.json();
    const s = d.summary, m = d.meta;
    const sum = $("evals-summary");
    sum.replaceChildren(
      document.createTextNode(`${s.n} cases · full pipeline `), el("b", {}, `${s.full_pass}/${s.n}`),
      document.createTextNode(" · model alone "), el("b", {}, `${s.model_only_pass}/${s.n}`),
      el("span", { class: "muted" }, ` · run ${m.label}, ${m.model}, ${m.timestamp.slice(0, 10)}`),
    );
    box.replaceChildren(...d.cases.map(evalCard));
    totalCases = s.n;
    if ($("eall").hidden) $("evals-all").textContent = `Show all ${s.n}`;
  } catch {
    box.replaceChildren(el("p", { class: "hint" }, "Evaluation results are not available."));
  }
}
$("evals-refresh").addEventListener("click", loadEvals);

els.form.addEventListener("submit", (e) => { e.preventDefault(); submit(); });
els.update.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); submit(); }
});
$("copy-json").addEventListener("click", async (e) => {
  try { await navigator.clipboard.writeText(lastJson); e.target.textContent = "Copied"; }
  catch { e.target.textContent = "Copy failed"; }
  setTimeout(() => (e.target.textContent = "Copy"), 1500);
});

bootstrap();
loadEvals();
