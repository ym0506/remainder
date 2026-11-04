"""SQLite ledger: who paid, what each refund round plans, and every PayPal answer that was saved.

A plan row with nothing to send is SKIPPED; the others move PLANNED -> SENDING -> DONE or
HELD. SENDING is written and committed before PayPal is called, so after a crash we know
which refunds may already exist at PayPal: the sender looks those up on the order first
and sends again only with the same request ID, never a new one.
"""

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from .rules import Payment, _norm

# Fixed namespace so a (round, capture) pair always maps to the same PayPal-Request-Id.
REQUEST_ID_NAMESPACE = uuid.UUID("6f1c2f0e-5b2a-4a51-9a0f-2f7c1d3e8b40")

SCHEMA = """
CREATE TABLE IF NOT EXISTS payments (
    family_id TEXT PRIMARY KEY,
    family_name TEXT NOT NULL,
    players INTEGER NOT NULL CHECK (players > 0),
    paid_cents INTEGER NOT NULL CHECK (paid_cents > 0),
    refunded_before_cents INTEGER NOT NULL DEFAULT 0 CHECK (refunded_before_cents >= 0),
    order_id TEXT NOT NULL,
    capture_id TEXT NOT NULL UNIQUE,
    paid_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prior_refunds (
    refund_id TEXT PRIMARY KEY,
    capture_id TEXT NOT NULL REFERENCES payments (capture_id),
    cents INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS rounds (
    round_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    notice TEXT NOT NULL,
    rules_json TEXT NOT NULL,
    pool_cents INTEGER NOT NULL,
    use_request_ids INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    approved_at TEXT
);
CREATE TABLE IF NOT EXISTS plan_rows (
    round_id TEXT NOT NULL REFERENCES rounds,
    family_id TEXT NOT NULL REFERENCES payments,
    share_cents INTEGER NOT NULL CHECK (share_cents >= 0),
    weight_cents INTEGER NOT NULL,
    exact TEXT NOT NULL,
    extra_cent INTEGER NOT NULL,
    excluded TEXT NOT NULL,
    kept_cents INTEGER NOT NULL DEFAULT 0 CHECK (kept_cents >= 0),
    note TEXT NOT NULL,
    request_id TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK (state IN ('PLANNED', 'SENDING', 'DONE', 'HELD', 'SKIPPED')),
    paypal_refund_id TEXT,
    replayed INTEGER NOT NULL DEFAULT 0,
    detail TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (round_id, family_id)
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (
    attempt_id INTEGER PRIMARY KEY,
    round_id TEXT NOT NULL,
    family_id TEXT NOT NULL,
    request_id TEXT,
    sent_at TEXT NOT NULL,
    http_status INTEGER,
    paypal_refund_id TEXT,
    error TEXT NOT NULL DEFAULT ''
);
"""


@dataclass(frozen=True)
class Round:
    round_id: str
    title: str
    notice: str
    rules_json: str
    pool_cents: int
    use_request_ids: bool
    created_at: str
    approved_at: str | None


@dataclass(frozen=True)
class Row:
    round_id: str
    family_id: str
    family_name: str
    capture_id: str
    order_id: str
    paid_cents: int
    share_cents: int
    weight_cents: int
    exact: str
    extra_cent: bool
    excluded: str
    kept_cents: int
    note: str
    request_id: str
    state: str
    paypal_refund_id: str | None
    replayed: bool
    detail: str
    paid_at: str


# Answers that mean "not now" rather than "no": the refund may or may not exist yet.
RETRY_LATER = {408, 409, 425, 429}


class RoundExists(Exception):
    def __init__(self, round_id, reason):
        super().__init__(reason)
        self.round_id = round_id


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def request_id_for(round_id, capture_id):
    return str(uuid.uuid5(REQUEST_ID_NAMESPACE, f"{round_id}:{capture_id}"))


