"""Web app for Remainder. Each visitor gets their own season (own SQLite file, own sandbox payments)."""

import json
import os
import subprocess
import sys
import threading
import uuid
from datetime import date
from pathlib import Path

from fastapi import Cookie, FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from remainder import demo, notice, rules, season
from remainder.allocate import AllocationError
from remainder.ledger import Ledger
from remainder.money import display
from remainder.paypal import PayPalGateway
from remainder.reconcile import reconcile

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("REMAINDER_DATA", ROOT / "data"))
DATA.mkdir(parents=True, exist_ok=True)
DAILY_CLAUDE_CALLS = int(os.environ.get("REMAINDER_DAILY_CLAUDE_CALLS", "300"))
DAILY_SEASONS = int(os.environ.get("REMAINDER_DAILY_SEASONS", "60"))

app = FastAPI(title="Remainder")
app.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")

gateway = PayPalGateway.from_env()
seeding = {}  # season_id -> {"done": int, "total": int, "error": str}
workers = {}  # round_id -> Popen
lock = threading.Lock()


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


def _ledger(season_id):
    return Ledger(str(_db_path(season_id)))


def _seed(season_id, families):
    seeding[season_id] = {"done": 0, "total": len(families), "error": ""}
    ledger = Ledger(str(DATA / f"{season_id}.db"))

    def pay(label, cents):
        result = gateway.pay_with_test_card(label, cents)
        with lock:
            seeding[season_id]["done"] += 1
        return result

    try:
        season.seed(ledger, pay, gateway.refund_in_full, families=families, workers=6)
        ledger.set_meta("seeded", "1")
    except Exception as exc:  # shown to the visitor; the season is unusable
        seeding[season_id]["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        ledger.close()


def _payment_json(p):
    return {
        "family_id": p.family_id,
        "family_name": p.family_name,
        "players": p.players,
        "paid": display(p.paid_cents),
        "refunded_before": display(p.refunded_before_cents),
        "capture_id": p.capture_id,
    }


@app.get("/")
def index():
    return FileResponse(ROOT / "web" / "index.html")


class NewSeason(BaseModel):
    compare: bool = False


@app.post("/api/season")
def new_season(body: NewSeason, response: Response):
    if not _count_today("seasons", DAILY_SEASONS):
        raise HTTPException(429, "the demo has created as many sandbox seasons as it may today; try tomorrow")
    season_id = ("cmp-" if body.compare else "s-") + uuid.uuid4().hex[:12]
    threading.Thread(target=_seed, args=(season_id, demo.FAMILIES), daemon=True).start()
    if not body.compare:
        response.set_cookie("season", season_id, max_age=60 * 60 * 24 * 30, samesite="lax")
    return {"season_id": season_id}


@app.get("/api/season/{season_id}")
def season_status(season_id: str):
    status = seeding.get(season_id, {"done": None, "total": None, "error": ""})
    try:
        ledger = _ledger(season_id)
    except HTTPException:
        return {"season_id": season_id, "seeding": status, "complete": False, "payments": []}
    try:
        return {
            "season_id": season_id,
            "seeding": status,
            "complete": ledger.get_meta("seeded") == "1",
            "payments": [_payment_json(p) for p in ledger.payments()],
        }
    finally:
        ledger.close()


@app.get("/api/me")
def me(season: str | None = Cookie(default=None)):
    if not season:
        return {"season_id": None}
    try:
        _db_path(season)
    except HTTPException:
        return {"season_id": None}
    ledger = _ledger(season)
    try:
        if ledger.get_meta("seeded") != "1":
            return {"season_id": None}
        return {"season_id": season, "round_id": ledger.latest_round()}
    finally:
        ledger.close()


class ReadRequest(BaseModel):
    season_id: str
    notice: str


def _problems_json(problems):
    return [{"field": p.field, "message": p.message} for p in problems]


@app.post("/api/read")
def read_notice(body: ReadRequest):
    ledger = _ledger(body.season_id)
    try:
        payments = ledger.payments()
    finally:
        ledger.close()
    names = [p.family_name for p in payments]
    use_claude = notice.saved_reading_for(body.notice) is None or os.environ.get("REMAINDER_LIVE_DEMO_READ") == "1"
    if use_claude and not _count_today("claude", DAILY_CLAUDE_CALLS):
        raise HTTPException(429, "the demo has used its Claude calls for today; the saved reading still works for the sample notice")
    reading = notice.read(body.notice, names) if use_claude else notice.saved_reading_for(body.notice)
    if reading.rules is None:
        raise HTTPException(502, reading.error)
    return {
        "source": reading.source,
        "model": reading.model,
        "rules": reading.rules.to_json(),
        "problems": _problems_json(rules.check(reading.rules, body.notice, payments)),
    }


class PlanRequest(BaseModel):
    season_id: str
    notice: str
    title: str
    rules: dict
    answered_questions: list[str] = []
    use_request_ids: bool = True


@app.post("/api/plan")
def make_plan(body: PlanRequest):
    ledger = _ledger(body.season_id)
    try:
        r = rules.Rules.from_json(body.rules)
        payments = ledger.payments()
        problems = rules.check(r, body.notice, payments)
        open_problems = [p for p in problems if not (p.field == "question" and p.message in body.answered_questions)]
        if open_problems:
            raise HTTPException(422, {"problems": _problems_json(open_problems)})
        try:
            lines = rules.plan(r, payments, body.title)
        except AllocationError as exc:
            raise HTTPException(422, {"problems": [{"field": "pool", "message": str(exc)}]})
        record = {**r.to_json(), "answered_questions": body.answered_questions}
        round_id = ledger.create_round(body.title, body.notice, record, r.pool_cents, lines, body.use_request_ids)
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
    return {0: "finished", 70: "crashed"}.get(code, f"exited {code}")


def _round_json(ledger, round_id):
    rnd = ledger.round(round_id)
    rows = ledger.rows(round_id)
    return {
        "round_id": round_id,
        "title": rnd.title,
        "pool": display(rnd.pool_cents),
        "planned_total": display(sum(r.share_cents for r in rows)),
        "use_request_ids": rnd.use_request_ids,
        "approved": rnd.approved_at is not None,
        "worker": _worker_state(round_id),
        "rows": [
            {
                "family_id": r.family_id,
                "family_name": r.family_name,
                "paid": display(r.paid_cents),
                "counted": display(r.weight_cents),
                "exact": r.exact,
                "share": display(r.share_cents),
                "extra_cent": r.extra_cent,
                "excluded": r.excluded,
                "note": r.note,
                "request_id": r.request_id,
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
    crash_after_send: int | None = None


@app.post("/api/run")
def run_round(body: RunRequest):
    path = _db_path(body.season_id)
    if _worker_state(body.round_id) == "running":
        raise HTTPException(409, "already sending")
    ledger = Ledger(str(path))
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
    log = open(DATA / f"{body.round_id}.log", "a")
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
    return {
        "pool": display(report.pool_cents),
        "paid_out": display(report.paid_out_cents),
        "difference": ("+" if report.difference_cents > 0 else "") + display(report.difference_cents),
        "clean": report.clean,
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


@app.get("/api/demo")
def demo_text():
    return {"notice": demo.NOTICE, "title": demo.TITLE}
