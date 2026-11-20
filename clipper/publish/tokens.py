"""OAuth token storage (data/secrets/<platform>.json, owner-only permissions)."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path


class TokenStore:
    def __init__(self, secrets_dir: Path):
        self.dir = secrets_dir

    def _path(self, platform: str) -> Path:
        return self.dir / f"{platform}.json"

    def load(self, platform: str) -> dict | None:
        p = self._path(platform)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            return None

    def save(self, platform: str, data: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        p = self._path(platform)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(p)

    def delete(self, platform: str) -> None:
        self._path(platform).unlink(missing_ok=True)


def expires_at(expires_in) -> float:
    return time.time() + float(expires_in or 3600) - 60
