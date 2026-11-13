"""Edit the target list in the YAML config from the API.

Every change is validated by loading the whole resulting config with the same
parser used at startup, so the dashboard can never write a file that Vigil
would refuse to start with. Writes are atomic (temp file + rename) so a crash
mid-write leaves the previous config intact.
"""

from __future__ import annotations

import copy
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

import yaml

from .config import PROBE_TYPES, ConfigError, Target, parse_config

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
OPTION_KEYS = {
    "http": {"expect_status", "expect_body", "cert_warn_days", "method", "follow_redirects", "verify_tls"},
    "dns": {"server", "record", "expect"},
    "icmp": {"count"},
    "tcp": set(),
}
FIELDS = ("interval", "timeout", "fail_after", "recover_after", "latency_slo_ms", "availability_slo")


def clean_target(data: Any) -> dict:
    """Reduce untrusted input to the fields a target may have."""
    if not isinstance(data, dict):
        raise ConfigError("target must be an object")
    name, kind, address = str(data.get("name", "")).strip(), data.get("type"), str(data.get("address", "")).strip()
    if not NAME.match(name):
        raise ConfigError("name: letters, digits, '.', '_' or '-', up to 64 characters")
    if kind not in PROBE_TYPES:
        raise ConfigError(f"type must be one of {', '.join(PROBE_TYPES)}")
    if not address or len(address) > 255:
        raise ConfigError("address is required")
    if kind == "http" and not address.startswith(("http://", "https://")):
        raise ConfigError("http address must start with http:// or https://")

    out: dict[str, Any] = {"name": name, "type": kind, "address": address}
    for key in FIELDS:
        if data.get(key) not in (None, ""):
            out[key] = data[key]
    options = {k: v for k, v in (data.get("options") or {}).items() if k in OPTION_KEYS[kind] and v not in (None, "", [])}
    if options:
        out["options"] = options
    labels = {str(k): str(v) for k, v in (data.get("labels") or {}).items() if v not in (None, "")}
    if labels:
        out["labels"] = labels
    return out


class ConfigFile:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _read(self) -> dict:
        with self.path.open(encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}

    def _write(self, raw: dict) -> None:
        backup = self.path.with_suffix(self.path.suffix + ".bak")
        if not backup.exists():
            # The first API write drops comments; keep the hand-written original.
            shutil.copy2(self.path, backup)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".vigil-", suffix=".yml")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            yaml.safe_dump(raw, fh, allow_unicode=True, sort_keys=False)
        os.replace(tmp, self.path)

    def _commit(self, raw: dict, name: str) -> Target:
        config = parse_config(raw)  # raises ConfigError, file untouched
        self._write(raw)
        return next(t for t in config.targets if t.name == name)

    def add(self, data: Any) -> Target:
        target = clean_target(data)
        raw = copy.deepcopy(self._read())
        if any(t.get("name") == target["name"] for t in raw.get("targets") or []):
            raise ConfigError(f"a target named {target['name']} already exists")
        raw.setdefault("targets", []).append(target)
        return self._commit(raw, target["name"])

    def replace(self, name: str, data: Any) -> Target:
        target = clean_target(data)
        raw = copy.deepcopy(self._read())
        targets = raw.get("targets") or []
        idx = next((i for i, t in enumerate(targets) if t.get("name") == name), None)
        if idx is None:
            raise KeyError(name)
        if target["name"] != name and any(t.get("name") == target["name"] for t in targets):
            raise ConfigError(f"a target named {target['name']} already exists")
        targets[idx] = target
        return self._commit(raw, target["name"])

    def remove(self, name: str) -> None:
        raw = copy.deepcopy(self._read())
        targets = raw.get("targets") or []
        if not any(t.get("name") == name for t in targets):
            raise KeyError(name)
        raw["targets"] = [t for t in targets if t.get("name") != name]
        if not raw["targets"]:
            raise ConfigError("cannot remove the last target")
        parse_config(raw)
        self._write(raw)
