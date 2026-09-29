"""Local, per-account message history. Passwords are never persisted."""

import csv
import sqlite3
from pathlib import Path


class History:
    def __init__(self, path=None):
        if path is None:
            path = Path.home() / ".local" / "share" / "openfire-chat" / "history.db"
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.create_function("casefold", 1, str.casefold, deterministic=True)
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

    def recent(self, account, peer, limit=100, days=3):
        rows = self.db.execute(
            """SELECT direction, body, timestamp FROM (
                SELECT id, direction, body, timestamp FROM messages
                WHERE account=? AND peer=?
                AND timestamp >= datetime('now', 'localtime', ?)
                ORDER BY id DESC LIMIT ?
            ) ORDER BY id""",
            (account, peer, f"-{days} days", limit),
        ).fetchall()
        return rows

    @staticmethod
    def _search_pattern(text):
        return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"

    def page(self, account, peer, query="", page=0, page_size=50):
        """Return one chronological page from newest to oldest pages and total matches."""
        if page < 0 or page_size < 1:
            raise ValueError("Неверный номер страницы")
        where = "account=? AND peer=?"
        params = [account, peer]
        if query:
            where += " AND casefold(body) LIKE ? ESCAPE '\\'"
            params.append(self._search_pattern(query.casefold()))
        total = self.db.execute(f"SELECT COUNT(*) FROM messages WHERE {where}", params).fetchone()[0]
        rows = self.db.execute(
            f"""SELECT direction, body, timestamp FROM (
                SELECT id, direction, body, timestamp FROM messages
                WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?
            ) ORDER BY id""",
            [*params, page_size, page * page_size],
        ).fetchall()
        return rows, total

    def export(self, account, peer, path):
        """Stream the full conversation as UTF-8 TXT or CSV."""
        path = Path(path)
        with path.open("w", encoding="utf-8", newline="") as output:
            writer = csv.writer(output) if path.suffix.lower() == ".csv" else None
            if writer:
                writer.writerow(("Дата", "Отправитель", "Сообщение"))
            last_id = 0
            while True:
                rows = self.db.execute(
                    """SELECT id, direction, body, timestamp FROM messages
                    WHERE account=? AND peer=? AND id>? ORDER BY id LIMIT 500""",
                    (account, peer, last_id),
                ).fetchall()
                if not rows:
                    break
                for message_id, direction, body, timestamp in rows:
                    sender = account if direction == "out" else peer
                    if writer:
                        writer.writerow((timestamp, sender, body))
                    else:
                        output.write(f"[{timestamp}] {sender}: {body}\n")
                    last_id = message_id

    def peers(self, account):
        return [row[0] for row in self.db.execute(
            """SELECT peer FROM messages WHERE account=?
            GROUP BY peer ORDER BY MAX(id) DESC""", (account,)
        )]

    def close(self):
        self.db.close()
