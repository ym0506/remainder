const $ = (sel) => document.querySelector(sel);
const state = { seasonId: null, reading: null, answered: [], roundId: null };

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(typeof data.detail === "string" ? data.detail : `HTTP ${res.status}`);
    err.detail = data.detail;
    throw err;
  }
  return data;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function esc(text) {
  return String(text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function dollars(value) {
  const [whole, cents = "00"] = String(value).split(".");
  return "$" + Number(whole).toLocaleString("en-US") + "." + cents.padEnd(2, "0");
}

function exactDollars(exact) {
  const [n, d] = exact.split("/").map(Number);
  const cents = d ? n / d : n;
  return "$" + (cents / 100).toFixed(4);
}

function show(id) {
  $(id).hidden = false;
}

function setProgress(el, done, total, label) {
  el.hidden = false;
  el.querySelector("div").style.width = total ? `${(100 * done) / total}%` : "0";
  el.querySelector("span").textContent = label;
}

async function waitForSeason(seasonId, progressEl) {
  for (;;) {
    const s = await api("GET", `/api/season/${seasonId}`);
    const { done, total, error } = s.seeding;
    if (error) throw new Error(error);
    if (total) setProgress(progressEl, done ?? 0, total, `PayPal sandbox payments: ${done ?? 0} of ${total}`);
    if (s.complete) return s.payments;
    await sleep(700);
  }
}

function renderSeason(payments) {
  $("#season-table tbody").innerHTML = payments
    .map(
      (p) => `<tr><td>${esc(p.family_name)}</td><td class="n">${p.players}</td><td class="n">${p.paid}</td>` +
        `<td class="n">${p.refunded_before}</td><td><code>${esc(p.capture_id)}</code></td></tr>`,
    )
    .join("");
  $("#season-table summary").textContent = `${payments.length} payments recorded`;
  show("#season-table");
  show("#s-notice");
}

async function createSeason() {
  $("#b-season").disabled = true;
  try {
    const { season_id } = await api("POST", "/api/season", { compare: false });
    state.seasonId = season_id;
    renderSeason(await waitForSeason(season_id, $("#season-progress")));
  } catch (e) {
    alert(e.message);
    $("#b-season").disabled = false;
  }
}

function highlighted(notice, quotes) {
  let html = esc(notice);
  for (const q of quotes) {
    if (q && html.includes(esc(q))) html = html.replace(esc(q), `<mark>${esc(q)}</mark>`);
  }
  return html;
}

function renderReading() {
  const { rules, problems, source, model } = state.reading;
  $("#reading-source").textContent =
    source === "saved"
      ? `Saved result of a real ${model} run on this exact notice (shown so the demo works without an API key).`
      : `Read just now by ${model}.`;
  const items = [
    `<div class="rule">Returning <b>${dollars(rules.pool_amount)}</b><q>${esc(rules.pool_quote)}</q></div>`,
  ];
  if (rules.non_refundable_per_player) {
    items.push(`<div class="rule">Left out of each share: <b>${dollars(rules.non_refundable_per_player)}</b> per player<q>${esc(rules.non_refundable_quote)}</q></div>`);
  }
  for (const e of rules.excluded_families) {
    items.push(`<div class="rule">Excluded: <b>${esc(e.family)}</b> (${esc(e.reason)})<q>${esc(e.quote)}</q></div>`);
  }
  $("#reading .rules").innerHTML = items.join("");

  const open = problems.filter((p) => !(p.field === "question" && state.answered.includes(p.message)));
  $("#problems").innerHTML = open
    .map((p, i) =>
      p.field === "question"
        ? `<div class="problem">Claude asks: ${esc(p.message)}<br><button data-q="${i}">The notice is right, keep these rules</button></div>`
        : `<div class="problem">Check failed (${esc(p.field)}): ${esc(p.message)}</div>`,
    )
    .join("");
  $("#problems").querySelectorAll("button[data-q]").forEach((b) =>
    b.addEventListener("click", () => {
      state.answered.push(open[Number(b.dataset.q)].message);
      renderReading();
    }),
  );
  const quotes = [rules.pool_quote, rules.non_refundable_quote, ...rules.excluded_families.map((e) => e.quote)];
  $("#reading .quoted").innerHTML = highlighted($("#notice").value, quotes);
  show("#reading");
  $("#b-plan").disabled = open.length > 0;
  show("#s-plan");
}

async function readNotice() {
  $("#b-read").disabled = true;
  try {
    state.reading = await api("POST", "/api/read", { season_id: state.seasonId, notice: $("#notice").value });
    state.answered = [];
    renderReading();
  } catch (e) {
    alert(e.message);
  } finally {
    $("#b-read").disabled = false;
  }
}

function renderPlan(round) {
  const extra = round.rows.filter((r) => r.extra_cent).length;
  const sending = round.rows.filter((r) => r.state !== "SKIPPED").length;
  $("#plan .totals").innerHTML =
    `<div>To return<b>${round.pool}</b></div><div>Planned refunds<b>${round.planned_total}</b></div>` +
    `<div class="zero">Left over<b>$0.00</b></div><div>Refunds to send<b>${sending}</b></div>` +
    `<div>Got one extra cent<b>${extra} families</b></div>`;
  renderRows(round);
  show("#plan");
  $("#b-send").textContent = `Approve and send ${sending} refunds`;
  show("#s-send");
}

function renderRows(round) {
  $("#plan tbody").innerHTML = round.rows
    .map((r) => {
      const cls = r.excluded ? "excluded" : { DONE: "sent", SENDING: "sending", HELD: "held" }[r.state] || "";
      let status = r.excluded ? esc(r.excluded) : `<span class="state ${r.state.toLowerCase()}">${r.state}</span>`;
      if (r.refund_id) status += ` <code>${esc(r.refund_id)}</code>`;
      if (r.replayed) status += `<br><span class="fine">PayPal returned the refund it had already made</span>`;
      if (r.state === "HELD") status += `<br><span class="fine">${esc(r.detail)}</span>`;
      return (
        `<tr class="${cls}"><td>${esc(r.family_name)}</td><td class="n">${r.paid}</td><td class="n">${r.counted}</td>` +
        `<td class="n">${r.excluded ? "" : exactDollars(r.exact)}</td>` +
        `<td class="n">${r.share}${r.extra_cent ? '<span class="cent" title="got one of the leftover cents">+1¢</span>' : ""}</td>` +
        `<td>${status}</td></tr>`
      );
    })
    .join("");
}

async function makePlan() {
  $("#b-plan").disabled = true;
  try {
    const round = await api("POST", "/api/plan", {
      season_id: state.seasonId,
      notice: $("#notice").value,
      title: state.title,
      rules: state.reading.rules,
      answered_questions: state.answered,
      use_request_ids: true,
    });
    state.roundId = round.round_id;
    renderPlan(round);
  } catch (e) {
    alert(e.detail && e.detail.problems ? e.detail.problems.map((p) => p.message).join("\n") : e.message);
    $("#b-plan").disabled = false;
  }
}

async function runUntilStopped(seasonId, roundId, crashAfter, onUpdate) {
  await api("POST", "/api/run", { season_id: seasonId, round_id: roundId, crash_after_send: crashAfter });
  for (;;) {
    await sleep(600);
    const round = await api("GET", `/api/round/${seasonId}/${roundId}`);
    onUpdate(round);
    if (round.worker !== "running") return round;
  }
}

async function send(crashAfter) {
  $("#b-send").disabled = true;
  $("#b-resume").disabled = true;
  $("#crash").disabled = true;
  const round = await runUntilStopped(state.seasonId, state.roundId, crashAfter, renderRows);
  if (round.worker === "crashed") {
    show("#crash-banner");
    $("#b-resume").disabled = false;
  } else {
    $("#crash-banner").hidden = true;
    show("#s-check");
  }
}

function renderCheck(report, el) {
  const issues = report.families.filter((f) => f.status !== "OK");
  const matched = report.families.length - issues.length;
  const off = report.difference !== "$0.00";
  let html =
    `<p class="verdict ${report.clean ? "ok" : "bad"}">${report.clean ? "PayPal and the ledger agree." : "PayPal and the ledger disagree."}</p>` +
    `<div class="totals"><div>To return<b>${report.pool}</b></div><div>PayPal refunded<b>${report.paid_out}</b></div>` +
    `<div class="${off ? "off" : "zero"}">Difference<b>${report.difference}</b></div><div>Families matching<b>${matched} of ${report.families.length}</b></div></div>`;
  if (issues.length) {
    html +=
      `<table class="ledger"><thead><tr><th>Family</th><th class="n">Planned</th><th class="n">Refunds we recorded</th><th>What PayPal also shows</th><th>Status</th></tr></thead><tbody>` +
      issues
        .map(
          (f) =>
            `<tr class="held"><td>${esc(f.family_name)}</td><td class="n">${f.planned}</td><td class="n">${f.paypal}</td>` +
            `<td>${f.unrecorded.map((u) => `${u.amount} <code>${esc(u.refund_id)}</code>`).join("<br>") || ""}</td><td>${f.status}</td></tr>`,
        )
        .join("") +
      `</tbody></table>`;
  }
  el.innerHTML = html;
  el.hidden = false;
}

async function check() {
  $("#b-check").disabled = true;
  try {
    renderCheck(await api("GET", `/api/reconcile/${state.seasonId}/${state.roundId}`), $("#check"));
    show("#s-compare");
  } catch (e) {
    alert(e.message);
  } finally {
    $("#b-check").disabled = false;
  }
}

async function compare() {
  $("#b-compare").disabled = true;
  const progress = $("#compare-progress");
  try {
    const { season_id } = await api("POST", "/api/season", { compare: true });
    await waitForSeason(season_id, progress);
    const round = await api("POST", "/api/plan", {
      season_id,
      notice: $("#notice").value,
      title: state.title,
      rules: state.reading.rules,
      answered_questions: state.answered,
      use_request_ids: false,
    });
    const total = round.rows.filter((r) => r.state !== "SKIPPED").length;
    const count = (r) => r.rows.filter((x) => x.state === "DONE").length;
    setProgress(progress, 0, total, "Sending without request IDs...");
    await runUntilStopped(season_id, round.round_id, 9, (r) => setProgress(progress, count(r), total, `Sent ${count(r)} of ${total}, then the same crash after #9`));
    await runUntilStopped(season_id, round.round_id, null, (r) => setProgress(progress, count(r), total, `Plain retry: ${count(r)} of ${total}`));
    renderCheck(await api("GET", `/api/reconcile/${season_id}/${round.round_id}`), $("#compare"));
  } catch (e) {
    alert(e.message);
    $("#b-compare").disabled = false;
  }
}

async function start() {
  const d = await api("GET", "/api/demo");
  $("#notice").value = d.notice;
  state.title = d.title;
  const me = await api("GET", "/api/me");
  if (me.season_id) {
    state.seasonId = me.season_id;
    renderSeason(await waitForSeason(me.season_id, $("#season-progress")));
    $("#b-season").textContent = "Start a new season";
  }
  $("#b-season").addEventListener("click", createSeason);
  $("#b-read").addEventListener("click", readNotice);
  $("#b-plan").addEventListener("click", makePlan);
  $("#b-send").addEventListener("click", () => send($("#crash").checked ? 9 : null));
  $("#b-resume").addEventListener("click", () => send(null));
  $("#b-check").addEventListener("click", check);
  $("#b-compare").addEventListener("click", compare);
}

start();
