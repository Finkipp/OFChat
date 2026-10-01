import asyncio
import csv
import sqlite3
import ssl
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from slixmpp import JID

from chat.accounts import Preferences
from chat.attachments import FileStore
from chat.certificates import TrustedCertificates, fingerprint, inspect_certificate
from chat.formatting import blocks, inline_markup
from chat.storage import History
from chat.xmpp import MAX_FILE, ChatClient, Connection, bare_jid, presence_status


class ChatTests(unittest.TestCase):
    def test_existing_history_is_migrated_without_losing_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.db"
            db = sqlite3.connect(path)
            db.execute("""CREATE TABLE messages (
                id INTEGER PRIMARY KEY, account TEXT NOT NULL, peer TEXT NOT NULL,
                direction TEXT NOT NULL, body TEXT NOT NULL,
                timestamp TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            )""")
            db.execute("INSERT INTO messages(account, peer, direction, body) VALUES (?,?,?,?)",
                       ("alice@example.org", "bob@example.org", "in", "Старая переписка"))
            db.commit()
            db.close()
            history = History(path)
            rows, total = history.conversation("alice@example.org", "bob@example.org")
            self.assertEqual(total, 1)
            self.assertEqual(rows[0][2], "Старая переписка")
            history.close()

    def test_history_is_persistent_and_separated_by_account(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.db"
            history = History(path)
            history.add("a@example.org", "b@example.org", "out", "Привет")
            history.add("a@example.org", "b@example.org", "in", "Здравствуйте")
            history.add("c@example.org", "b@example.org", "in", "Другая история")
            history.close()
            history = History(path)
            self.assertEqual(
                [(direction, body) for direction, body, _ in history.recent("a@example.org", "b@example.org")],
                [("out", "Привет"), ("in", "Здравствуйте")],
            )
            self.assertEqual(history.peers("c@example.org"), ["b@example.org"])
            self.assertEqual(len(history.recent("c@example.org", "b@example.org")), 1)
            history.close()

    def test_favorites_self_message_can_be_deduplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            history = History(Path(directory) / "self.db")
            history.add("alice@example.org", "alice@example.org", "out", "Заметка",
                        stanza_id="self-1")
            self.assertTrue(history.has_message("alice@example.org", "alice@example.org", "self-1"))
            self.assertTrue(history.has_outgoing("alice@example.org", "self-1"))
            self.assertFalse(history.has_message("alice@example.org", "alice@example.org", "other-1"))
            history.close()

    def test_jid_validation_and_resource_normalization(self):
        self.assertEqual(bare_jid(" alice@example.org/phone "), "alice@example.org")
        for invalid in ("", "example.org", "alice"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                bare_jid(invalid)

    def test_history_pages_search_recent_window_and_export(self):
        with tempfile.TemporaryDirectory() as directory:
            history = History(Path(directory) / "messages.db")
            account, peer = "a@example.org", "b@example.org"
            history.add(account, peer, "in", "Старое сообщение")
            history.db.execute(
                "UPDATE messages SET timestamp=datetime('now', '-4 days') WHERE body=?",
                ("Старое сообщение",),
            )
            for body in ("первое", "второе 100%", "третье 100_", "четвёртое"):
                history.add(account, peer, "out", body)
            self.assertEqual(len(history.recent(account, peer)), 4)
            newest, total = history.page(account, peer, page_size=2)
            older, _ = history.page(account, peer, page=1, page_size=2)
            self.assertEqual(total, 5)
            self.assertEqual([row[1] for row in newest], ["третье 100_", "четвёртое"])
            self.assertEqual([row[1] for row in older], ["первое", "второе 100%"])
            self.assertEqual(history.page(account, peer, "100%")[1], 1)
            self.assertEqual(history.page(account, peer, "100_")[1], 1)
            self.assertEqual(history.page(account, peer, "СТАРОЕ")[1], 1)
            csv_path = Path(directory) / "conversation.csv"
            history.export(account, peer, csv_path)
            with csv_path.open(encoding="utf-8", newline="") as output:
                records = list(csv.reader(output))
            self.assertEqual(len(records), 6)
            self.assertEqual(records[1][1:], [peer, "Старое сообщение"])
            txt_path = Path(directory) / "conversation.txt"
            history.export(account, peer, txt_path)
            self.assertIn("второе 100%", txt_path.read_text(encoding="utf-8"))
            history.close()

    def test_broadcast_queues_individual_messages(self):
        connection = Connection(lambda *args: None)
        sent = []
        class FakeMessage(dict):
            def send(self):
                sent.append(self)

        connection.client = type("FakeClient", (), {
            "make_message": lambda self, **kwargs: FakeMessage(kwargs),
        })()
        connection._schedule = lambda callback: callback()
        connection.send_messages([
            ("b@example.org", "id-b"), ("c@example.org", "id-c")
        ], "Привет")
        self.assertEqual([item["mto"] for item in sent], ["b@example.org", "c@example.org"])
        self.assertEqual([item["id"] for item in sent], ["id-b", "id-c"])
        self.assertTrue(all(item["mtype"] == "chat" for item in sent))
        self.assertTrue(all(item["request_receipt"] for item in sent))

    def test_incoming_message_and_attention_events(self):
        events = []
        client = ChatClient("alice@example.org", "password", lambda *args: events.append(args))
        message = client.Message()
        message["from"] = "bob@example.org/desktop"
        message["type"] = "chat"
        message["body"] = "Привет"
        message["markable"] = True
        client._on_message(message)
        client._on_attention(message)
        self.assertEqual(events[0][0], "message")
        self.assertEqual(events[0][1][:2], ("bob@example.org", "Привет"))
        self.assertTrue(events[0][1][2])
        self.assertEqual(events[1], ("attention", "bob@example.org"))

    def test_file_offer_authorization_and_received_chat_states(self):
        import hashlib

        events = []
        client = ChatClient("alice@example.org", "password", lambda *args: events.append(args))
        client.init_plugins()
        message = client.Message()
        message["from"] = "bob@example.org/desktop"
        message["quark_file"]["sid"] = "transfer-1"
        message["quark_file"]["filename"] = "image.png"
        message["quark_file"]["size"] = "5"
        message["quark_file"]["sha256"] = hashlib.sha256(b"image").hexdigest()
        client._on_message(message)
        self.assertTrue(client._authorize_file(None, "transfer-1", JID("bob@example.org/desktop"), None))
        self.assertFalse(client._authorize_file(None, "transfer-1", JID("evil@example.org"), None))
        oversized = client.Message()
        oversized["from"] = "bob@example.org/desktop"
        oversized["quark_file"]["sid"] = "too-big"
        oversized["quark_file"]["filename"] = "huge.bin"
        oversized["quark_file"]["size"] = str(MAX_FILE + 1)
        client._on_message(oversized)
        self.assertNotIn("too-big", client.pending_files)

        class Stream:
            sid = "transfer-1"

            async def gather(self, **kwargs):
                return b"image"

        client.loop.run_until_complete(client._receive_file(Stream(), client.pending_files["transfer-1"]))
        self.assertEqual(events[-1][0], "file_received")
        self.assertEqual(events[-1][1][:2], ("bob@example.org", "image.png"))
        state = client.Message()
        state["from"] = "bob@example.org/desktop"
        state["chat_state"] = "composing"
        client._on_chatstate(state)
        self.assertEqual(events[-1], ("chatstate", ("bob@example.org", "composing")))

    def test_delivery_receipt_and_read_marker(self):
        events = []
        client = ChatClient("alice@example.org", "password", lambda *args: events.append(args))
        client.init_plugins()
        receipt = client.Message()
        receipt["from"] = "bob@example.org/desktop"
        receipt["receipt"] = "message-1"
        client._on_receipt(receipt)
        displayed = client.Message()
        displayed["from"] = "bob@example.org/desktop"
        displayed["displayed"]["id"] = "message-1"
        client._on_displayed(displayed)
        self.assertEqual(events, [
            ("delivery", ("bob@example.org", "message-1", "delivered")),
            ("delivery", ("bob@example.org", "message-1", "read")),
        ])

    def test_carbon_from_second_device_reaches_local_history_handler(self):
        events = []
        client = ChatClient("alice@example.org", "password", lambda *args: events.append(args))
        client.init_plugins()
        forwarded = client.Message()
        forwarded["from"] = "bob@example.org/mobile"
        forwarded["body"] = "С телефона"
        forwarded["type"] = "chat"
        envelope = client.Message()
        envelope["from"] = "alice@example.org"
        envelope["carbon_received"] = forwarded
        client._on_carbon_received(envelope)
        self.assertEqual(events[-1][0], "message")
        self.assertEqual(events[-1][1][:2], ("bob@example.org", "С телефона"))

    def test_presence_status_accounts_for_all_resources(self):
        self.assertEqual(presence_status({}), "offline")
        self.assertEqual(presence_status({"phone": {"show": "xa"}}), "away")
        self.assertEqual(presence_status({"pc": {"show": "away"}, "phone": {"show": ""}}), "online")

    def test_formatting_escapes_html_and_parses_code_and_links(self):
        self.assertEqual(
            inline_markup('**Привет** *мир* `<tag>` [сайт](https://example.org?a=1&b=2)'),
            '<b>Привет</b> <i>мир</i> <span font_family="monospace">&lt;tag&gt;</span> '
            '<a href="https://example.org?a=1&amp;b=2">сайт</a>',
        )
        self.assertEqual(
            blocks('текст\n```py\nprint("hi")\n```\nещё'),
            [("text", "", "текст"), ("code", "py", 'print("hi")'), ("text", "", "ещё")],
        )
        self.assertIn("javascript:alert", inline_markup("[text](javascript:alert(1))"))
        self.assertNotIn("<a href", inline_markup("[text](javascript:alert(1))"))

    def test_delivery_read_and_file_expiry_preserve_existing_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = FileStore(root / "files")
            original = root / "original.png"
            original.write_bytes(b"png data")
            path, expiry = store.save(original.name, original.read_bytes())
            history = History(root / "history.db")
            history.add("a@server", "b@server", "out", "image.png", stanza_id="id-1",
                        status="pending", kind="file", attachment=str(path), expires_at=expiry)
            history.set_status("a@server", "b@server", "id-1", "delivered")
            history.set_status("a@server", "b@server", "id-1", "read")
            history.set_status("a@server", "b@server", "id-1", "failed")
            rows, total = history.conversation("a@server", "b@server")
            self.assertEqual((total, rows[0][5]), (1, "read"))
            self.assertEqual(history.expire_attachments(
                store.directory, datetime.now() + timedelta(days=2)
            ), 1)
            self.assertFalse(path.exists())
            self.assertEqual(original.read_bytes(), b"png data")
            rows, _ = history.conversation("a@server", "b@server")
            self.assertIsNone(rows[0][6])
            history.close()

    def test_preferences_store_no_password_and_manage_autostart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Preferences(root / "account.json", root / "autostart.desktop")
            settings.save("alice@example.org", "server", 5222, True)
            self.assertEqual(settings.load()["jid"], "alice@example.org")
            self.assertNotIn("password", settings.path.read_text(encoding="utf-8"))
            settings.set_autostart(True)
            self.assertIn("Exec=", settings.autostart.read_text(encoding="utf-8"))
            settings.set_autostart(False)
            self.assertFalse(settings.autostart.exists())

    def test_self_signed_starttls_certificate_can_be_inspected_and_pinned(self):
        with tempfile.TemporaryDirectory() as directory:
            cert = Path(directory) / "cert.pem"
            key = Path(directory) / "key.pem"
            subprocess.run([
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                "-keyout", str(key), "-out", str(cert), "-days", "1",
                "-subj", "/CN=localhost",
            ], check=True, capture_output=True)

            async def check():
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.load_cert_chain(cert, key)

                async def server(reader, writer):
                    try:
                        await reader.readuntil(b">")
                        writer.write(b"<stream:features><starttls xmlns='urn:ietf:params:xml:ns:xmpp-tls'/></stream:features>")
                        await writer.drain()
                        await reader.readuntil(b"/>")
                        writer.write(b"<proceed xmlns='urn:ietf:params:xml:ns:xmpp-tls'/>")
                        await writer.drain()
                        await writer.start_tls(context)
                    except (OSError, ssl.SSLError, asyncio.IncompleteReadError):
                        pass
                    finally:
                        writer.close()
                        try:
                            await writer.wait_closed()
                        except (OSError, ssl.SSLError):
                            pass

                listener = await asyncio.start_server(server, "127.0.0.1", 0)
                async with listener:
                    port = listener.sockets[0].getsockname()[1]
                    pem, valid, reason = await inspect_certificate("127.0.0.1", port, "localhost")
                self.assertFalse(valid)
                self.assertIn("certificate verify failed", reason)
                store = TrustedCertificates(Path(directory) / "trusted.json")
                store.save("localhost", "127.0.0.1", port, pem)
                saved = store.get("localhost", "127.0.0.1", port)
                self.assertEqual(saved["fingerprint"], fingerprint(ssl.PEM_cert_to_DER_cert(pem)))
                self.assertIsNone(store.get("localhost", "another-host", port))
                client = ChatClient("alice@localhost", "password", lambda *args: None, pinned=saved)

                async def tls_server(reader, writer):
                    writer.close()

                tls_listener = await asyncio.start_server(
                    tls_server, "127.0.0.1", 0, ssl=context
                )
                async with tls_listener:
                    tls_port = tls_listener.sockets[0].getsockname()[1]
                    reader, writer = await asyncio.open_connection(
                        "127.0.0.1", tls_port, ssl=client.ssl_context,
                        server_hostname="localhost",
                    )
                    self.assertEqual(
                        fingerprint(writer.get_extra_info("ssl_object").getpeercert(True)),
                        saved["fingerprint"],
                    )
                    writer.close()
                    await writer.wait_closed()

            try:
                old_loop = asyncio.get_event_loop()
            except RuntimeError:
                old_loop = None
            if old_loop and not old_loop.is_running():
                old_loop.close()
            asyncio.run(check())


if __name__ == "__main__":
    unittest.main()
