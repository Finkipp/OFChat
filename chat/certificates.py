"""STARTTLS certificate inspection and per-server certificate pinning."""

import asyncio
import hashlib
import json
import os
import re
import ssl
from pathlib import Path


def fingerprint(der):
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


class TrustedCertificates:
    def __init__(self, path=None):
        self.path = Path(path) if path else Path(os.environ.get(
            "XDG_CONFIG_HOME", Path.home() / ".config"
        )) / "openfire-chat" / "certificates.json"

    def get(self, domain, host, port):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        return data.get(self._key(domain, host, port))

    def save(self, domain, host, port, pem):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {}
        data[self._key(domain, host, port)] = {
            "pem": pem,
            "fingerprint": fingerprint(ssl.PEM_cert_to_DER_cert(pem)),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(data, output, ensure_ascii=False, indent=2)
        os.replace(temporary, self.path)

    @staticmethod
    def _key(domain, host, port):
        return f"{domain.lower()}|{host.lower()}|{port}"


async def _read_element(reader, name):
    """Read only the short, unauthenticated stream header / TLS response."""
    end = re.compile(rb"</(?:[\w-]+:)?" + name + rb"\s*>" +
                     rb"|<(?:[\w-]+:)?" + name + rb"\b[^>]*?/>")
    data = b""
    while not end.search(data):
        chunk = await asyncio.wait_for(reader.read(4096), timeout=10)
        if not chunk or len(data) + len(chunk) > 65536:
            raise ValueError("Некорректный ответ XMPP-сервера")
        data += chunk
    return data


async def _probe_once(host, port, domain, verify):
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=10)
    try:
        writer.write((
            f"<stream:stream to='{domain}' xmlns='jabber:client' "
            "xmlns:stream='http://etherx.jabber.org/streams' version='1.0'>"
        ).encode("utf-8"))
        await writer.drain()
        features = await _read_element(reader, b"features")
        if b"urn:ietf:params:xml:ns:xmpp-tls" not in features or b"starttls" not in features:
            raise ValueError("Сервер не предлагает XMPP STARTTLS")
        writer.write(b"<starttls xmlns='urn:ietf:params:xml:ns:xmpp-tls'/>")
        await writer.drain()
        reply = await _read_element(reader, b"proceed")
        if b"<proceed" not in reply and b":proceed" not in reply:
            raise ValueError("Сервер отклонил STARTTLS")
        context = ssl.create_default_context()
        if not verify:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        await asyncio.wait_for(writer.start_tls(context, server_hostname=domain), timeout=10)
        der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
        return ssl.DER_cert_to_PEM_cert(der)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, ssl.SSLError):
            pass


async def inspect_certificate(host, port, domain):
    """Return (PEM, system_trusted, verification_error). Never send credentials."""
    try:
        return await _probe_once(host, port, domain, True), True, ""
    except ssl.SSLCertVerificationError as exc:
        return await _probe_once(host, port, domain, False), False, str(exc)
