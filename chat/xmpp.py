"""Slixmpp runs on its own asyncio loop; UI callbacks are dispatched by the caller."""

import asyncio
import hashlib
import ssl
import threading

from slixmpp import ClientXMPP
from slixmpp.jid import JID
from slixmpp.stanza import Message
from slixmpp.xmlstream import ElementBase, register_stanza_plugin

from .certificates import TrustedCertificates, fingerprint, inspect_certificate


MAX_FILE = 5 * 1024 * 1024


class FileOffer(ElementBase):
    name = "file"
    namespace = "urn:quark:attachment:1"
    plugin_attrib = "quark_file"
    interfaces = {"sid", "filename", "size", "mime", "sha256"}


def bare_jid(value):
    jid = JID(value.strip())
    if not jid.user or not jid.domain:
        raise ValueError("Укажите JID вида user@server")
    return jid.bare


def presence_status(resources):
    """Map all of a contact's XMPP resources to a single visible status."""
    if not resources:
        return "offline"
    if any(info.get("show") not in ("away", "xa", "dnd") for info in resources.values()):
        return "online"
    return "away"


class ChatClient(ClientXMPP):
    def __init__(self, jid, password, emit, pinned=None):
        context = ssl.create_default_context()
        if pinned:
            context.load_verify_locations(cadata=pinned["pem"])
            context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
            context.check_hostname = False
        super().__init__(jid, password, ssl_context=context)
        self.emit = emit
        self.pinned_fingerprint = pinned["fingerprint"] if pinned else None
        self._closing = False
        self.enable_plaintext = False
        self.register_plugin("xep_0224")
        self.register_plugin("xep_0184")
        self.register_plugin("xep_0333")
        self.register_plugin("xep_0047")
        self.register_plugin("xep_0085")
        register_stanza_plugin(Message, FileOffer)
        self.pending_files = {}
        self.add_event_handler("ibb_stream_start", self._on_file_stream)
        self.add_event_handler("session_start", self._on_session)
        self.add_event_handler("message", self._on_message)
        self.add_event_handler("message_error", self._on_message_error)
        self.add_event_handler("receipt_received", self._on_receipt)
        self.add_event_handler("marker_received", self._on_receipt)
        self.add_event_handler("marker_displayed", self._on_displayed)
        self.add_event_handler("chatstate", self._on_chatstate)
        self.add_event_handler("attention", self._on_attention)
        self.add_event_handler("failed_auth", self._failed_auth)
        self.add_event_handler("disconnected", self._disconnected)
        self.add_event_handler("roster_update", lambda event: self._emit_roster())
        self.add_event_handler("changed_status", lambda event: self._emit_roster())

    def connection_made(self, transport, send_event=True):
        ssl_object = transport.get_extra_info("ssl_object")
        if ssl_object and self.pinned_fingerprint:
            actual = fingerprint(ssl_object.getpeercert(binary_form=True))
            if actual != self.pinned_fingerprint:
                transport.abort()
                self.emit("error", "Сертификат сервера изменился во время подключения")
                self.loop.call_soon(self.loop.stop)
                return
        super().connection_made(transport, send_event=send_event)

    async def _on_session(self, event):
        self.plugin["xep_0047"].api.register(self._authorize_file, "authorized", default=True)
        self.send_presence()
        try:
            await self.get_roster(timeout=15)
        except Exception as exc:
            self.emit("error", f"Не удалось загрузить контакты: {exc}")
        self.emit("connected", self.boundjid.bare)
        self._emit_roster()

    def _failed_auth(self, event):
        self.emit("error", "Неверный логин или пароль")
        self.disconnect(wait=0)

    def _disconnected(self, reason):
        if not self._closing:
            self.emit("disconnected", str(reason or ""))
            self.loop.call_soon(self.loop.stop)

    def _emit_roster(self):
        contacts = {}
        for jid in self.client_roster.keys():
            item = self.client_roster[jid]
            contacts[str(jid)] = (
                item["name"] or str(jid), presence_status(item.resources),
                tuple(sorted(item["groups"] or ())),
            )
        self.emit("roster", contacts)

    def _on_message(self, message):
        offer = message["quark_file"]
        if offer["sid"]:
            self._on_file_offer(message, offer)
            return
        if message["type"] not in ("chat", "normal") or not message["body"]:
            return
        markable_id = str(message["id"]) if message["markable"] else ""
        self.emit("message", (message["from"].bare, str(message["body"]), markable_id))

    def _on_message_error(self, message):
        self.emit("delivery", (message["from"].bare, str(message["id"]), "failed"))

    def _on_receipt(self, message):
        message_id = str(message["receipt"] or message["received"]["id"])
        if message_id:
            self.emit("delivery", (message["from"].bare, message_id, "delivered"))

    def _on_displayed(self, message):
        message_id = str(message["displayed"]["id"])
        if message_id:
            self.emit("delivery", (message["from"].bare, message_id, "read"))

    def _on_chatstate(self, message):
        self.emit("chatstate", (message["from"].bare, str(message["chat_state"])))

    def _on_file_offer(self, message, offer):
        try:
            size = int(offer["size"])
        except (TypeError, ValueError):
            return
        sid = str(offer["sid"])
        if not sid or size < 1 or size > MAX_FILE or not offer["filename"]:
            return
        self.pending_files[sid] = (
            message["from"].bare, str(offer["filename"]), size,
            str(offer["sha256"]), str(message["id"]),
        )

    def _authorize_file(self, jid, sid, sender, iq):
        offer = self.pending_files.get(sid)
        return bool(offer and offer[0] == sender.bare)

    def _on_file_stream(self, stream):
        offer = self.pending_files.get(stream.sid)
        if offer and offer[0] == stream.peer_jid.bare:
            self.loop.create_task(self._receive_file(stream, offer))

    async def _receive_file(self, stream, offer):
        peer, name, size, digest, message_id = offer
        try:
            data = await stream.gather(max_data=MAX_FILE, timeout=600)
            if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
                raise ValueError("Неверный размер или контрольная сумма")
            self.emit("file_received", (peer, name, data, message_id))
        except Exception as exc:
            self.emit("file_error", f"Не удалось получить {name}: {exc}")
        finally:
            self.pending_files.pop(stream.sid, None)

    async def send_file(self, peer, data, filename, mime, sid):
        stream = None
        try:
            destination = peer
            resources = self.client_roster[peer].resources
            if resources:
                resource = max(resources, key=lambda name: resources[name].get("priority", 0))
                destination = f"{peer}/{resource}"
            message = self.make_message(mto=destination, mbody=f"📎 {filename}", mtype="chat")
            message["id"] = sid
            message["quark_file"]["sid"] = sid
            message["quark_file"]["filename"] = filename
            message["quark_file"]["size"] = str(len(data))
            message["quark_file"]["mime"] = mime
            message["quark_file"]["sha256"] = hashlib.sha256(data).hexdigest()
            message["markable"] = True
            message.send()
            stream = await asyncio.wait_for(
                self.plugin["xep_0047"].open_stream(JID(destination), sid=sid, block_size=4096), 20
            )
            await asyncio.wait_for(stream.sendall(data, timeout=20), 600)
            await stream.close(timeout=20)
            self.emit("delivery", (peer, sid, "delivered"))
        except asyncio.CancelledError:
            self.emit("delivery", (peer, sid, "failed"))
            raise
        except Exception as exc:
            if stream and not stream.stream_out_closed:
                try:
                    await stream.close(timeout=5)
                except Exception:
                    pass
            self.emit("file_error", f"Не удалось отправить {filename}: {exc}")
            self.emit("delivery", (peer, sid, "failed"))

    def _on_attention(self, message):
        self.emit("attention", message["from"].bare)


