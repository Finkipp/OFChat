import asyncio
import ssl
import subprocess
import tempfile
import unittest
from pathlib import Path

from chat.certificates import TrustedCertificates, fingerprint, inspect_certificate
from chat.storage import History
from chat.xmpp import ChatClient, bare_jid, presence_status


class ChatTests(unittest.TestCase):
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

    def test_jid_validation_and_resource_normalization(self):
        self.assertEqual(bare_jid(" alice@example.org/phone "), "alice@example.org")
        for invalid in ("", "example.org", "alice"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                bare_jid(invalid)

    def test_incoming_message_and_attention_events(self):
        events = []
        client = ChatClient("alice@example.org", "password", lambda *args: events.append(args))
        message = client.Message()
        message["from"] = "bob@example.org/desktop"
        message["type"] = "chat"
        message["body"] = "Привет"
        client._on_message(message)
        client._on_attention(message)
        self.assertEqual(events, [
            ("message", ("bob@example.org", "Привет")),
            ("attention", "bob@example.org"),
        ])

    def test_presence_status_accounts_for_all_resources(self):
        self.assertEqual(presence_status({}), "offline")
        self.assertEqual(presence_status({"phone": {"show": "xa"}}), "away")
        self.assertEqual(presence_status({"pc": {"show": "away"}, "phone": {"show": ""}}), "online")

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

            asyncio.run(check())


if __name__ == "__main__":
    unittest.main()
