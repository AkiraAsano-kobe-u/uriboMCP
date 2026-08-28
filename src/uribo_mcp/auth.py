"""Credential and cookie storage for unattended desktop MCP processes."""

from __future__ import annotations

import base64
import ctypes
import getpass
import json
import os
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from dotenv import dotenv_values

from uribo_mcp.errors import AuthenticationRequired

KEYRING_SERVICE = "uribo-mcp"
USERNAME_KEY = "kobe-username"
PASSWORD_KEY = "kobe-password"


class SecretStore(Protocol):
    def get(self, key: str) -> str | None: ...

    def set(self, key: str, value: str) -> None: ...

    def delete(self, key: str) -> None: ...


class KeyringSecretStore:
    """OS credential manager backed by the ``keyring`` package."""

    def __init__(self, service: str = KEYRING_SERVICE):
        self.service = service

    def get(self, key: str) -> str | None:
        import keyring

        return keyring.get_password(self.service, key)

    def set(self, key: str, value: str) -> None:
        import keyring

        keyring.set_password(self.service, key, value)

    def delete(self, key: str) -> None:
        import keyring
        from keyring.errors import PasswordDeleteError

        try:
            keyring.delete_password(self.service, key)
        except PasswordDeleteError:
            pass


@dataclass(frozen=True)
class KobeCredentials:
    username: str
    password: str


def load_credentials(
    *,
    secret_store: SecretStore | None = None,
    env_path: Path | None = None,
) -> KobeCredentials:
    """Prefer the OS keyring and retain a gitignored .env compatibility fallback."""
    store = secret_store or KeyringSecretStore()
    username = store.get(USERNAME_KEY)
    password = store.get(PASSWORD_KEY)
    if not username or not password:
        env = dict(dotenv_values(env_path)) if env_path and env_path.exists() else {}
        username = username or os.environ.get("KOBE_USERNAME") or env.get("KOBE_USERNAME")
        password = password or os.environ.get("KOBE_PASSWORD") or env.get("KOBE_PASSWORD")
    if not username or not password:
        raise AuthenticationRequired(
            "Kobe credentials are not configured. Run `uribo-mcp auth setup`."
        )
    return KobeCredentials(str(username), str(password))


def interactive_setup(secret_store: SecretStore | None = None) -> None:
    """Prompt in a terminal, never through MCP stdout."""
    store = secret_store or KeyringSecretStore()
    username = input("Kobe University username: ").strip()
    password = getpass.getpass("Kobe University password: ")
    if not username or not password:
        raise AuthenticationRequired("Username and password must not be empty")
    store.set(USERNAME_KEY, username)
    store.set(PASSWORD_KEY, password)


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data)
    blob = _DataBlob(
        len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))
    )
    return blob, buffer


def dpapi_protect(data: bytes, *, description: str = "uriboMCP cookies") -> bytes:
    """Protect bytes for the current Windows user using DPAPI."""
    if os.name != "nt":
        raise OSError("DPAPI is only available on Windows")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    in_blob, in_buffer = _blob(data)
    out_blob = _DataBlob()
    # CRYPTPROTECT_UI_FORBIDDEN prevents an unexpected dialog in a headless MCP process.
    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        description,
        None,
        None,
        None,
        0x1,
        ctypes.byref(out_blob),
    )
    del in_buffer
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def dpapi_unprotect(data: bytes) -> bytes:
    """Decrypt bytes protected by :func:`dpapi_protect`."""
    if os.name != "nt":
        raise OSError("DPAPI is only available on Windows")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    in_blob, in_buffer = _blob(data)
    out_blob = _DataBlob()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        0x1,
        ctypes.byref(out_blob),
    )
    del in_buffer
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


class CookieVault:
    """DPAPI-encrypted JSON cookie files.

    On non-Windows systems the fallback is an explicitly marked base64 file with
    user-only permissions where supported. Windows is the primary supported host.
    """

    def __init__(self, directory: Path):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, source: str) -> Path:
        safe = "".join(ch for ch in source if ch.isalnum() or ch in "-_")
        if not safe:
            raise ValueError("Invalid cookie source")
        return self.directory / f"{safe}.cookies"

    def save(self, source: str, cookies: list[dict]) -> None:
        raw = json.dumps(cookies, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if os.name == "nt":
            payload = b"DPAPI1\n" + base64.b64encode(dpapi_protect(raw))
        else:
            payload = b"PLAIN1\n" + base64.b64encode(raw)
        path = self._path(source)
        temp = path.with_suffix(".tmp")
        temp.write_bytes(payload)
        try:
            os.chmod(temp, 0o600)
        except OSError:
            pass
        temp.replace(path)

    def load(self, source: str) -> list[dict] | None:
        path = self._path(source)
        if not path.exists():
            return None
        marker, encoded = path.read_bytes().split(b"\n", 1)
        payload = base64.b64decode(encoded)
        if marker == b"DPAPI1":
            payload = dpapi_unprotect(payload)
        elif marker != b"PLAIN1":
            raise AuthenticationRequired(f"Unsupported cookie vault format: {path}")
        value = json.loads(payload.decode("utf-8"))
        if not isinstance(value, list):
            raise AuthenticationRequired(f"Invalid cookie data: {path}")
        return value

    def delete(self, source: str) -> None:
        self._path(source).unlink(missing_ok=True)

