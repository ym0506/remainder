"""SQLite ledger: who paid, what each refund round plans, and every call made to PayPal.

A plan row moves PLANNED -> SENDING -> DONE or HELD. SENDING is written and committed
before PayPal is called, so after a crash we know which refunds may already exist at
PayPal and must be sent again with the same request ID, never a new one.
"""

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from .rules import Payment

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
    note: str
    request_id: str
    state: str
    paypal_refund_id: str | None
    replayed: bool
    detail: str
    paid_at: str


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
            " p.order_id, p.capture_id, p.paid_at FROM payments p ORDER BY p.paid_at, p.family_id"
        ).fetchall()
        return [Payment(**dict(r)) for r in rows]

    # rounds

    def create_round(self, title, notice, rules_json, pool_cents, lines, use_request_ids=True):
        if sum(line.share_cents for line in lines) != pool_cents:
            raise ValueError("plan does not add up to the pool")
        round_id = "r-" + uuid.uuid4().hex[:12]
        captures = {p.family_id: p.capture_id for p in self.payments()}
        stamp = now()
        with self.db:
            self.db.execute("BEGIN")
            self.db.execute(
                "INSERT INTO rounds VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
                (round_id, title, notice, json.dumps(rules_json), pool_cents, int(use_request_ids), stamp),
            )
            for line in lines:
                self.db.execute(
                    "INSERT INTO plan_rows (round_id, family_id, share_cents, weight_cents, exact, extra_cent, excluded,"
                    " note, request_id, state, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        round_id,
                        line.family_id,
                        line.share_cents,
                        line.weight_cents,
                        line.exact,
                        int(line.extra_cent),
                        line.excluded,
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
        sql += " ORDER BY p.paid_at, pr.family_id"
        out = []
        for r in self.db.execute(sql, args).fetchall():
            d = dict(r)
            d.pop("updated_at")
            d["extra_cent"], d["replayed"] = bool(d["extra_cent"]), bool(d["replayed"])
            out.append(Row(**d))
        return out

    # sending

    def mark_sending(self, round_id, family_id):
        self.db.execute(
            "UPDATE plan_rows SET state = 'SENDING', updated_at = ? WHERE round_id = ? AND family_id = ? AND state IN ('PLANNED', 'SENDING')",
            (now(), round_id, family_id),
        )

    def record(self, round_id, family_id, request_id, outcome):
        """Write down what PayPal answered. Unknown outcomes stay SENDING, to be sent again."""
        if outcome.http_status in (200, 201):
            state, detail = "DONE", ("PayPal returned the refund it already made" if outcome.http_status == 200 else "")
        elif outcome.http_status is None or outcome.http_status >= 500:
            state, detail = "SENDING", f"no clear answer ({outcome.error or outcome.http_status}); will send again with the same request ID"
        else:
            state, detail = "HELD", outcome.error or f"HTTP {outcome.http_status}"
        with self.db:
            self.db.execute("BEGIN")
            self.db.execute(
                "INSERT INTO attempts (round_id, family_id, request_id, sent_at, http_status, paypal_refund_id, error)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (round_id, family_id, request_id, now(), outcome.http_status, outcome.refund_id, outcome.error),
            )
            self.db.execute(
                "UPDATE plan_rows SET state = ?, paypal_refund_id = COALESCE(?, paypal_refund_id), replayed = ?,"
                " detail = ?, updated_at = ? WHERE round_id = ? AND family_id = ?",
                (state, outcome.refund_id, int(outcome.http_status == 200), detail, now(), round_id, family_id),
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
