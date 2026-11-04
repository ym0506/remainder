"""Web app for Remainder. Each visitor gets their own season (own SQLite file, own sandbox payments).

What Claude read is stored on the server with the season, and the plan is built from that.
For a plan the browser only says which questions the treasurer confirmed and which "keep
our share" replies they turned off. Whether request IDs are used is decided here: always
for a visitor's season, never for a comparison season, which may copy its readings only
from another season.
"""

import json
import os
import subprocess
import sys
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import Cookie, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from remainder import demo, notice, replies, rules, season
from remainder.allocate import AllocationError
from remainder.ledger import Ledger, RoundExists
from remainder.money import display
from remainder.paypal import PayPalGateway
from remainder.reconcile import reconcile
from remainder.runner import BUSY_EXIT, CRASH_EXIT

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("REMAINDER_DATA", ROOT / "data"))
DATA.mkdir(parents=True, exist_ok=True)
DAILY_CLAUDE_CALLS = int(os.environ.get("REMAINDER_DAILY_CLAUDE_CALLS", "300"))
DAILY_SEASONS = int(os.environ.get("REMAINDER_DAILY_SEASONS", "60"))
SPARE_SEASONS = int(os.environ.get("REMAINDER_SPARE_SEASONS", "1"))
MAX_ADDED_REPLIES = 4  # replies a visitor may write into a season's inbox

gateway = PayPalGateway.from_env()
seeding = {}  # season_id -> {"done": int, "total": int, "error": str}
workers = {}  # round_id -> Popen
spares = []  # seeded seasons nobody has taken yet
spares_pending = [0]  # spare seasons still being created
lock = threading.Lock()


@asynccontextmanager
async def lifespan(_app):
    threading.Thread(target=_top_up_spares, daemon=True).start()
    yield


