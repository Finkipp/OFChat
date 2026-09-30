"""Local, per-account message history. Passwords are never persisted."""

import csv
import sqlite3
from datetime import datetime
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
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(messages)")}
        for name, definition in (
            ("stanza_id", "TEXT"), ("status", "TEXT NOT NULL DEFAULT 'unknown'"),
            ("attachment", "TEXT"), ("expires_at", "TEXT"),
            ("kind", "TEXT NOT NULL DEFAULT 'text'"),
        ):
            if name not in columns:
                self.db.execute(f"ALTER TABLE messages ADD COLUMN {name} {definition}")
        self.db.execute("CREATE INDEX IF NOT EXISTS messages_stanza ON messages(account, peer, stanza_id)")
        self.db.commit()

    def add(self, account, peer, direction, body, *, stanza_id=None, status="unknown",
            attachment=None, expires_at=None, kind="text"):
        with self.db:
            cursor = self.db.execute(
                """INSERT INTO messages(account, peer, direction, body, stanza_id, status, attachment, expires_at, kind)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (account, peer, direction, body, stanza_id, status, attachment, expires_at, kind),
            )
        return cursor.lastrowid

    def set_status(self, account, peer, stanza_id, status):
        row = self.db.execute(
            """SELECT peer FROM messages WHERE account=? AND direction='out'
            AND stanza_id=? ORDER BY id DESC LIMIT 1""", (account, stanza_id)
        ).fetchone()
        if not row or (row[0] != peer and status != "failed"):
            return None
        with self.db:
            updated = self.db.execute(
                """UPDATE messages SET status=? WHERE account=? AND peer=?
                AND direction='out' AND stanza_id=?
                AND (status != 'read' OR ?='read')""",
                (status, account, row[0], stanza_id, status),
            )
        return (row[0], status) if updated.rowcount else None

    def mark_read(self, account, peer):
        ids = [row[0] for row in self.db.execute(
            """SELECT stanza_id FROM messages WHERE account=? AND peer=?
            AND direction='in' AND stanza_id IS NOT NULL AND status!='read'""",
            (account, peer),
        )]
        with self.db:
            self.db.execute(
                """UPDATE messages SET status='read' WHERE account=? AND peer=?
                AND direction='in' AND stanza_id IS NOT NULL""", (account, peer)
            )
        return ids

    def conversation(self, account, peer, query="", page=0, page_size=100):
        """One chronological page; page zero is the newest."""
        if page < 0 or page_size < 1:
            raise ValueError("Неверный номер страницы")
        where = "account=? AND peer=?"
        params = [account, peer]
        if query:
            where += " AND casefold(body) LIKE ? ESCAPE '\\'"
            params.append(self._search_pattern(query.casefold()))
        total = self.db.execute(f"SELECT COUNT(*) FROM messages WHERE {where}", params).fetchone()[0]
        rows = self.db.execute(
            f"""SELECT id, direction, body, timestamp, stanza_id, status, attachment, kind FROM (
                SELECT id, direction, body, timestamp, stanza_id, status, attachment, kind
                FROM messages WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?
            ) ORDER BY id""",
            [*params, page_size, page * page_size],
        ).fetchall()
        return rows, total

    def expire_attachments(self, directory, now=None):
        """Delete only our own saved files after 24 hours, keeping text history."""
        directory = Path(directory).resolve()
        now = now or datetime.now()
        expired = self.db.execute(
            "SELECT id, attachment FROM messages WHERE attachment IS NOT NULL AND expires_at <= ?",
            (now.strftime("%Y-%m-%d %H:%M:%S"),),
        ).fetchall()
        with self.db:
            for row_id, filename in expired:
                path = Path(filename).resolve()
                if path.is_relative_to(directory):
                    path.unlink(missing_ok=True)
                self.db.execute("UPDATE messages SET attachment=NULL WHERE id=?", (row_id,))
        return len(expired)

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
