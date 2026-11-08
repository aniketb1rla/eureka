"""Private file storage. Nothing is publicly readable; the API checks ownership first."""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Protocol
from urllib.parse import quote

import httpx

from . import secrets_store
from .config import Settings


class Storage(Protocol):
    def put(self, key: str, data: bytes, mime: str) -> None: ...
    def get(self, key: str) -> bytes: ...
    def delete(self, key: str) -> None: ...
    def local_path(self, key: str) -> Optional[Path]: ...
    def signed_url(self, key: str, expires: int = 300, download: Optional[str] = None) -> Optional[str]: ...


class LocalStorage:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # Checked from the text alone. Path.resolve() can return a different form on Windows while another
        # thread is creating the same folder, which rejected valid uploads under load.
        parts = key.split("/")
        if not key or "\\" in key or ":" in key or any(part in ("", ".", "..") for part in parts):
            raise ValueError("bad storage key")
        return self.root.joinpath(*parts)

    def put(self, key, data, mime):
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def get(self, key):
        return self._path(key).read_bytes()

    def delete(self, key):
        self._path(key).unlink(missing_ok=True)

    def local_path(self, key):
        return self._path(key)

    def signed_url(self, key, expires=300, download=None):
        return None


class SupabaseStorage:
    def __init__(self, s: Settings):
        self._base = f"{s.supabase_url}/storage/v1"
        self._bucket = s.supabase_bucket
        self._service_key = secrets_store.load_supabase_service_key(s)

    def __repr__(self) -> str:
        return "<SupabaseStorage>"

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._service_key}", "apikey": self._service_key}

    def put(self, key, data, mime):
        r = httpx.post(f"{self._base}/object/{self._bucket}/{key}", content=data, timeout=120,
                       headers={**self._headers(), "Content-Type": mime, "x-upsert": "true"})
        r.raise_for_status()

    def get(self, key):
        r = httpx.get(f"{self._base}/object/{self._bucket}/{key}", headers=self._headers(), timeout=120)
        r.raise_for_status()
        return r.content

    def delete(self, key):
        httpx.request("DELETE", f"{self._base}/object/{self._bucket}", json={"prefixes": [key]},
                      headers=self._headers(), timeout=30)

    def local_path(self, key):
        return None

    def signed_url(self, key, expires=300, download=None):
        r = httpx.post(f"{self._base}/object/sign/{self._bucket}/{key}", json={"expiresIn": expires},
                       headers=self._headers(), timeout=30)
        r.raise_for_status()
        url = f"{self._base}{r.json()['signedURL']}"
        return f"{url}&download={quote(download)}" if download else url


def build_storage(s: Settings) -> Storage:
    return SupabaseStorage(s) if s.storage_backend == "supabase" else LocalStorage(s.data_dir / "files")