app = FastAPI(title="Remainder", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")


def _count_today(kind, limit):
    path = DATA / f"usage-{date.today().isoformat()}.json"
    with lock:
        usage = json.loads(path.read_text()) if path.exists() else {}
        if usage.get(kind, 0) >= limit:
            return False
        usage[kind] = usage.get(kind, 0) + 1
        path.write_text(json.dumps(usage))
    return True


def _db_path(season_id):
    if not season_id or not season_id.replace("-", "").isalnum():
        raise HTTPException(404, "no season yet")
    path = DATA / f"{season_id}.db"
    if not path.exists():
        raise HTTPException(404, "no season yet")
    return path


def _ledger(season_id, seeded=True):
    ledger = Ledger(str(_db_path(season_id)))
    if seeded and ledger.get_meta("seeded") != "1":
        ledger.close()
        raise HTTPException(409, "this season's payments are still being created")
    return ledger


def _seed(season_id, spare=False):
    seeding[season_id] = {"done": 0, "total": len(demo.FAMILIES), "error": ""}
    ledger = None

    def pay(label, cents):
        result = gateway.pay_with_test_card(label, cents)
        with lock:
            seeding[season_id]["done"] += 1
        return result

    try:
        ledger = Ledger(str(DATA / f"{season_id}.db"))
        season.seed(ledger, pay, gateway.refund_in_full, workers=6)
        ledger.set_meta("seeded", "1")
        if spare:
            with lock:
                spares.append(season_id)
    except Exception as exc:  # shown to the visitor; the season is unusable
        seeding[season_id]["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if ledger is not None:
            ledger.close()
        if spare:
            with lock:
                spares_pending[0] -= 1


def _start_seeding(prefix, spare=False):
    season_id = prefix + uuid.uuid4().hex[:12]
    threading.Thread(target=_seed, args=(season_id, spare), daemon=True).start()
    return season_id


def _top_up_spares():
    """Keep a season ready so a visitor doesn't wait for 24 sandbox payments."""
    with lock:
        missing = SPARE_SEASONS - len(spares) - spares_pending[0]
        spares_pending[0] += max(missing, 0)
    for _ in range(max(missing, 0)):
        if _count_today("seasons", DAILY_SEASONS):
            _start_seeding("s-", spare=True)
        else:
            with lock:
                spares_pending[0] -= 1


def _inbox(ledger):
    """The season's replies: the demo's six, plus any the visitor wrote."""
    stored = json.loads(ledger.get_meta("inbox") or "null")
    return [tuple(x) for x in stored] if stored else list(demo.REPLIES)


def _inbox_json(inbox):
    return [
        {"reply_id": i, "family": f, "text": t, "added": i >= len(demo.REPLIES)} for i, (f, t) in enumerate(inbox)
    ]


def _payment_json(p):
    return {
        "family_name": p.family_name,
        "players": p.players,
        "paid": display(p.paid_cents),
        "refunded_before": display(p.refunded_before_cents),
        "capture_id": p.capture_id,
    }


@app.get("/")
def index():
    return FileResponse(ROOT / "web" / "index.html")


@app.get("/api/demo")
def demo_text():
    return {
        "notice": demo.NOTICE,
        "title": demo.TITLE,
        "replies": _inbox_json(demo.REPLIES),
    }


class NewSeason(BaseModel):
    compare: bool = False


@app.post("/api/season")
def new_season(body: NewSeason, request: Request, response: Response):
    season_id = None
    if not body.compare:
        with lock:
            season_id = spares.pop(0) if spares else None
    if season_id is None:
        if not _count_today("seasons", DAILY_SEASONS):
            raise HTTPException(429, "the demo has created as many sandbox seasons as it may today; try tomorrow")
        season_id = _start_seeding("cmp-" if body.compare else "s-")
    if not body.compare:
        secure = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
        response.set_cookie("season", season_id, max_age=60 * 60 * 24 * 30, samesite="lax", httponly=True, secure=secure)
        threading.Thread(target=_top_up_spares, daemon=True).start()
    return {"season_id": season_id}


@app.get("/api/season/{season_id}")
def season_status(season_id: str):
    status = seeding.get(season_id, {"done": None, "total": None, "error": ""})
    try:
        ledger = _ledger(season_id, seeded=False)
    except HTTPException:
        if status["done"] is None:
            status = {**status, "error": "this season no longer exists; start a new one"}
        return {"season_id": season_id, "seeding": status, "complete": False, "payments": []}
    try:
        complete = ledger.get_meta("seeded") == "1"
        if not complete and status["done"] is None:
            status = {**status, "error": "this season was interrupted; start a new one"}
        return {
            "season_id": season_id,
            "seeding": status,
            "complete": complete,
            "payments": [_payment_json(p) for p in ledger.payments()] if complete else [],
        }
    finally:
        ledger.close()


@app.get("/api/me")
def me(season: str | None = Cookie(default=None)):
    try:
        ledger = _ledger(season)
    except HTTPException:
        return {"season_id": None}
    try:
        round_id = ledger.latest_round()
        reading = json.loads(ledger.get_meta("reading") or "null")
        stored_replies = json.loads(ledger.get_meta("replies") or "null")
        if reading:
            r = rules.Rules.from_json(reading["rules"])
            reading["problems"] = _problems_json(rules.check(r, reading["notice"], ledger.payments()))
        if reading and stored_replies:
            sorted_replies = [replies.from_json(d) for d in stored_replies["results"]]
            stored_replies["problems"] = _problems_json(
                replies.check(sorted_replies, _inbox(ledger), reading["notice"], _plan_families(ledger, reading))
            )
        return {
            "season_id": season,
            "roster": [p.family_name for p in ledger.payments()],
            "inbox": _inbox_json(_inbox(ledger)),
            "reading": reading,
            "replies": stored_replies,
            "round": _round_json(ledger, round_id) if round_id else None,
        }
    finally:
        ledger.close()


def _problems_json(problems):
    return [{"field": p.field, "message": p.message, "confirmable": p.confirmable} for p in problems]


def _claude_allowed():
    return notice._client() is not None and _count_today("claude", DAILY_CLAUDE_CALLS)


class ReadRequest(BaseModel):
    season_id: str
    notice: str = Field(max_length=5000)


def _refuse_if_approved(ledger):
    for r in ledger.db.execute("SELECT approved_at FROM rounds").fetchall():
        if r["approved_at"]:
            raise HTTPException(409, "this season's refunds are already approved; start a new season to try another notice")


@app.post("/api/read")
def read_notice(body: ReadRequest):
    ledger = _ledger(body.season_id)
    try:
        _refuse_if_approved(ledger)
        payments = ledger.payments()
        saved = notice.saved_reading_for(body.notice)
        reading = notice.read_with_claude(body.notice, [p.family_name for p in payments]) if _claude_allowed() else None
        if reading is None or reading.rules is None:
            if saved is None:
                raise HTTPException(503, reading.error if reading else "Claude is not available for this demo right now")
            reading = saved
        stored = {"notice": body.notice, "rules": reading.rules.to_json(), "source": reading.source, "model": reading.model}
        ledger.set_meta("reading", json.dumps(stored))
        ledger.set_meta("replies", "null")
        return {**stored, "problems": _problems_json(rules.check(reading.rules, body.notice, payments))}
    finally:
        ledger.close()


class RepliesRequest(BaseModel):
    season_id: str


def _plan_families(ledger, stored):
    r = rules.Rules.from_json(stored["rules"])
    excluded = {rules._norm(e.family) for e in r.exclusions}
    return [p.family_name for p in ledger.payments() if rules._norm(p.family_name) not in excluded]


@app.post("/api/replies")
def sort_replies(body: RepliesRequest):
    ledger = _ledger(body.season_id)
    try:
        _refuse_if_approved(ledger)
        stored = json.loads(ledger.get_meta("reading") or "null")
        if not stored:
            raise HTTPException(409, "read the notice first")
        inbox = _inbox(ledger)
        results, source, model, error = None, "claude", notice.MODEL, ""
        if _claude_allowed():
            results, error = replies.read_with_claude(inbox, stored["notice"])
        if results is None:
            saved = replies.saved_for(inbox, stored["notice"])
            if saved is None:
                raise HTTPException(503, error or "Claude is not available for this demo right now")
            (results, model), source = saved, "saved"
        out = {"results": [replies.to_json(s) for s in results], "source": source, "model": model}
        ledger.set_meta("replies", json.dumps(out))
        problems = replies.check(results, inbox, stored["notice"], _plan_families(ledger, stored))
        return {**out, "problems": _problems_json(problems)}
    finally:
        ledger.close()


class AddReply(BaseModel):
    season_id: str
    family: str = Field(max_length=60)
    text: str = Field(min_length=1, max_length=600)


@app.post("/api/inbox")
def add_reply(body: AddReply):
    """A visitor writes a reply from a family. It changes nothing until Claude sorts it and the checks pass."""
    ledger = _ledger(body.season_id)
    try:
        _refuse_if_approved(ledger)
        if body.family not in {p.family_name for p in ledger.payments()}:
            raise HTTPException(422, "pick a family from the roster")
        inbox = _inbox(ledger)
        if len(inbox) - len(demo.REPLIES) >= MAX_ADDED_REPLIES:
            raise HTTPException(429, f"up to {MAX_ADDED_REPLIES} replies of your own per season; start a new season for more")
        inbox.append((body.family, body.text.strip()))
        ledger.set_meta("inbox", json.dumps([list(x) for x in inbox]))
        ledger.set_meta("replies", "null")
        return {"inbox": _inbox_json(inbox)}
    finally:
        ledger.close()


class PlanRequest(BaseModel):
    season_id: str
    confirmed: list[str] = []
    ignored_replies: list[int] = []
    copy_from: str | None = None  # the comparison season reuses the main season's readings


@app.post("/api/plan")
def make_plan(body: PlanRequest):
    comparison = body.season_id.startswith("cmp-")
    if comparison != bool(body.copy_from):
        raise HTTPException(422, "only a comparison season copies readings, and it must")
    source = _ledger(body.copy_from or body.season_id)
    try:
        stored = json.loads(source.get_meta("reading") or "null")
        stored_replies = json.loads(source.get_meta("replies") or "null")
        inbox = _inbox(source)
    finally:
        source.close()
    if not stored or not stored_replies:
        raise HTTPException(409, "read the notice and the replies first")

    ledger = _ledger(body.season_id)
    try:
        r = rules.Rules.from_json(stored["rules"])
        payments = ledger.payments()
        sorted_replies = [replies.from_json(d) for d in stored_replies["results"]]
        active = [s for s in sorted_replies if s.reply_id not in body.ignored_replies]
        problems = rules.check(r, stored["notice"], payments) + replies.check(
            active, inbox, stored["notice"], _plan_families(ledger, stored)
        )
        still_open = rules.open_problems(problems, set(body.confirmed))
        if still_open:
            raise HTTPException(422, {"problems": _problems_json(still_open)})
        try:
            lines = rules.plan(r, payments, demo.TITLE, keep=replies.keep_list(active))
        except AllocationError as exc:
            raise HTTPException(422, {"problems": [{"field": "pool", "message": str(exc), "confirmable": False}]})
        record = {
            **r.to_json(),
            "confirmed": body.confirmed,
            "replies": [replies.to_json(s) for s in active],
            "inbox_size": len(inbox),
        }
        try:
            round_id = ledger.create_round(demo.TITLE, stored["notice"], record, r.pool_cents, lines, not comparison)
        except RoundExists as exc:
            return {**_round_json(ledger, exc.round_id), "existing": str(exc)}
        return _round_json(ledger, round_id)
    finally:
        ledger.close()


def _worker_state(round_id):
    proc = workers.get(round_id)
    if proc is None:
        return "idle"
    code = proc.poll()
    if code is None:
        return "running"
    return {0: "finished", CRASH_EXIT: "crashed", BUSY_EXIT: "busy"}.get(code, f"exited {code}")


def _round_json(ledger, round_id):
    rnd = ledger.round(round_id)
    rows = ledger.rows(round_id)
    record = json.loads(rnd.rules_json)
    active = {d["reply_id"] for d in record.get("replies", [])}
    kept = sum(r.kept_cents for r in rows)
    states = {}
    for r in rows:
        states[r.state] = states.get(r.state, 0) + 1
    return {
        "round_id": round_id,
        "pool": display(rnd.pool_cents),
        "kept": display(kept),
        "to_refund": display(rnd.pool_cents - kept),
        "planned_total": display(sum(r.share_cents for r in rows)),
        "left_over": display(rnd.pool_cents - kept - sum(r.share_cents for r in rows)),
        "refunds": sum(1 for r in rows if r.share_cents),
        "extra_cents": sum(1 for r in rows if r.extra_cent),
        "states": states,
        "confirmed": record.get("confirmed", []),
        "ignored": [i for i in range(record.get("inbox_size", len(demo.REPLIES))) if i not in active],
        "use_request_ids": rnd.use_request_ids,
        "approved": rnd.approved_at is not None,
        "worker": _worker_state(round_id),
        "rows": [
            {
                "family_name": r.family_name,
                "paid": display(r.paid_cents),
                "counted": display(r.weight_cents),
                "exact": r.exact,
                "share": display(r.share_cents),
                "kept": display(r.kept_cents) if r.kept_cents else "",
                "extra_cent": r.extra_cent,
                "excluded": r.excluded,
                "note": r.note,
                "state": r.state,
                "refund_id": r.paypal_refund_id,
                "replayed": r.replayed,
                "detail": r.detail,
            }
            for r in rows
        ],
    }


@app.get("/api/round/{season_id}/{round_id}")
def get_round(season_id: str, round_id: str):
    ledger = _ledger(season_id)
    try:
        return _round_json(ledger, round_id)
    except KeyError:
        raise HTTPException(404, "no such round")
    finally:
        ledger.close()


class RunRequest(BaseModel):
    season_id: str
    round_id: str
    crash_after_send: int | None = Field(default=None, ge=1)


@app.post("/api/run")
def run_round(body: RunRequest):
    ledger = _ledger(body.season_id)
    path = _db_path(body.season_id)
    with lock:
        if _worker_state(body.round_id) == "running":
            ledger.close()
            raise HTTPException(409, "already sending")
        try:
            ledger.round(body.round_id)
            ledger.approve(body.round_id)
        except KeyError:
            raise HTTPException(404, "no such round")
        finally:
            ledger.close()
        cmd = [sys.executable, "-m", "remainder.runner", "--db", str(path), "--round", body.round_id]
        if body.crash_after_send:
            cmd += ["--crash-after-send", str(body.crash_after_send)]
        with open(DATA / f"{body.round_id}.log", "a") as log:
            workers[body.round_id] = subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    return {"worker": "running"}


@app.get("/api/reconcile/{season_id}/{round_id}")
def check_against_paypal(season_id: str, round_id: str):
    ledger = _ledger(season_id)
    try:
        report = reconcile(ledger, gateway, round_id)
    except KeyError:
        raise HTTPException(404, "no such round")
    finally:
        ledger.close()
    diff = report.difference_cents
    return {
        "to_refund": display(report.to_refund_cents),
        "kept": display(report.kept_cents),
        "paid_out": display(report.paid_out_cents),
        "difference": ("+" if diff > 0 else "") + display(diff),
        "clean": report.clean,
        "matching": sum(1 for f in report.families if f.status == "OK"),
        "families": [
            {
                "family_name": f.family_name,
                "planned": display(f.planned_cents),
                "paypal": display(f.paypal_cents),
                "unrecorded": [{"refund_id": rid, "amount": display(c)} for rid, c in f.unrecorded],
                "status": f.status,
            }
            for f in report.families
        ],
    }
