const $ = (sel) => document.querySelector(sel);
const state = { seasonId: null, reading: null, replies: null, confirmed: [], ignored: [], round: null, check: null };

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const problems = data.detail && data.detail.problems;
    const text = problems ? problems.map((p) => p.message).join(" · ") : typeof data.detail === "string" ? data.detail : `HTTP ${res.status}`;
    throw new Error(text);
  }
  return data;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function esc(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function dollars(value) {
  const [whole, cents = "00"] = String(value).split(".");
  return "$" + Number(whole).toLocaleString("en-US") + "." + cents.padEnd(2, "0");
}

function exactDollars(exact) {
  const [n, d] = exact.split("/").map(Number);
  return "$" + ((d ? n / d : n) / 100).toFixed(4);
}

function show(id) {
  $(id).hidden = false;
}

function say(id, text, bad = false) {
  const el = $(id);
  el.textContent = text || "";
  el.classList.toggle("bad", bad);
}

async function step(button, statusId, work) {
  $(button).disabled = true;
  say(statusId, "");
  try {
    await work();
  } catch (e) {
    say(statusId, e.message, true);
  } finally {
    $(button).disabled = false;
  }
}

function setProgress(el, done, total, label) {
  el.hidden = false;
  el.querySelector("div").style.width = total ? `${(100 * done) / total}%` : "0";
  el.querySelector("span").textContent = label;
}

// 1. Season

async function waitForSeason(seasonId, progressEl) {
  for (;;) {
    const s = await api("GET", `/api/season/${seasonId}`);
    if (s.seeding.error) throw new Error(s.seeding.error);
    if (s.complete) {
      progressEl.hidden = true;
      return s.payments;
    }
    const { done, total } = s.seeding;
    if (total) setProgress(progressEl, done ?? 0, total, `Creating payments in the PayPal sandbox: ${done ?? 0} of ${total}`);
    await sleep(700);
  }
}

function renderSeason(payments) {
  $("#season-table tbody").innerHTML = payments
    .map(
      (p) => `<tr><td>${esc(p.family_name)}</td><td class="n">${p.players}</td><td class="n">${p.paid}</td>` +
        `<td class="n">${p.refunded_before}</td><td class="wide"><code>${esc(p.capture_id)}</code></td></tr>`,
    )
    .join("");
  $("#season-table summary").textContent = `${payments.length} PayPal payments on record`;
  show("#season-table");
  show("#s-notice");
  $("#b-season").textContent = "Start over with a new season";
}

async function openSeason() {
  const { season_id } = await api("POST", "/api/season", { compare: false });
  Object.assign(state, { seasonId: season_id, reading: null, replies: null, confirmed: [], ignored: [], round: null, check: null });
  for (const id of ["#s-replies", "#s-plan", "#s-send", "#s-check", "#s-compare", "#reading", "#plan", "#check", "#compare", "#crash-banner", "#summary", "#replies-source"]) $(id).hidden = true;
  $("#replies-problems").innerHTML = "";
  $("#b-send").hidden = false;
  $("#b-plan").hidden = false;
  $("#crash").disabled = false;
  renderInbox();
  renderSeason(await waitForSeason(season_id, $("#season-progress")));
}

// 2. Notice

function problemCards(problems, container, onConfirm) {
  container.innerHTML = problems
    .map((p, i) => {
      if (!p.confirmable) return `<div class="problem">Fix before planning: ${esc(p.message)}</div>`;
      const done = state.confirmed.includes(p.message);
      return `<div class="problem confirm ${done ? "done" : ""}">${esc(p.message)}` +
        (done ? ` <span class="ok-mark">Confirmed</span>` : `<br><button data-i="${i}">I've checked this; keep the rules as they are</button>`) +
        `</div>`;
    })
    .join("");
  container.querySelectorAll("button[data-i]").forEach((b) =>
    b.addEventListener("click", () => {
      state.confirmed.push(problems[Number(b.dataset.i)].message);
      onConfirm();
    }),
  );
}

function highlighted(text, quotes) {
  let html = esc(text);
  for (const q of quotes) {
    const target = q && esc(q.trim());
    if (target && html.includes(target)) html = html.replace(target, `<mark>${target}</mark>`);
  }
  return html;
}

function cleared(problems) {
  return problems.every((p) => p.confirmable && state.confirmed.includes(p.message));
}

function renderReading() {
  const { rules, source, model, problems, notice } = state.reading;
  $("#reading-source").textContent =
    source === "saved"
      ? `Live reading isn't available right now, so this is the saved result of a real ${model} run on this exact notice.`
      : `Read by ${model}.`;
  const items = [`<div class="rule">Returning <b>${dollars(rules.pool_amount)}</b><q>${esc(rules.pool_quote)}</q></div>`];
  if (rules.non_refundable_per_player) {
    items.push(`<div class="rule">Left out of each share: <b>${dollars(rules.non_refundable_per_player)}</b> per player<q>${esc(rules.non_refundable_quote)}</q></div>`);
  }
  for (const e of rules.excluded_families) {
    items.push(`<div class="rule">Not included: <b>${esc(e.family)}</b>, ${esc(e.reason)}<q>${esc(e.quote)}</q></div>`);
  }
  $("#reading .rules").innerHTML = items.join("");
  problemCards(problems, $("#reading .problems"), renderReading);
  const quotes = [rules.pool_quote, rules.non_refundable_quote, ...rules.excluded_families.map((e) => e.quote)];
  $("#reading .quoted").innerHTML = highlighted(notice, quotes);
  show("#reading");
  $("#s-replies").hidden = !cleared(problems);
}

async function readNotice() {
  say("#read-status", "Claude is reading the notice...");
  state.reading = await api("POST", "/api/read", { season_id: state.seasonId, notice: $("#notice").value });
  state.replies = null;
  say("#read-status", "");
  for (const id of ["#s-plan", "#replies-source"]) $(id).hidden = true;
  $("#replies-problems").innerHTML = "";
  renderReading();
  renderInbox();
}

// 3. Replies

const KIND = {
  keep_share: ["Keep their share", "kind-keep"],
  question: ["Question", "kind-question"],
  change_destination: ["Can't do this", "kind-stop"],
  outside_rules: ["Outside this refund", "kind-muted"],
  no_action: ["Nothing to do", "kind-muted"],
};

function replyEffect(r) {
  if (r.kind === "keep_share") {
    const on = !state.ignored.includes(r.reply_id);
    return `Their share is worked out as usual and kept for the club instead of refunded. ` +
      `<label class="check small inline"><input type="checkbox" data-keep="${r.reply_id}" ${on ? "checked" : ""}> Apply</label>`;
  }
  if (r.kind === "question" && r.notice_quote) return `The notice answers it. Suggested reply: <q>${esc(r.notice_quote)}</q>`;
  if (r.kind === "question") return "The notice doesn't answer this, so the plan waits until you confirm the rules stay as they are.";
  if (r.kind === "change_destination")
    return "A PayPal refund can only go back to the payment it came from, and Remainder has no field for another account or email. Nothing changes. A request to send money somewhere new is worth confirming with the family directly.";
  if (r.kind === "outside_rules") return "The notice doesn't provide this. Nothing changes; answer it outside this refund.";
  return "No change.";
}

function renderInbox() {
  const sorted = state.replies ? Object.fromEntries(state.replies.results.map((r) => [r.reply_id, r])) : {};
  $("#inbox").innerHTML = state.demo.replies
    .map((m) => {
      const r = sorted[m.reply_id];
      const [label, cls] = r ? KIND[r.kind] : ["", ""];
      return `<div class="mail"><div class="from">From the ${esc(m.family)} family${r ? ` <span class="kind ${cls}">${label}</span>` : ""}</div>` +
        `<div class="body">${r ? highlighted(m.text, [r.quote]) : esc(m.text)}</div>` +
        (r ? `<div class="effect">${esc(r.summary)} ${replyEffect(r)}</div>` : "") +
        `</div>`;
    })
    .join("");
  $("#inbox").querySelectorAll("input[data-keep]").forEach((box) =>
    box.addEventListener("change", () => {
      const id = Number(box.dataset.keep);
      state.ignored = box.checked ? state.ignored.filter((x) => x !== id) : [...state.ignored, id];
    }),
  );
}

function renderReplies() {
  renderInbox();
  const { source, model, problems } = state.replies;
  const el = $("#replies-source");
  el.textContent = source === "saved"
    ? `Live sorting isn't available right now, so this is the saved result of a real ${model} run on these replies.`
    : `Sorted by ${model}.`;
  el.hidden = false;
  problemCards(problems, $("#replies-problems"), renderReplies);
  $("#s-plan").hidden = !cleared(problems);
}

async function sortReplies() {
  say("#replies-status", "Claude is reading the replies...");
  state.replies = await api("POST", "/api/replies", { season_id: state.seasonId });
  say("#replies-status", "");
  renderReplies();
}

// 4. Plan

function renderSummary(round) {
  const parts = [`<div>Pool<b>${round.pool}</b></div>`];
  if (round.kept !== "$0.00") parts.push(`<div>Kept at a family's request<b>${round.kept}</b></div>`);
  parts.push(`<div>Refunds<b>${round.to_refund}</b><span>to ${round.refunds} families</span></div>`);
  if (round.approved) parts.push(`<div>Answered by PayPal<b>${round.states.DONE || 0} of ${round.refunds}</b></div>`);
  if (state.check) {
    parts.push(`<div class="${state.check.difference !== "$0.00" ? "off" : "zero"}">PayPal vs plan<b>${state.check.difference}</b></div>`);
  } else {
    parts.push(`<div class="${round.left_over !== "$0.00" ? "off" : "zero"}">Left over<b>${round.left_over}</b></div>`);
  }
  $("#summary").innerHTML = parts.join("");
  show("#summary");
}

function statusCell(r) {
  if (r.excluded) return `<span class="fine">${esc(r.excluded)}</span>`;
  if (r.kept) return `<span class="fine">${r.kept} kept for the club at the family's request</span>`;
  const label = { PLANNED: "Planned", SENDING: "Unknown: did PayPal pay?", DONE: "Refunded", HELD: "Held" }[r.state] || r.state;
  let html = `<span class="state ${r.state.toLowerCase()}">${label}</span>`;
  if (r.detail) html += `<br><span class="fine">${esc(r.detail)}</span>`;
  if (r.refund_id) html += `<br><code>${esc(r.refund_id)}</code>`;
  return html;
}

function renderRows(round) {
  state.round = round;
  $("#plan tbody").innerHTML = round.rows
    .map((r) => {
      const cls = r.excluded || r.kept ? "excluded" : { DONE: "sent", SENDING: "sending", HELD: "held" }[r.state] || "";
      const refund = r.excluded ? "" : r.kept ? "$0.00" : r.share;
      return `<tr class="${cls}"><td>${esc(r.family_name)}</td><td class="n">${r.paid}</td>` +
        `<td class="n math">${r.counted}</td><td class="n math">${r.excluded ? "" : exactDollars(r.exact)}</td>` +
        `<td class="n">${refund}${r.extra_cent && !r.kept ? '<span class="cent" title="got one of the leftover cents">+1¢</span>' : ""}</td>` +
        `<td>${statusCell(r)}</td></tr>`;
    })
    .join("");
  $("#math-note").textContent =
    `Counted is what a family paid minus the per-player amount the notice leaves out. Exact share is the pool times counted, ` +
    `divided by the total counted for every included family. ${round.extra_cents} families got one leftover cent each: ` +
    `the largest fractions first, ties to the earlier payment.`;
  renderSummary(round);
}

function renderReceipt(round) {
  const r = round.rows.find((x) => x.note);
  if (!r) return;
  $("#receipt").innerHTML =
    `<div class="fine">What the ${esc(r.family_name)} family sees with the PayPal refund</div>` +
    `<div class="receipt-card"><div class="receipt-amount">Refund ${r.share}</div><div>${esc(r.note)}</div></div>`;
}

function renderPlan(round) {
  renderRows(round);
  renderReceipt(round);
  show("#s-plan");
  show("#plan");
  $("#b-plan").hidden = true;
  $("#b-send").textContent = `Approve and send ${round.refunds} refunds`;
  show("#s-send");
}

async function makePlan() {
  const round = await api("POST", "/api/plan", {
    season_id: state.seasonId,
    confirmed: state.confirmed,
    ignored_replies: state.ignored,
    use_request_ids: true,
  });
  if (round.existing) say("#plan-status", `${round.existing}, so it is shown here instead of a second plan.`);
  renderPlan(round);
  afterRound(round);
}

// 5. Send

async function runUntilStopped(seasonId, roundId, crashAfter, onUpdate) {
  await api("POST", "/api/run", { season_id: seasonId, round_id: roundId, crash_after_send: crashAfter });
  for (;;) {
    await sleep(600);
    const round = await api("GET", `/api/round/${seasonId}/${roundId}`);
    onUpdate(round);
    if (round.worker !== "running") return round;
  }
}

function afterRound(round) {
  const unfinished = (round.states.PLANNED || 0) + (round.states.SENDING || 0);
  const sending = round.rows.find((r) => r.state === "SENDING");
  $("#crash-banner").hidden = !(round.approved && unfinished);
  if (sending) $("#crash-family").textContent = `the ${sending.family_name} family`;
  $("#b-send").hidden = round.approved;
  $("#crash").disabled = round.approved;
  if (round.approved && !unfinished) show("#s-check");
}

async function send(crashAfter) {
  say("#send-status", crashAfter ? "Sending..." : "Resuming...");
  const round = await runUntilStopped(state.seasonId, state.round.round_id, crashAfter, renderRows);
  const fine = round.worker === "crashed" || round.worker === "finished";
  say("#send-status", round.worker === "finished" ? "Every refund has an answer from PayPal." : fine ? "" : `The sender stopped (${round.worker}).`, !fine);
  afterRound(round);
}

// 6. Check

function renderCheck(report, el) {
  const issues = report.families.filter((f) => f.status !== "OK");
  let html =
    `<p class="verdict ${report.clean ? "ok" : "bad"}">${report.clean ? "PayPal and the ledger agree." : "PayPal and the ledger disagree."}</p>` +
    `<div class="totals"><div>Planned refunds<b>${report.to_refund}</b></div><div>PayPal refunded<b>${report.paid_out}</b></div>` +
    `<div class="${report.difference !== "$0.00" ? "off" : "zero"}">Difference<b>${report.difference}</b></div>` +
    `<div>Families matching<b>${report.matching} of ${report.families.length}</b></div></div>`;
  if (issues.length) {
    html +=
      `<table class="ledger"><thead><tr><th>Family</th><th class="n">Planned</th><th class="n">Recorded</th><th>PayPal also has</th><th>Status</th></tr></thead><tbody>` +
      issues
        .map(
          (f) =>
            `<tr class="held"><td>${esc(f.family_name)}</td><td class="n">${f.planned}</td><td class="n">${f.paypal}</td>` +
            `<td>${f.unrecorded.map((u) => `${u.amount} <code>${esc(u.refund_id)}</code>, in no record of ours`).join("<br>")}</td><td>${f.status}</td></tr>`,
        )
        .join("") +
      `</tbody></table>`;
  }
  el.innerHTML = html;
  el.hidden = false;
}

async function check() {
  say("#check-status", "Reading every order back from PayPal...");
  state.check = await api("GET", `/api/reconcile/${state.seasonId}/${state.round.round_id}`);
  say("#check-status", "");
  renderCheck(state.check, $("#check"));
  renderSummary(state.round);
  show("#s-compare");
}

// 7. Compare

async function compare() {
  const progress = $("#compare-progress");
  const { season_id } = await api("POST", "/api/season", { compare: true });
  await waitForSeason(season_id, progress);
  const round = await api("POST", "/api/plan", {
    season_id,
    confirmed: state.confirmed,
    ignored_replies: state.ignored,
    use_request_ids: false,
    copy_from: state.seasonId,
  });
  const total = round.refunds;
  const count = (r) => r.states.DONE || 0;
  await runUntilStopped(season_id, round.round_id, 9, (r) => setProgress(progress, count(r), total, `Sent ${count(r)} of ${total} without request IDs, then the same crash after #9`));
  await runUntilStopped(season_id, round.round_id, null, (r) => setProgress(progress, count(r), total, `Plain retry: ${count(r)} of ${total}`));
  progress.hidden = true;
  say("#compare-status", "Reading every order back from PayPal...");
  renderCheck(await api("GET", `/api/reconcile/${season_id}/${round.round_id}`), $("#compare"));
  say("#compare-status", "");
}

// Start, and pick up where this visitor left off

async function start() {
  state.demo = await api("GET", "/api/demo");
  $("#notice").value = state.demo.notice;
  renderInbox();
  $("#b-season").addEventListener("click", () => step("#b-season", "#season-status", openSeason));
  $("#b-read").addEventListener("click", () => step("#b-read", "#read-status", readNotice));
  $("#b-replies").addEventListener("click", () => step("#b-replies", "#replies-status", sortReplies));
  $("#b-plan").addEventListener("click", () => step("#b-plan", "#plan-status", makePlan));
  $("#b-send").addEventListener("click", () => step("#b-send", "#send-status", () => send($("#crash").checked ? 9 : null)));
  $("#b-resume").addEventListener("click", () => step("#b-resume", "#send-status", () => send(null)));
  $("#b-check").addEventListener("click", () => step("#b-check", "#check-status", check));
  $("#b-compare").addEventListener("click", () => step("#b-compare", "#compare-status", compare));
  $("#show-math").addEventListener("change", (e) => $("#plan").classList.toggle("with-math", e.target.checked));

  const me = await api("GET", "/api/me");
  if (!me.season_id) return;
  state.seasonId = me.season_id;
  renderSeason((await api("GET", `/api/season/${me.season_id}`)).payments);
  if (me.reading) {
    state.reading = me.reading;
    $("#notice").value = me.reading.notice;
    if (me.round) state.confirmed = me.round.confirmed || [];
    renderReading();
  }
  if (me.replies) {
    state.replies = me.replies;
    if (me.round) state.ignored = me.round.ignored || [];
    renderReplies();
  }
  if (me.round) {
    renderPlan(me.round);
    afterRound(me.round);
  }
}

start();
