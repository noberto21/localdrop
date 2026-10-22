"""Settings, paths and persistence for LocalDrop."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

APP_NAME = "LocalDrop"
VERSION = "1.0.0"

BASE_DIR = Path(__file__).resolve().parent
# LOCALDROP_HOME relocates uploads/, shared/ and data/ (used by selftest.py).
HOME_DIR = Path(os.environ.get("LOCALDROP_HOME", BASE_DIR)).resolve()
DATA_DIR = HOME_DIR / "data"
CONFIG_FILE = DATA_DIR / "config.json"
HISTORY_FILE = DATA_DIR / "history.json"
LOG_FILE = DATA_DIR / "localdrop.log"


class SettingsError(ValueError):
    """Raised when a setting is invalid; the message is safe to show to the user."""


def _check_dir(value: str, label: str, *, shared: bool) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SettingsError(f"{label} cannot be empty.")
    p = Path(os.path.expandvars(os.path.expanduser(value.strip()))).resolve()
    if p.parent == p:
        raise SettingsError(f"{label} cannot be a drive or filesystem root.")
    if shared and p == Path.home().resolve():
        raise SettingsError("Share a specific folder, not your whole user profile.")
    if p == BASE_DIR or p in BASE_DIR.parents:
        raise SettingsError(f"{label} must not contain the LocalDrop application folder.")
    if p == DATA_DIR or DATA_DIR in p.parents:
        raise SettingsError(f"{label} cannot be inside LocalDrop's data folder.")
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SettingsError(f"{label} could not be created: {exc.strerror or exc}") from exc
    if not p.is_dir():
        raise SettingsError(f"{label} is not a folder.")
    if not os.access(p, os.W_OK if not shared else os.R_OK):
        raise SettingsError(f"{label} is not accessible with the current permissions.")
    return str(p)


@dataclass
class Settings:
    port: int = 8765
    upload_dir: str = str(HOME_DIR / "uploads")
    shared_dir: str = str(HOME_DIR / "shared")
    max_file_mb: int = 10240      # 0 = unlimited
    max_concurrent: int = 3
    preferred_ip: str = ""

    @classmethod
    def load(cls) -> "Settings":
        s = cls()
        try:
            raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            known = {f.name for f in fields(cls)}
            for k, v in raw.items():
                if k in known:
                    setattr(s, k, v)
        except (OSError, ValueError):
            pass
        # Re-validate stored folders; fall back to defaults if they became invalid.
        for name, shared in (("upload_dir", False), ("shared_dir", True)):
            try:
                setattr(s, name, _check_dir(getattr(s, name), name, shared=shared))
            except SettingsError:
                default = getattr(cls(), name)
                Path(default).mkdir(parents=True, exist_ok=True)
                setattr(s, name, default)
        return s

    def save(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        os.replace(tmp, CONFIG_FILE)

    def update(self, data: dict) -> None:
        """Validate every supplied field first, then apply them all (or none)."""
        new: dict = {}
        if "upload_dir" in data:
            new["upload_dir"] = _check_dir(data["upload_dir"], "Upload folder", shared=False)
        if "shared_dir" in data:
            new["shared_dir"] = _check_dir(data["shared_dir"], "Shared folder", shared=True)
        if "max_file_mb" in data:
            try:
                v = int(data["max_file_mb"])
            except (TypeError, ValueError):
                raise SettingsError("Maximum file size must be a number of megabytes.")
            if v < 0 or v > 10_000_000:
                raise SettingsError("Maximum file size must be 0 (no limit) or a positive number.")
            new["max_file_mb"] = v
        if "max_concurrent" in data:
            try:
                v = int(data["max_concurrent"])
            except (TypeError, ValueError):
                raise SettingsError("Concurrent transfers must be a number.")
            if not 1 <= v <= 16:
                raise SettingsError("Concurrent transfers must be between 1 and 16.")
            new["max_concurrent"] = v
        if "port" in data:
            try:
                v = int(data["port"])
            except (TypeError, ValueError):
                raise SettingsError("Port must be a number.")
            if not 1024 <= v <= 65535:
                raise SettingsError("Port must be between 1024 and 65535.")
            new["port"] = v
        if "preferred_ip" in data:
            new["preferred_ip"] = str(data["preferred_ip"] or "")[:64]
        for k, v in new.items():
            setattr(self, k, v)

    @property
    def max_bytes(self) -> int:
        return self.max_file_mb * 1024 * 1024 if self.max_file_mb > 0 else 0


settings = Settings.load()