class Connection:
    def __init__(self, emit, certificates=None):
        self.emit = emit
        self.certificates = certificates or TrustedCertificates()
        self.loop = None
        self.client = None
        self.thread = None
        self._decision = threading.Event()
        self._approved = False

    def start(self, jid, password, host=None, port=5222):
        if self.thread and self.thread.is_alive():
            raise RuntimeError("Сначала отключитесь от текущего сервера")
        self._decision.clear()
        self._approved = False
        self.thread = threading.Thread(
            target=self._run, args=(jid, password, host, port), daemon=True
        )
        self.thread.start()

    def _run(self, jid, password, host, port):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop
        try:
            domain = JID(jid).domain
            address = host or domain
            pem, valid, reason = loop.run_until_complete(
                inspect_certificate(address, port, domain)
            )
            saved = self.certificates.get(domain, address, port)
            current = fingerprint(ssl.PEM_cert_to_DER_cert(pem))
            if not saved or saved["fingerprint"] != current:
                if not valid or saved:
                    self.emit("certificate", {
                        "host": address, "port": port, "domain": domain,
                        "fingerprint": current, "previous": saved["fingerprint"] if saved else None,
                        "reason": reason, "pem": pem,
                    })
                    if not self._decision.wait(120) or not self._approved:
                        self.emit("error", "Сертификат сервера не принят")
                        return
                    self.certificates.save(domain, address, port, pem)
                    saved = self.certificates.get(domain, address, port)
            self.client = ChatClient(jid, password, self.emit, pinned=saved)
            # connect() schedules the actual socket connection on this loop.
            attempt = self.client.connect(host=address, port=port)
            async def wait_for_connection():
                await asyncio.wait_for(attempt, timeout=20)
            loop.run_until_complete(wait_for_connection())
            loop.run_forever()
        except (Exception, asyncio.CancelledError) as exc:
            self.emit("error", f"Подключение не удалось: {exc}")
        finally:
            if self.client:
                self.client._closing = True
                loop.run_until_complete(self.client.disconnect(wait=0))
            tasks = asyncio.all_tasks(loop)
            for task in tasks:
                task.cancel()
            if tasks:
                loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
            loop.close()
            self.loop = None
            self.client = None

    def approve_certificate(self, approved):
        self._approved = approved
        self._decision.set()

    def send_message(self, peer, text, stanza_id=None):
        def send():
            try:
                message = self.client.make_message(mto=peer, mbody=text, mtype="chat")
                if stanza_id:
                    message["id"] = stanza_id
                message["markable"] = True
                message["request_receipt"] = True
                message["chat_state"] = "active"
                message.send()
            except Exception:
                if stanza_id:
                    self.emit("delivery", (peer, stanza_id, "failed"))
        self._schedule(send)

    def mark_read(self, peer, message_id):
        self._schedule(lambda: self.client.plugin["xep_0333"].send_marker(
            peer, message_id, "displayed", mtype="chat"
        ))

    def send_chatstate(self, peer, state):
        def send():
            message = self.client.make_message(mto=peer, mtype="chat")
            message["chat_state"] = state
            message.send()
        self._schedule(send)

    def send_messages(self, peers, text):
        def send_all():
            for recipient in peers:
                peer, stanza_id = recipient if isinstance(recipient, tuple) else (recipient, None)
                try:
                    message = self.client.make_message(mto=peer, mbody=text, mtype="chat")
                    if stanza_id:
                        message["id"] = stanza_id
                    message["markable"] = True
                    message["request_receipt"] = True
                    message.send()
                except Exception:
                    if stanza_id:
                        self.emit("delivery", (peer, stanza_id, "failed"))
        self._schedule(send_all)

    def send_attention(self, peer):
        self._schedule(lambda: self.client.plugin["xep_0224"].request_attention(peer))

    def send_file(self, peer, data, filename, mime, sid):
        self._schedule(lambda: self.loop.create_task(
            self.client.send_file(peer, data, filename, mime, sid)
        ))

    def _schedule(self, action):
        if not self.loop or not self.client or not self.loop.is_running():
            raise RuntimeError("Нет подключения к серверу")
        self.loop.call_soon_threadsafe(action)

    def stop(self):
        self._approved = False
        self._decision.set()
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