class Ledger:
    def __init__(self, path):
        self.db = sqlite3.connect(path, isolation_level=None, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    def set_meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))

    def get_meta(self, key):
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    # payments

    def add_payment(self, p):
        self.db.execute(
            "INSERT INTO payments VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (p.family_id, p.family_name, p.players, p.paid_cents, p.refunded_before_cents, p.order_id, p.capture_id, p.paid_at),
        )

    def add_prior_refund(self, capture_id, refund_id, cents):
        with self.db:
            self.db.execute("BEGIN")
            self.db.execute("INSERT INTO prior_refunds VALUES (?, ?, ?)", (refund_id, capture_id, cents))
            self.db.execute(
                "UPDATE payments SET refunded_before_cents = refunded_before_cents + ? WHERE capture_id = ?",
                (cents, capture_id),
            )

    def payments(self):
        """Payments with refunded_before_cents counting earlier refunds and refunds sent in any round."""
        rows = self.db.execute(
            "SELECT p.family_id, p.family_name, p.players, p.paid_cents,"
            " p.refunded_before_cents + COALESCE((SELECT SUM(share_cents) FROM plan_rows pr"
            "   WHERE pr.family_id = p.family_id AND pr.state IN ('DONE', 'SENDING')), 0) AS refunded_before_cents,"
            " p.order_id, p.capture_id, p.paid_at FROM payments p ORDER BY p.family_id"
        ).fetchall()
        return [Payment(**dict(r)) for r in rows]

    # rounds

    def create_round(self, title, notice, rules_json, pool_cents, lines, use_request_ids=True):
        """Save a plan, checked and written in one write transaction.

        A plan nobody has approved and nothing has been sent for is replaced. Otherwise a
        second plan for the same notice is refused, and so is any new plan while another
        round still has refunds waiting to go out.
        """
        if sum(line.share_cents + line.kept_cents for line in lines) != pool_cents:
            raise ValueError("plan does not add up to the pool")
        round_id = "r-" + uuid.uuid4().hex[:12]
        stamp = now()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            for r in self.db.execute("SELECT round_id, notice, approved_at FROM rounds ORDER BY created_at").fetchall():
                touched = self.rows(r["round_id"], states=("SENDING", "DONE", "HELD"))
                if r["approved_at"] is None and not touched:
                    self.db.execute("DELETE FROM plan_rows WHERE round_id = ?", (r["round_id"],))
                    self.db.execute("DELETE FROM rounds WHERE round_id = ?", (r["round_id"],))
                elif _norm(r["notice"]) == _norm(notice):
                    raise RoundExists(r["round_id"], "a refund round for this notice already exists")
                elif self.rows(r["round_id"], states=("PLANNED", "SENDING")):
                    raise RoundExists(r["round_id"], "another refund round still has refunds to send")
            captures = {p.family_id: p.capture_id for p in self.payments()}
            self.db.execute(
                "INSERT INTO rounds VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
                (round_id, title, notice, json.dumps(rules_json), pool_cents, int(use_request_ids), stamp),
            )
            for line in lines:
                self.db.execute(
                    "INSERT INTO plan_rows (round_id, family_id, share_cents, weight_cents, exact, extra_cent, excluded,"
                    " kept_cents, note, request_id, state, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        round_id,
                        line.family_id,
                        line.share_cents,
                        line.weight_cents,
                        line.exact,
                        int(line.extra_cent),
                        line.excluded,
                        line.kept_cents,
                        line.note,
                        request_id_for(round_id, captures[line.family_id]),
                        "PLANNED" if line.share_cents else "SKIPPED",
                        stamp,
                    ),
                )
        return round_id

    def approve(self, round_id):
        self.db.execute("UPDATE rounds SET approved_at = ? WHERE round_id = ? AND approved_at IS NULL", (now(), round_id))

    def round(self, round_id):
        r = self.db.execute("SELECT * FROM rounds WHERE round_id = ?", (round_id,)).fetchone()
        if r is None:
            raise KeyError(round_id)
        return Round(**{**dict(r), "use_request_ids": bool(r["use_request_ids"])})

    def latest_round(self):
        r = self.db.execute("SELECT round_id FROM rounds ORDER BY created_at DESC LIMIT 1").fetchone()
        return r["round_id"] if r else None

    def rows(self, round_id, states=None):
        sql = (
            "SELECT pr.*, p.family_name, p.capture_id, p.order_id, p.paid_cents, p.paid_at"
            " FROM plan_rows pr JOIN payments p USING (family_id) WHERE pr.round_id = ?"
        )
        args = [round_id]
        if states:
            sql += f" AND pr.state IN ({','.join('?' * len(states))})"
            args += list(states)
        sql += " ORDER BY pr.family_id"
        out = []
        for r in self.db.execute(sql, args).fetchall():
            d = dict(r)
            d.pop("updated_at")
            d["extra_cent"], d["replayed"] = bool(d["extra_cent"]), bool(d["replayed"])
            out.append(Row(**d))
        return out

    # sending

    def mark_sending(self, round_id, family_id):
        """False if the row is no longer waiting to be sent (another runner finished it)."""
        cur = self.db.execute(
            "UPDATE plan_rows SET state = 'SENDING', updated_at = ? WHERE round_id = ? AND family_id = ? AND state IN ('PLANNED', 'SENDING')",
            (now(), round_id, family_id),
        )
        return cur.rowcount == 1

    def record(self, round_id, family_id, request_id, outcome, found=False):
        """Write down what PayPal answered. Only a SENDING row changes; DONE is never undone.

        Unclear answers (no answer, 5xx, 408/409/425/429) leave the row SENDING, so the next
        run looks it up at PayPal and, if it isn't there, sends it again with the same ID.
        """
        status = outcome.http_status
        if (found or status in (200, 201)) and outcome.refund_status in ("FAILED", "CANCELLED"):
            state, detail = "HELD", f"PayPal refund {outcome.refund_status.lower()}"
        elif found:
            state, detail = "DONE", "PayPal already had this refund; it was not sent again"
        elif status in (200, 201):
            state, detail = "DONE", ("PayPal returned the refund it already made" if status == 200 else "")
        elif status is None or status >= 500 or status in RETRY_LATER:
            state, detail = "SENDING", f"no clear answer ({outcome.error or status}); will check PayPal and resend with the same request ID"
        else:
            state, detail = "HELD", outcome.error or f"HTTP {status}"
        with self.db:
            self.db.execute("BEGIN")
            self.db.execute(
                "INSERT INTO attempts (round_id, family_id, request_id, sent_at, http_status, paypal_refund_id, error)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (round_id, family_id, request_id, now(), outcome.http_status, outcome.refund_id, outcome.error),
            )
            self.db.execute(
                "UPDATE plan_rows SET state = ?, paypal_refund_id = COALESCE(?, paypal_refund_id), replayed = ?,"
                " detail = ?, updated_at = ? WHERE round_id = ? AND family_id = ? AND state = 'SENDING'",
                (state, outcome.refund_id, int(status == 200 or found), detail, now(), round_id, family_id),
            )

    # reconciling

    def known_refund_ids(self, capture_id):
        rows = self.db.execute(
            "SELECT refund_id FROM prior_refunds WHERE capture_id = ?"
            " UNION SELECT a.paypal_refund_id FROM attempts a JOIN payments p USING (family_id)"
            " WHERE p.capture_id = ? AND a.paypal_refund_id IS NOT NULL",
            (capture_id, capture_id),
        ).fetchall()
        return {r[0] for r in rows}

    def refund_ids_for(self, round_id, family_id):
        rows = self.db.execute(
            "SELECT DISTINCT paypal_refund_id FROM attempts WHERE round_id = ? AND family_id = ? AND paypal_refund_id IS NOT NULL",
            (round_id, family_id),
        ).fetchall()
        return {r[0] for r in rows}

    def attempts(self, round_id):
        return [dict(r) for r in self.db.execute("SELECT * FROM attempts WHERE round_id = ? ORDER BY attempt_id", (round_id,))]
