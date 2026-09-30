"""Account preferences and desktop autostart; passwords live in Secret Service."""

import json
import os
from pathlib import Path


class Preferences:
    def __init__(self, path=None, autostart=None):
        config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        self.path = Path(path) if path else config / "quark" / "account.json"
        self.autostart = Path(autostart) if autostart else config / "autostart" / "org.example.Quark.desktop"

    def load(self):
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def save(self, jid, host, port, autostart):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump({"jid": jid, "host": host, "port": port, "autostart": autostart}, output)
        os.replace(temp, self.path)

    def set_autostart(self, enabled):
        if not enabled:
            self.autostart.unlink(missing_ok=True)
            return
        self.autostart.parent.mkdir(parents=True, exist_ok=True)
        main = Path(__file__).resolve().parent.parent / "main.py"
        if main == Path("/usr/share/quark/main.py"):
            command = "quark"
        else:
            escaped = str(main).replace("\\", "\\\\").replace('"', '\\"')
            command = f'/usr/bin/python3 "{escaped}"'
        self.autostart.write_text(
            "[Desktop Entry]\nType=Application\nName=Quark\n"
            f"Exec={command}\nIcon=org.example.Quark\n"
            "Terminal=false\nX-GNOME-Autostart-enabled=true\n",
            encoding="utf-8",
        )


def _schema():
    import gi
    gi.require_version("Secret", "1")
    from gi.repository import Secret
    return Secret, Secret.Schema.new(
        "org.example.Quark", Secret.SchemaFlags.NONE,
        {"jid": Secret.SchemaAttributeType.STRING},
    )


def store_password(jid, password):
    Secret, schema = _schema()
    if not Secret.password_store_sync(
        schema, {"jid": jid}, Secret.COLLECTION_DEFAULT, "Quark XMPP", password, None
    ):
        raise RuntimeError("Не удалось сохранить пароль в системной ключнице")


def load_password(jid):
    Secret, schema = _schema()
    return Secret.password_lookup_sync(schema, {"jid": jid}, None)


def clear_password(jid):
    Secret, schema = _schema()
    Secret.password_clear_sync(schema, {"jid": jid}, None)
