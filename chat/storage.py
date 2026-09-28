"""Local, per-account message history. Passwords are never persisted."""

import sqlite3
from pathlib import Path


class History:
    def __init__(self, path=None):
        if path is None:
            path = Path.home() / ".local" / "share" / "openfire-chat" / "history.db"
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("""CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY,
            account TEXT NOT NULL,
            peer TEXT NOT NULL,
            direction TEXT NOT NULL,
            body TEXT NOT NULL,
            timestamp TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        )""")
        self.db.execute("CREATE INDEX IF NOT EXISTS messages_peer ON messages(account, peer, id)")
        self.db.commit()

    def add(self, account, peer, direction, body):
        with self.db:
            self.db.execute(
                "INSERT INTO messages(account, peer, direction, body) VALUES (?, ?, ?, ?)",
                (account, peer, direction, body),
            )

    def recent(self, account, peer, limit=200):
        rows = self.db.execute(
            """SELECT direction, body, timestamp FROM (
                SELECT id, direction, body, timestamp FROM messages
                WHERE account=? AND peer=? ORDER BY id DESC LIMIT ?
            ) ORDER BY id""",
            (account, peer, limit),
        ).fetchall()
        return rows

    def peers(self, account):
        return [row[0] for row in self.db.execute(
            """SELECT peer FROM messages WHERE account=?
            GROUP BY peer ORDER BY MAX(id) DESC""", (account,)
        )]

    def close(self):
        self.db.close()
