"""SQLite storage shared by the bot and the website (two processes, one file).

Safety across processes: WAL mode + busy_timeout, and every write runs in a
BEGIN IMMEDIATE transaction that re-checks balances/positions *inside* the
transaction, so two processes can never overspend or double-sell.
"""
import sqlite3
import time
from contextlib import contextmanager


class DBConflict(Exception):
    """A write was refused because the data changed (e.g. not enough balance, position already closed)."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  tg_id INTEGER PRIMARY KEY, username TEXT, balance REAL NOT NULL,
  created_at REAL NOT NULL, last_reset REAL);
CREATE TABLE IF NOT EXISTS positions(
  id INTEGER PRIMARY KEY, tg_id INTEGER NOT NULL, mint TEXT NOT NULL, symbol TEXT,
  tokens REAL NOT NULL, cost_sol REAL NOT NULL, opened_at REAL NOT NULL,
  tp_pct REAL, sl_pct REAL, status TEXT NOT NULL DEFAULT 'open', closed_at REAL,
  realized_sol REAL NOT NULL DEFAULT 0);
CREATE UNIQUE INDEX IF NOT EXISTS one_open ON positions(tg_id, mint) WHERE status='open';
CREATE TABLE IF NOT EXISTS trades(
  id INTEGER PRIMARY KEY, tg_id INTEGER NOT NULL, position_id INTEGER, mint TEXT, symbol TEXT,
  side TEXT, sol REAL, tokens REAL, fee_sol REAL, impact_pct REAL, naive REAL,
  pnl_sol REAL, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS login_nonces(nonce TEXT PRIMARY KEY, used_at REAL NOT NULL);
"""


