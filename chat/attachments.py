"""Small local-only file cache; originals are never removed by cleanup."""

import os
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from .xmpp import MAX_FILE


class FileStore:
    def __init__(self, directory=None):
        self.directory = Path(directory or Path.home() / ".local" / "share" /
                              "openfire-chat" / "attachments")
        self.directory.mkdir(parents=True, exist_ok=True)

    def save(self, filename, data):
        if not 0 < len(data) <= MAX_FILE:
            raise ValueError("Размер файла должен быть от 1 байта до 5 МБ")
        extension = Path(filename).suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,10}", extension):
            extension = ".bin"
        path = self.directory / (uuid.uuid4().hex + extension)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
        expires = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        return path, expires
