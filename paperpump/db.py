"""SQLite storage. One file, no server. The website can reuse the same schema later."""
import sqlite3
import time

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
"""


class DB:
    def __init__(self, path: str = ":memory:"):
        self.c = sqlite3.connect(path, check_same_thread=False)
        self.c.row_factory = sqlite3.Row
        self.c.executescript(SCHEMA)

    def user(self, tg_id):
        return self.c.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()

    def create_user(self, tg_id, username, balance) -> bool:
        cur = self.c.execute("INSERT OR IGNORE INTO users VALUES(?,?,?,?,NULL)",
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
        with self.c:
            cost = fill.sol + fill.priority_fee  # swap fee is already inside fill.sol
            self.c.execute("UPDATE users SET balance=balance-? WHERE tg_id=?", (cost, tg_id))
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
        frac = min(1.0, fill.tokens / pos["tokens"])
        cost_part = pos["cost_sol"] * frac
        pnl_sol = fill.sol - cost_part
        with self.c:
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
        with self.c:
            self.c.execute("UPDATE positions SET status='reset', closed_at=? WHERE tg_id=? AND status='open'",
                           (now, tg_id))
            self.c.execute("UPDATE users SET balance=?, last_reset=? WHERE tg_id=?", (balance, now, tg_id))

    def leaderboard(self, limit=10):
        return self.c.execute(
            "SELECT u.tg_id, u.username, COALESCE(SUM(p.realized_sol),0) AS pnl, COUNT(p.id) AS n"
            " FROM users u LEFT JOIN positions p ON p.tg_id=u.tg_id AND p.status='closed'"
            " AND p.closed_at > COALESCE(u.last_reset,0)"
            " GROUP BY u.tg_id ORDER BY pnl DESC LIMIT ?", (limit,)).fetchall()