class DB:
    def __init__(self, path: str = ":memory:"):
        # isolation_level=None: we control transactions ourselves (see tx()).
        self.c = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=10)
        self.c.row_factory = sqlite3.Row
        self.c.execute("PRAGMA busy_timeout=10000")
        if path != ":memory:":
            self.c.execute("PRAGMA journal_mode=WAL")
        self.c.executescript(SCHEMA)
        self._migrate()

    @contextmanager
    def tx(self):
        """Write transaction that takes the DB write lock up front (safe across processes)."""
        self.c.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.c.execute("ROLLBACK")
            raise
        self.c.execute("COMMIT")

    def use_nonce(self, nonce: str) -> bool:
        """Mark a one-time login nonce as used. False if it was already used."""
        try:
            with self.tx():
                self.c.execute("DELETE FROM login_nonces WHERE used_at < ?", (time.time() - 86400,))
                self.c.execute("INSERT INTO login_nonces VALUES(?,?)", (nonce, time.time()))
            return True
        except sqlite3.IntegrityError:
            return False

    def _migrate(self):
        cols = {r["name"] for r in self.c.execute("PRAGMA table_info(users)")}
        if "reset_count" not in cols:
            with self.tx():
                self.c.execute("ALTER TABLE users ADD COLUMN reset_count INTEGER NOT NULL DEFAULT 0")
                # existing users who already reset at least once (exact older count is unknown)
                self.c.execute("UPDATE users SET reset_count=1 WHERE last_reset IS NOT NULL")

    def user(self, tg_id):
        return self.c.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()

    def create_user(self, tg_id, username, balance) -> bool:
        cur = self.c.execute("INSERT OR IGNORE INTO users(tg_id,username,balance,created_at) VALUES(?,?,?,?)",
                             (tg_id, username, balance, time.time()))
        self.c.commit()
        return cur.rowcount == 1

    def open_position(self, tg_id, mint):
        return self.c.execute("SELECT * FROM positions WHERE tg_id=? AND mint=? AND status='open'",
                              (tg_id, mint)).fetchone()

    def position(self, pos_id, tg_id):
        return self.c.execute("SELECT * FROM positions WHERE id=? AND tg_id=? AND status='open'",
                              (pos_id, tg_id)).fetchone()

    def open_positions(self, tg_id=None):
        if tg_id is None:
            return self.c.execute("SELECT * FROM positions WHERE status='open'").fetchall()
        return self.c.execute("SELECT * FROM positions WHERE tg_id=? AND status='open' ORDER BY opened_at",
                              (tg_id,)).fetchall()

    def record_buy(self, tg_id, mint, symbol, fill, now=None):
        now = now or time.time()
        with self.tx():
            cost = fill.sol + fill.priority_fee  # swap fee is already inside fill.sol
            cur = self.c.execute("UPDATE users SET balance=balance-? WHERE tg_id=? AND balance>=?", (cost, tg_id, cost))
            if cur.rowcount != 1:
                raise DBConflict("Not enough paper SOL.")
            pos = self.open_position(tg_id, mint)
            if pos:
                self.c.execute("UPDATE positions SET tokens=tokens+?, cost_sol=cost_sol+? WHERE id=?",
                               (fill.tokens, cost, pos["id"]))
                pid = pos["id"]
            else:
                pid = self.c.execute("INSERT INTO positions(tg_id,mint,symbol,tokens,cost_sol,opened_at)"
                                     " VALUES(?,?,?,?,?,?)", (tg_id, mint, symbol, fill.tokens, cost, now)).lastrowid
            self._trade(tg_id, pid, mint, symbol, fill, None, now)
        return pid

    def record_sell(self, pos, fill, now=None) -> float:
        """Reduce position by fill.tokens; returns realized PnL of this sell (SOL)."""
        now = now or time.time()
        with self.tx():
            pos = self.c.execute("SELECT * FROM positions WHERE id=? AND status='open'", (pos["id"],)).fetchone()
            if not pos or pos["tokens"] <= 0:
                raise DBConflict("That position is already closed.")
            if fill.tokens > pos["tokens"]:  # position shrank meanwhile (other process): scale the fill down
                k = pos["tokens"] / fill.tokens
                fill.tokens, fill.sol = pos["tokens"], fill.sol * k
            frac = min(1.0, fill.tokens / pos["tokens"])
            cost_part = pos["cost_sol"] * frac
            pnl_sol = fill.sol - cost_part
            self.c.execute("UPDATE users SET balance=balance+? WHERE tg_id=?", (fill.sol, pos["tg_id"]))
            if frac >= 0.999999:
                self.c.execute("UPDATE positions SET tokens=0, cost_sol=0, status='closed', closed_at=?,"
                               " realized_sol=realized_sol+? WHERE id=?", (now, pnl_sol, pos["id"]))
            else:
                self.c.execute("UPDATE positions SET tokens=tokens-?, cost_sol=cost_sol-?,"
                               " realized_sol=realized_sol+? WHERE id=?",
                               (fill.tokens, cost_part, pnl_sol, pos["id"]))
            self._trade(pos["tg_id"], pos["id"], pos["mint"], pos["symbol"], fill, pnl_sol, now)
        return pnl_sol

    def _trade(self, tg_id, pid, mint, symbol, f, pnl_sol, now):
        self.c.execute("INSERT INTO trades(tg_id,position_id,mint,symbol,side,sol,tokens,fee_sol,impact_pct,naive,"
                       "pnl_sol,ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (tg_id, pid, mint, symbol, f.side, f.sol, f.tokens, f.fee_sol, f.impact_pct, f.naive,
                        pnl_sol, now))

    def set_tpsl(self, pos_id, tp=None, sl=None):
        if tp is not None:
            self.c.execute("UPDATE positions SET tp_pct=? WHERE id=?", (tp, pos_id))
        if sl is not None:
            self.c.execute("UPDATE positions SET sl_pct=? WHERE id=?", (sl, pos_id))
        self.c.commit()

    def clear_tpsl(self, pos_id, kind):
        col = {"tp": "tp_pct", "sl": "sl_pct"}[kind]
        self.c.execute(f"UPDATE positions SET {col}=NULL WHERE id=?", (pos_id,))
        self.c.commit()

    def sold_sol(self, pos_id) -> float:
        """SOL received so far from sells of this position."""
        return self.c.execute("SELECT COALESCE(SUM(sol),0) FROM trades WHERE position_id=? AND side='sell'",
                              (pos_id,)).fetchone()[0]

    def history(self, tg_id, limit=10):
        return self.c.execute("SELECT * FROM trades WHERE tg_id=? ORDER BY id DESC LIMIT ?",
                              (tg_id, limit)).fetchall()

    def stats(self, tg_id) -> dict:
        rows = self.c.execute("SELECT realized_sol, opened_at, closed_at FROM positions"
                              " WHERE tg_id=? AND status='closed'", (tg_id,)).fetchall()
        n = len(rows)
        wins = sum(1 for r in rows if r["realized_sol"] > 0)
        return {"closed": n, "win_rate": wins / n * 100 if n else 0.0,
                "net_pnl": sum(r["realized_sol"] for r in rows),
                "avg_hold_s": sum(r["closed_at"] - r["opened_at"] for r in rows) / n if n else 0.0}

    def reset(self, tg_id, balance, now=None):
        now = now or time.time()
        with self.tx():
            self.c.execute("UPDATE positions SET status='reset', closed_at=? WHERE tg_id=? AND status='open'",
                           (now, tg_id))
            self.c.execute("UPDATE users SET balance=?, last_reset=?, reset_count=reset_count+1 WHERE tg_id=?",
                           (balance, now, tg_id))

    def leaderboard(self, limit=10):
        return self.c.execute(
            "SELECT u.tg_id, u.username, COALESCE(SUM(p.realized_sol),0) AS pnl, COUNT(p.id) AS n,"
            " COALESCE(SUM(p.realized_sol > 0),0) AS wins, u.reset_count AS resets"
            " FROM users u LEFT JOIN positions p ON p.tg_id=u.tg_id AND p.status='closed'"
            " AND p.closed_at > COALESCE(u.last_reset,0)"
            " GROUP BY u.tg_id ORDER BY pnl DESC LIMIT ?", (limit,)).fetchall()

