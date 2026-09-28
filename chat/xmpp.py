"""Slixmpp runs on its own asyncio loop; UI callbacks are dispatched by the caller."""

import asyncio
import ssl
import threading

from slixmpp import ClientXMPP
from slixmpp.jid import JID

from .certificates import TrustedCertificates, fingerprint, inspect_certificate


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
        self.add_event_handler("session_start", self._on_session)
        self.add_event_handler("message", self._on_message)
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
            contacts[str(jid)] = (item["name"] or str(jid), presence_status(item.resources))
        self.emit("roster", contacts)

    def _on_message(self, message):
        if message["type"] not in ("chat", "normal") or not message["body"]:
            return
        self.emit("message", (message["from"].bare, str(message["body"])))

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

    def send_message(self, peer, text):
        self._schedule(lambda: self.client.send_message(mto=peer, mbody=text, mtype="chat"))

    def send_attention(self, peer):
        self._schedule(lambda: self.client.plugin["xep_0224"].request_attention(peer))

    def _schedule(self, action):
        if not self.loop or not self.client or not self.loop.is_running():
            raise RuntimeError("Нет подключения к серверу")
        self.loop.call_soon_threadsafe(action)

    def stop(self):
        self._approved = False
        self._decision.set()
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
