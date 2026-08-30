"""
secure_storage.py

Encrypts the Twilio Auth Token before it's written to config.json, using a
key derived from this machine's identity. The key itself is never stored —
it's re-derived each run from stable local machine identifiers, combined
with a random per-value salt that IS stored (salts aren't secret).

This means config.json can be freely looked at or backed up, but the
encrypted token can only be decrypted again on the same machine. Copying
config.json to another computer (or a fresh OS install) will fail to
decrypt, and the app will simply ask for the Auth Token again — it will
never silently produce garbage credentials.

This is a convenience measure, not a substitute for keeping the Twilio
Auth Token itself secret. Anyone with access to the machine while logged
in as the same user could, in principle, run this same derivation.
"""

from __future__ import annotations

import base64
import hashlib
import platform
import subprocess
import uuid
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

KDF_ITERATIONS = 390_000


class DecryptionError(RuntimeError):
    """Raised when a stored value can't be decrypted on this machine."""


def _read_first_line(path: str) -> str | None:
    try:
        text = Path(path).read_text().strip()
        return text or None
    except OSError:
        return None


def _machine_id() -> str:
    """Best-effort stable machine identifier, OS-appropriate."""
    system = platform.system()

    if system == "Linux":
        for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            value = _read_first_line(path)
            if value:
                return value

    elif system == "Darwin":
        try:
            output = subprocess.run(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True, text=True, timeout=5, check=True,
            ).stdout
            for line in output.splitlines():
                if "IOPlatformUUID" in line:
                    return line.split('"')[-2]
        except Exception:
            pass

    elif system == "Windows":
        try:
            output = subprocess.run(
                ["reg", "query", r"HKLM\SOFTWARE\Microsoft\Cryptography", "/v", "MachineGuid"],
                capture_output=True, text=True, timeout=5, check=True,
            ).stdout
            for line in output.splitlines():
                if "MachineGuid" in line:
                    return line.strip().split()[-1]
        except Exception:
            pass

    # Fallback available on every platform, though slightly less stable
    # (changes if the network adapter providing the MAC address changes).
    return f"{platform.node()}-{uuid.getnode()}"


def _derive_key(salt: bytes) -> bytes:
    material = _machine_id().encode("utf-8")
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32, salt=salt, iterations=KDF_ITERATIONS
    )
    return base64.urlsafe_b64encode(kdf.derive(material))


def encrypt(plaintext: str) -> dict:
    """Returns {"salt": ..., "ciphertext": ...}, both base64 text, JSON-safe."""
    salt = hashlib.sha256(uuid.uuid4().bytes).digest()[:16]
    key = _derive_key(salt)
    ciphertext = Fernet(key).encrypt(plaintext.encode("utf-8"))
    return {
        "salt": base64.b64encode(salt).decode("ascii"),
        "ciphertext": ciphertext.decode("ascii"),
    }


def decrypt(blob: dict) -> str:
    """Raises DecryptionError if this isn't the machine that encrypted it."""
    try:
        salt = base64.b64decode(blob["salt"])
        key = _derive_key(salt)
        plaintext = Fernet(key).decrypt(blob["ciphertext"].encode("ascii"))
        return plaintext.decode("utf-8")
    except (InvalidToken, KeyError, ValueError) as e:
        raise DecryptionError(
            "Saved Auth Token could not be decrypted on this machine."
        ) from e
