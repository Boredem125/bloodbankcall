const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pct = (x) => (x == null ? "–" : Math.round(x * 100) + "%");
const ORDER = ["calling", "deciding", "done"];
const AVAIL = {
  confirmed: ["Confirmed", "ok"],
  unconfirmed: ["Unconfirmed: call to verify", "warn"],
  not_available: ["Not available", "no"],
  closed: ["Closed", "no"],
  unknown: ["No answer", "muted"],
};
let source = null;
let current = null;

$("#search-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = Object.fromEntries(new FormData(e.target).entries());
  body.units = Number(body.units);
  $("#go").disabled = true;
  try {
    const r = await fetch("/searches", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!r.ok) throw new Error(await r.text());
    watch((await r.json()).id);
  } catch (err) {
    alert("Could not start: " + err.message);
    $("#go").disabled = false;
  }
});

function watch(id) {
  if (source) source.close();
  source = new EventSource(`/searches/${id}/stream`);
  source.onmessage = (ev) => render(JSON.parse(ev.data));
}

function render(s) {
  current = s;
  $("#empty").classList.add("hidden");
  $("#go").disabled = !["done", "failed"].includes(s.status);
  const idx = s.status === "failed" ? -1 : ORDER.indexOf(s.status);
  document.querySelectorAll("#steps li").forEach((li, i) => {
    li.className = idx < 0 ? "" : i < idx ? "done" : i === idx ? "now" : "";
  });
  renderVerdict(s);
  renderCalls(s);
  renderCompare(s);
  renderDecisions(s);
  $("#log").innerHTML = s.log.slice().reverse().map((l) => `<li>${esc(l)}</li>`).join("");
}

function badge(a) {
  const [label, cls] = AVAIL[a] || [a, "muted"];
  return `<span class="avail ${cls}">${esc(label)}</span>`;
}

function renderVerdict(s) {
  const v = $("#verdict");
  const warn = s.warnings?.length ? `<ul class="warnings">${s.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>` : "";
  if (s.status === "failed") {
    v.className = "panel verdict failed";
    v.innerHTML = `<h3>No confirmed blood yet</h3><p class="why">${esc(s.reason)}</p>${warn}`;
    return;
  }
  const chosen = s.calls.find((c) => c.id === s.chosen_call_id);
  if (!chosen) { v.className = "panel verdict hidden"; return; }
  const plan = s.plan.length > 1
    ? `<ol class="plan">${s.plan.map((p) => `<li><b>${p.units} unit(s)</b> from ${esc(p.facility)}</li>`).join("")}</ol>` : "";
  v.className = "panel verdict";
  v.innerHTML = `
    <h3>Go to</h3>
    <div class="who">${esc(chosen.facility.name)}</div>
    <div class="facts">
      <span>⏱ ~${chosen.eta_min} min</span>
      ${chosen.units ? `<span>🩸 ${chosen.units} unit(s)</span>` : ""}
      ${chosen.staff ? `<span>👤 ${esc(chosen.staff)}</span>` : ""}
      ${chosen.fee ? `<span>₹ ${chosen.fee}/unit</span>` : ""}
      <span>📞 ${esc(chosen.phone.number)}</span>
      <span>choice confidence ${pct(s.choice_confidence)}</span>
    </div>
    <p class="why">${esc(s.reason)}</p>${plan}${warn}`;
}

function sigRow(label, value, bad = false) {
  if (value == null) return "";
  return `<div class="sig"><span>${label}</span><span class="bar ${bad ? "neg" : ""}"><i style="width:${Math.round(value * 100)}%"></i></span><span>${pct(value)}</span></div>`;
}

