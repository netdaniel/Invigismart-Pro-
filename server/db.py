"""SQLite persistence layer. Standard library only.

This is the source of truth for accounts and subscriptions. Because the server
is multi-threaded we keep one connection per thread and serialise writes with a
lock, so the browser can never hold the authoritative record.
"""
import json
import os
import sqlite3
import threading
import time
import uuid

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id            TEXT PRIMARY KEY,
  school        TEXT NOT NULL,
  email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
  pw_hash       TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  trial_ends_at TEXT
);
CREATE TABLE IF NOT EXISTS subscriptions (
  user_id                TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  plan                   TEXT NOT NULL,
  status                 TEXT NOT NULL,
  current_period_end     TEXT,
  stripe_customer_id     TEXT,
  stripe_subscription_id TEXT,
  updated_at             TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  stripe_event_id TEXT UNIQUE,
  type            TEXT NOT NULL,
  payload         TEXT NOT NULL,
  created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
  id                       INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id                  TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  provider                 TEXT NOT NULL,
  provider_payment_id      TEXT UNIQUE,
  provider_subscription_id TEXT,
  amount                   TEXT,
  currency                 TEXT,
  status                   TEXT NOT NULL,
  item_name                TEXT,
  email                    TEXT,
  period_end               TEXT,
  created_at               TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
  id         TEXT PRIMARY KEY,
  user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  filename   TEXT NOT NULL,
  title      TEXT NOT NULL,
  category   TEXT NOT NULL,
  size       INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_subs_customer ON subscriptions(stripe_customer_id);
CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(user_id);
CREATE INDEX IF NOT EXISTS idx_documents_user ON documents(user_id, created_at);
"""


def utcnow_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Store:
    def __init__(self, db_path):
        self.db_path = db_path
        # SQLite cannot create a file in a directory that does not exist, and the
        # two places this runs unattended - a container volume and cPanel's
        # Passenger app - both start with an empty data directory. Failing here
        # would mean "cannot import application" and a 500 on every URL.
        parent = os.path.dirname(os.path.abspath(db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self._conn().executescript(SCHEMA)
        self._migrate()

    # -- schema migration ----------------------------------------------------
    def _migrate(self):
        """Add columns that appeared after the first release.

        CREATE TABLE IF NOT EXISTS cannot add a column to a table that already
        exists, so a database created by an older build has to be upgraded in
        place - otherwise the first paying school's renewal would fail.
        """
        self._ensure_column("subscriptions", "provider", "TEXT")

    def _ensure_column(self, table, column, declaration):
        existing = [row[1] for row in self._conn().execute("PRAGMA table_info(%s)" % table)]
        if column in existing:
            return
        with self._write_lock:
            self._conn().execute(
                "ALTER TABLE %s ADD COLUMN %s %s" % (table, column, declaration)
            )
            self._conn().commit()

    # -- connection handling -------------------------------------------------
    def _conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.executescript(SCHEMA)
            conn.commit()
            self._local.conn = conn
        return conn

    def close(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # -- users ---------------------------------------------------------------
    def create_user(self, school, email, pw_hash, trial_ends_at=None):
        user = {
            "id": uuid.uuid4().hex,
            "school": school,
            "email": email.strip().lower(),
            "pw_hash": pw_hash,
            "created_at": utcnow_iso(),
            "trial_ends_at": trial_ends_at,
        }
        with self._write_lock:
            self._conn().execute(
                "INSERT INTO users (id, school, email, pw_hash, created_at, trial_ends_at)"
                " VALUES (:id, :school, :email, :pw_hash, :created_at, :trial_ends_at)",
                user,
            )
            self._conn().commit()
        return user

    def get_user_by_email(self, email):
        row = self._conn().execute(
            "SELECT * FROM users WHERE email = ?", ((email or "").strip().lower(),)
        ).fetchone()
        return dict(row) if row else None

    def get_user_by_id(self, user_id):
        row = self._conn().execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        return dict(row) if row else None

    def set_trial_end(self, user_id, trial_ends_at):
        with self._write_lock:
            self._conn().execute(
                "UPDATE users SET trial_ends_at = ? WHERE id = ?", (trial_ends_at, user_id)
            )
            self._conn().commit()

    def set_password(self, user_id, pw_hash):
        """Replace the stored password hash (password rotation / ops tooling)."""
        with self._write_lock:
            self._conn().execute(
                "UPDATE users SET pw_hash = ? WHERE id = ?", (pw_hash, user_id)
            )
            self._conn().commit()

    # -- subscriptions -------------------------------------------------------
    def get_subscription(self, user_id):
        row = self._conn().execute(
            "SELECT * FROM subscriptions WHERE user_id = ?", (user_id,)
        ).fetchone()
        return dict(row) if row else None

    def find_subscription_by_customer(self, customer_id):
        if not customer_id:
            return None
        row = self._conn().execute(
            "SELECT * FROM subscriptions WHERE stripe_customer_id = ?", (customer_id,)
        ).fetchone()
        return dict(row) if row else None

    def upsert_subscription(
        self,
        user_id,
        plan=None,
        status=None,
        current_period_end=None,
        stripe_customer_id=None,
        stripe_subscription_id=None,
        provider=None,
        clear_period_end=False,
    ):
        """Write the authoritative subscription row.

        The two *_id columns are named after Stripe for historical reasons but
        hold whichever gateway is live: for PayFast the subscription id is the
        recurring `token` that the ITN and the recurring API both use.
        """
        existing = self.get_subscription(user_id) or {}

        def pick(new, key, default=None):
            if new is not None:
                return new
            if key in existing:
                return existing[key]
            return default

        record = {
            "user_id": user_id,
            "plan": pick(plan, "plan", "annual_school_unlimited"),
            "status": pick(status, "status", "none"),
            "current_period_end": None if clear_period_end else pick(
                current_period_end, "current_period_end"
            ),
            "stripe_customer_id": pick(stripe_customer_id, "stripe_customer_id"),
            "stripe_subscription_id": pick(stripe_subscription_id, "stripe_subscription_id"),
            "provider": pick(provider, "provider"),
            "updated_at": utcnow_iso(),
        }
        with self._write_lock:
            self._conn().execute(
                "INSERT INTO subscriptions (user_id, plan, status, current_period_end,"
                " stripe_customer_id, stripe_subscription_id, provider, updated_at)"
                " VALUES (:user_id, :plan, :status, :current_period_end,"
                " :stripe_customer_id, :stripe_subscription_id, :provider, :updated_at)"
                " ON CONFLICT(user_id) DO UPDATE SET"
                " plan=excluded.plan, status=excluded.status,"
                " current_period_end=excluded.current_period_end,"
                " stripe_customer_id=excluded.stripe_customer_id,"
                " stripe_subscription_id=excluded.stripe_subscription_id,"
                " provider=excluded.provider,"
                " updated_at=excluded.updated_at",
                record,
            )
            self._conn().commit()
        return self.get_subscription(user_id)

    # -- event log (webhook idempotency + audit) -----------------------------
    def record_event(self, event_id, event_type, payload):
        """Return True if newly stored, False if the event id was already seen."""
        try:
            with self._write_lock:
                self._conn().execute(
                    "INSERT INTO events (stripe_event_id, type, payload, created_at)"
                    " VALUES (?, ?, ?, ?)",
                    (event_id, event_type, json.dumps(payload), utcnow_iso()),
                )
                self._conn().commit()
            return True
        except sqlite3.IntegrityError:
            # The event id was already stored. Roll back explicitly: a failed
            # INSERT leaves its transaction (and the write lock) open, and every
            # later write from another thread would block until it timed out.
            self._conn().rollback()
            return False

    def list_events(self, limit=50):
        rows = self._conn().execute(
            "SELECT id, stripe_event_id, type, created_at FROM events"
            " ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- payments (the school's own invoice history) --------------------------
    def record_payment(
        self,
        user_id,
        provider,
        provider_payment_id=None,
        provider_subscription_id=None,
        amount=None,
        currency=None,
        status="unknown",
        item_name=None,
        email=None,
        period_end=None,
    ):
        """Store one payment. Returns True when new, False when already seen.

        A local gateway retries notifications, so the same payment can arrive
        several times; the UNIQUE provider_payment_id is the guard.
        """
        try:
            with self._write_lock:
                self._conn().execute(
                    "INSERT INTO payments (user_id, provider, provider_payment_id,"
                    " provider_subscription_id, amount, currency, status, item_name,"
                    " email, period_end, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        user_id,
                        provider,
                        provider_payment_id,
                        provider_subscription_id,
                        amount,
                        currency,
                        status,
                        item_name,
                        email,
                        period_end,
                        utcnow_iso(),
                    ),
                )
                self._conn().commit()
            return True
        except sqlite3.IntegrityError:
            # Already recorded (the gateway retried the notification). Roll back
            # so the failed INSERT does not hold the write lock for the next
            # request and turn a retry into a stall.
            self._conn().rollback()
            return False

    def list_payments(self, user_id, limit=24):
        rows = self._conn().execute(
            "SELECT * FROM payments WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- uploaded documents (per-account PDF library) -------------------------
    def create_document(self, document_id, user_id, filename, title, category, size):
        """Record one uploaded document. The bytes live beside the database."""
        with self._write_lock:
            self._conn().execute(
                "INSERT INTO documents (id, user_id, filename, title, category,"
                " size, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (document_id, user_id, filename, title, category, size, utcnow_iso()),
            )
            self._conn().commit()
        return self.get_document(document_id, user_id)

    def get_document(self, document_id, user_id):
        """Fetch one document only if it belongs to this user."""
        row = self._conn().execute(
            "SELECT * FROM documents WHERE id = ? AND user_id = ?",
            (document_id, user_id),
        ).fetchone()
        return dict(row) if row else None

    def list_documents(self, user_id):
        rows = self._conn().execute(
            "SELECT * FROM documents WHERE user_id = ?"
            " ORDER BY created_at DESC, id DESC",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def document_usage(self, user_id):
        """Return ``(count, total_bytes)`` for one account's storage quota."""
        row = self._conn().execute(
            "SELECT COUNT(*) AS count, COALESCE(SUM(size), 0) AS total"
            " FROM documents WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return int(row["count"]), int(row["total"])

    def delete_document(self, document_id, user_id):
        """Delete one document row, scoped to its owner. True when removed."""
        with self._write_lock:
            cursor = self._conn().execute(
                "DELETE FROM documents WHERE id = ? AND user_id = ?",
                (document_id, user_id),
            )
            self._conn().commit()
        return cursor.rowcount > 0
