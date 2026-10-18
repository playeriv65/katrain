"""Constants and helpers shared by the GUI-side server and the CLI client. No Kivy imports allowed here."""

import json
import os

API_VERSION = 1
DEFAULT_PORT = 8765
PORT_SEARCH_RANGE = 10
COMMAND_PATH = "/v1/command"
HEALTH_PATH = "/v1/health"
TOKEN_HEADER = "X-KaTrain-Token"

DATA_FOLDER = os.path.expanduser("~/.katrain")
DISCOVERY_FILE = os.path.join(DATA_FOLDER, "api.json")


class ApiError(Exception):
    """Error reported back to the client as {"ok": false, "error": {...}}."""

    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_json(self):
        return {"code": self.code, "message": self.message, **self.details}


def read_discovery(path=DISCOVERY_FILE):
    """Return the connection info written by a running GUI, or None."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_discovery(info, path=DISCOVERY_FILE):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(info, f, indent=2)
    os.replace(tmp, path)


def remove_discovery(pid, path=DISCOVERY_FILE):
    """Remove the discovery file, but only if it still belongs to this process."""
    info = read_discovery(path)
    if info and info.get("pid") == pid:
        try:
            os.remove(path)
        except OSError:
            pass