function renderCalls(s) {
  $("#calls").innerHTML = s.calls.map((c) => {
    const lines = c.transcript.split("\n").filter(Boolean).slice(-4)
      .map((l) => l.startsWith("user:") ? `<span class="u">${esc(l.replace("user:", "staff:"))}</span>` : esc(l.replace("assistant:", "ai:"))).join("\n");
    const sg = c.signals || {};
    const done = c.outcome != null;
    return `<article class="card ${c.id === s.chosen_call_id ? "chosen" : ""} ${["unknown", "not_available", "closed"].includes(c.availability) && done ? "dead" : ""}">
      <div class="card-head"><div><b>${esc(c.facility.name)}</b>
        <div class="meta">${esc(c.phone.label)}${c.attempt > 1 ? " (fallback)" : ""} · ~${c.eta_min} min${c.facility.open_24x7 ? " · 24×7" : ""}</div></div>
        <span class="pill ${esc(c.status)}">${esc(c.status)}</span></div>
      ${done || ["no-answer", "busy", "voicemail", "failed", "timeout"].includes(c.status) ? badge(c.availability) : ""}
      ${lines ? `<div class="transcript">${lines}</div>` : ""}
      ${c.units != null || c.fee || c.staff ? `<div class="meta">${c.units != null ? `${c.units} unit(s)` : ""}${c.fee ? ` · ₹${c.fee}/unit` : ""}${c.staff ? ` · ${esc(c.staff)}` : ""}${c.groups_heard.length ? ` · heard ${esc([...new Set(c.groups_heard)].join(", "))}` : ""}</div>` : ""}
      ${done ? sigRow("has group", sg.has_requested) + sigRow("read-back ok", sg.confirmed_readback) + sigRow("other group", sg.mentioned_other_group, true) + sigRow("enough units", sg.enough_units) + sigRow("will hold", sg.will_hold) + sigRow("needs donor", sg.replacement_donor_required, true) : ""}
      ${c.gate_reasons?.length && c.availability === "unconfirmed" ? `<ul class="reasons">${c.gate_reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : ""}
      ${c.recording_url ? `<a class="meta" href="${esc(c.recording_url)}" target="_blank" rel="noopener">▶ recording · ${Math.round(c.duration_s)}s · ${c.cost_cents.toFixed(1)}¢</a>` : ""}
    </article>`;
  }).join("");
}

function renderCompare(s) {
  const rank = { confirmed: 0, unconfirmed: 1, not_available: 2, closed: 3, unknown: 4 };
  const rows = s.calls.filter((c) => c.outcome != null || ["no-answer", "busy", "voicemail", "failed", "timeout"].includes(c.status))
    .sort((a, b) => (rank[a.availability] - rank[b.availability]) || ((b.rank_score ?? -1) - (a.rank_score ?? -1)));
  if (!rows.length) { $("#compare").innerHTML = ""; return; }
  $("#compare").innerHTML = `<tr><th>Blood bank</th><th>Status</th><th>Units</th><th>ETA</th><th>Donor?</th><th>Hold</th><th>Score</th></tr>` +
    rows.map((c) => {
      const sg = c.signals || {};
      return `<tr class="${c.id === s.chosen_call_id ? "chosen" : ""}"><td>${esc(c.facility.name)}</td><td>${badge(c.availability)}</td>
        <td class="num">${c.units ?? "–"}</td><td class="num">${c.eta_min} min</td><td class="num">${pct(sg.replacement_donor_required)}</td>
        <td class="num">${pct(sg.will_hold)}</td><td class="num">${c.rank_score != null ? c.rank_score.toFixed(2) : ""}</td></tr>`;
    }).join("");
}

function renderDecisions(s) {
  const name = (id) => s.calls.find((c) => c.id === id)?.facility.name || "";
  $("#decisions").innerHTML = s.decisions.slice().reverse().map((d) => {
    const what = d.stage === "call"
      ? `${esc(name(d.subject))}: <code>${esc(d.answers.outcome?.choice)}</code>, has group ${pct(d.answers.has_requested?.noul)}`
      : `go to → <code>${esc(name(d.answers.best_facility?.choice))}</code> (${pct(d.answers.best_facility?.confidence)})`;
    return `<li><b>${esc(d.backend)}</b> · ${d.latency_ms} ms · ${what}</li>`;
  }).join("");
}
