#!/usr/bin/env python3
"""Launch with the project's environment when called via system Python."""

import importlib.util
import os
import sys
from pathlib import Path


if importlib.util.find_spec("slixmpp") is None:
    local_python = Path(__file__).resolve().parent / ".venv" / "bin" / "python"
    if local_python.is_file() and sys.prefix != str(local_python.parent.parent):
        os.execv(str(local_python), [str(local_python), str(Path(__file__).resolve()), *sys.argv[1:]])
    raise SystemExit(
        "Не найден slixmpp. Установите зависимости: "
        "python3 -m venv --system-site-packages .venv && "
        ".venv/bin/pip install -r requirements.txt"
    )

from chat.app import main


if __name__ == "__main__":
    raise SystemExit(main())
