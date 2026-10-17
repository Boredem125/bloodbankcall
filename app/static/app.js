const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pct = (x) => (x == null ? "–" : Math.round(x * 100) + "%");
const ORDER = ["planning", "calling", "deciding", "awaiting_approval", "booked"];
let source = null;
let current = null;

$("#errand-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  const body = Object.fromEntries(f.entries());
  body.budget = Number(body.budget);
  body.auto_book_limit = Number(body.auto_book_limit || 0);
  $("#go").disabled = true;
  try {
    const r = await fetch("/errands", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!r.ok) throw new Error(await r.text());
    const { id } = await r.json();
    watch(id);
  } catch (err) {
    alert("Could not start: " + err.message);
    $("#go").disabled = false;
  }
});

function watch(id) {
  if (source) source.close();
  source = new EventSource(`/errands/${id}/stream`);
  source.onmessage = (ev) => render(JSON.parse(ev.data));
}

async function approve(callId) {
  const r = await fetch(`/errands/${current.id}/approve`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ call_id: callId }),
  });
  if (!r.ok) alert((await r.json()).detail);
}
window.approve = approve;

function render(e) {
  current = e;
  $("#empty").classList.add("hidden");
  const done = ["booked", "failed"].includes(e.status);
  $("#go").disabled = !done && e.status !== "awaiting_approval";
  renderSteps(e.status);
  renderVerdict(e);
  renderCalls(e);
  renderCompare(e);
  renderDecisions(e);
  $("#log").innerHTML = e.log.slice().reverse().map((l) => `<li>${esc(l)}</li>`).join("");
}

function renderSteps(status) {
  const idx = status === "booking" ? 3 : ORDER.indexOf(status);
  document.querySelectorAll("#steps li").forEach((li, i) => {
    li.className = idx < 0 ? "" : i < idx ? "done" : i === idx ? "now" : "";
  });
}

function renderVerdict(e) {
  const v = $("#verdict");
  const chosen = e.calls.find((c) => c.id === e.chosen_call_id);
  if (e.status === "failed") {
    v.className = "panel verdict failed";
    v.innerHTML = `<div class="who">No booking</div><p class="why">${esc(e.reason)}</p>`;
    return;
  }
  if (!chosen) { v.className = "panel verdict hidden"; return; }
  const booked = e.status === "booked";
  v.className = "panel verdict" + (booked ? " booked" : "");
  v.innerHTML = `
    <h3>${booked ? "Booked" : e.status === "booking" ? "Booking…" : "Recommended"}</h3>
    <div class="who">${esc(chosen.vendor.name)} · Rs ${esc(chosen.price)}</div>
    <p class="why">${esc(e.reason)}</p>
    <div class="meters">
      <span>Choice confidence <b>${pct(e.choice_confidence)}</b></span>
      <span>Safe to auto-book <b>${pct(e.auto_book_probability)}</b></span>
      <span>Calls <b>${e.calls.filter((c) => c.purpose === "quote").length}</b></span>
    </div>
    ${e.status === "awaiting_approval" ? `<button onclick="approve('${chosen.id}')">Book ${esc(chosen.vendor.name)}</button>` : ""}`;
}

function sigRow(label, value, negative = false) {
  if (value == null) return "";
  return `<div class="sig"><span>${label}</span><span class="bar ${negative ? "neg" : ""}"><i style="width:${Math.round(value * 100)}%"></i></span><span>${pct(value)}</span></div>`;
}

function renderCalls(e) {
  $("#calls").innerHTML = e.calls.map((c) => {
    const dead = ["no-answer", "busy", "failed", "timeout", "error"].includes(c.status) || (c.outcome && !c.viable && c.status === "completed" && c.purpose === "quote");
    const lines = c.transcript.split("\n").filter(Boolean).slice(-4)
      .map((l) => l.startsWith("user:") ? `<span class="u">${esc(l.replace("user:", "biz:"))}</span>` : esc(l.replace("assistant:", "ai:"))).join("\n");
    const s = c.signals || {};
    return `<article class="card ${c.id === e.chosen_call_id ? "chosen" : ""} ${dead ? "dead" : ""}">
      <div class="card-head"><div><b>${esc(c.vendor.name)}</b><div class="meta">${esc(c.vendor.area)} · ★ ${c.vendor.rating}${c.purpose === "book" ? " · booking call" : ""}</div></div>
      <span class="pill ${esc(c.status)}">${esc(c.status)}</span></div>
      ${lines ? `<div class="transcript">${lines}</div>` : ""}
      ${c.price ? `<div class="price">Rs ${c.price} <small>${esc(c.slot || "")}</small></div>` : ""}
      ${c.recording_url ? `<a class="meta" href="${esc(c.recording_url)}" target="_blank" rel="noopener">▶ recording · ${Math.round(c.duration_s)}s · ${c.cost_cents.toFixed(1)}¢</a>` : ""}
      ${c.outcome ? `<div class="tag-out">${esc(c.outcome.replace("_", " "))}${c.rank_score != null ? ` · score ${c.rank_score.toFixed(2)}` : ""}</div>` : ""}
      ${sigRow("within budget", s.within_budget)}${sigRow("slot fits", s.slot_fits)}${sigRow("reliability", s.reliability)}${sigRow("hidden charges", s.hidden_charges, true)}
      ${e.status === "awaiting_approval" && c.viable && c.id !== e.chosen_call_id ? `<button onclick="approve('${c.id}')" style="padding:6px">Book this instead</button>` : ""}
    </article>`;
  }).join("");
}

function renderCompare(e) {
  const rows = e.calls.filter((c) => c.purpose === "quote" && c.status === "completed")
    .sort((a, b) => (b.rank_score ?? -1) - (a.rank_score ?? -1));
  if (!rows.length) { $("#compare").innerHTML = ""; return; }
  $("#compare").innerHTML = `<tr><th>Business</th><th>Price</th><th>Slot</th><th>Budget</th><th>Hidden</th><th>Slot fit</th><th>Reliab.</th><th>Score</th></tr>` +
    rows.map((c) => {
      const s = c.signals || {};
      return `<tr class="${c.id === e.chosen_call_id ? "chosen" : ""}"><td>${esc(c.vendor.name)}</td><td class="num">${c.price ? "Rs " + c.price : "–"}</td><td>${esc(c.slot || "–")}</td>
        <td class="num">${pct(s.within_budget)}</td><td class="num">${pct(s.hidden_charges)}</td><td class="num">${pct(s.slot_fits)}</td><td class="num">${pct(s.reliability)}</td>
        <td class="num">${c.rank_score != null ? c.rank_score.toFixed(2) : esc(c.outcome || "")}</td></tr>`;
    }).join("");
}

function renderDecisions(e) {
  const name = (id) => e.calls.find((c) => c.id === id)?.vendor.name || "";
  $("#decisions").innerHTML = e.decisions.slice().reverse().map((d) => {
    let what = "";
    if (d.stage === "intake") what = `category → <code>${esc(d.answers.category?.choice)}</code>`;
    if (d.stage === "call") what = `${esc(name(d.subject))}: outcome <code>${esc(d.answers.outcome?.choice)}</code>`;
    if (d.stage === "final") what = `best → <code>${esc(name(d.answers.best_vendor?.choice))}</code> (${pct(d.answers.best_vendor?.confidence)})`;
    return `<li><b>${esc(d.backend)}</b> · ${d.latency_ms} ms · ${what}</li>`;
  }).join("");
}
