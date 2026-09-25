"""End-to-end sealing for LAN context blobs — the codec above the store.

The LAN context bus lets several machines share one context store through
``context-busd``. The daemon and the network must only ever hold ciphertext, so
every blob is sealed *client-side* with a fleet pre-shared key (PSK) before it
leaves the machine that wrote it, and unsealed only by a machine that holds the
keyring. The daemon needs no key at all.

Design (fixed by the LAN-bus plan, 2026-09-12):

- **Armor**: ``afctx1:`` + base64 of ``ver(1) || kid(1) || wlen(2 BE) || writer
  || ts(8 BE) || salt(16) || nonce(12) || ct+tag``. Text-safe, so it fits the
  existing ``value TEXT`` column with no schema change.
- **Header** (ver/kid/writer/ts) is cleartext but authenticated: it is included
  in the AEAD associated data, so a holder cannot re-label, re-date, or
  re-attribute a blob without the key. Writer/ts live in the header so an
  envelope is self-describing — a reader that holds the keyring can unseal it
  with nothing but the storage key name, which is what makes the rollback guard
  work when a sealed blob turns up in a local store.
- **Key derivation**: ``K = HKDF-SHA256(psk, salt=<fresh 16B per seal>,
  info=b"ask-fable/ctx/v1/blob")``. A fresh salt on every seal (including
  overwrites) gives every blob its own key, so 96-bit random nonces cannot
  collide across blobs.
- **Nonce**: fresh ``os.urandom(12)`` per seal. Never a counter (machines share
  no counter state), never derived from the key name or timestamp.
- **Plaintext**: JSON ``{"v": value, "d": description}`` — the description is
  content and stays inside the sealed body.
- **AAD**: length-prefixed ``ver | kid | key_name | writer | ts_ns``; the
  storage key name is bound at seal time and must be supplied again at unseal,
  so ciphertext cannot be lifted from one key to another.

Cryptography is imported lazily: the base install stays dependency-light and a
LAN feature refuses cleanly (``CryptoUnavailable``) when ``ask-fable[lan]`` is
not installed. The keyring lives in a 0600 file
(``ASK_FABLE_CONTEXT_KEYRING``, default ``~/.config/ask_fable/context_keyring``)
with one ``kid:base64(32 bytes)`` per line, newest first; the first line seals,
every listed kid can unseal, so rotation has no flag day and a retired machine
is cut off by removing its kid after a reseal.

Best-effort discipline matches the rest of ask_fable: callers on the store path
translate every raised ``CryptoError`` into a degraded-store signal — a sealed
blob that cannot be unsealed is never silently returned and never spliced into
a prompt.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from pathlib import Path

from . import _paths

MAGIC = "afctx1:"
VER = 1
SALT_LEN = 16
NONCE_LEN = 12
KEY_LEN = 32
TAG_LEN = 16
_WRITER_MAX = 0xFFFF
_AAD_INFO = b"ask-fable/ctx/v1/blob"
# ver + kid + wlen + ts + salt + nonce + tag, with an empty writer field.
_MIN_ENVELOPE = 1 + 1 + 2 + 8 + SALT_LEN + NONCE_LEN + TAG_LEN


class CryptoError(RuntimeError):
    """Base class for every codec failure — safe to catch as a group."""


class CryptoUnavailable(CryptoError):
    """The optional ``cryptography`` dependency is not installed."""


class KeyringError(CryptoError):
    """The keyring file is missing, unreadable, loose, or malformed."""


class SealedError(CryptoError):
    """A sealed envelope could not be parsed or opened."""


class UnknownKidError(SealedError):
    """The envelope names a kid this keyring does not hold."""


class CorruptEnvelopeError(SealedError):
    """The armor, header, or AEAD tag is invalid."""


def keyring_path() -> Path:
    """Resolved keyring path (may not exist — absence is a hard refusal)."""
    override = os.environ.get("ASK_FABLE_CONTEXT_KEYRING")
    if override:
        return Path(override).expanduser()
    return _paths.xdg_config_dir() / "ask_fable" / "context_keyring"


class Keyring:
    """Parsed keyring: an ordered list of ``(kid, psk)``, first line active.

    The active (first) entry seals; any entry can unseal, so a rotation is
    "prepend the new kid, keep the old line until resealed".
    """

    def __init__(self, entries: list[tuple[int, bytes]]):
        self._entries = list(entries)

    def active(self) -> tuple[int, bytes]:
        return self._entries[0]

    def get(self, kid: int) -> bytes | None:
        for k, psk in self._entries:
            if k == kid:
                return psk
        return None

    def kids(self) -> list[int]:
        return [k for k, _ in self._entries]


def load_keyring() -> Keyring:
    """Read and validate the keyring. Raises ``KeyringError`` (never returns a
    partial/empty keyring): an absent key is a refusal, not a silent no-op."""
    p = keyring_path()
    try:
        st = p.stat()
    except OSError as exc:
        raise KeyringError(f"keyring not readable: {p} ({exc})") from exc
    if st.st_mode & 0o077:
        raise KeyringError(
            f"keyring {p} is group/other-accessible "
            f"(mode {oct(st.st_mode & 0o777)}); chmod 600 it"
        )
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise KeyringError(f"keyring read failed: {p} ({exc})") from exc

    entries: list[tuple[int, bytes]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        kid_str, sep, key_str = s.partition(":")
        if not sep:
            raise KeyringError(f"keyring line {lineno}: expected 'kid:base64'")
        try:
            kid = int(kid_str.strip(), 10)
        except ValueError as exc:
            raise KeyringError(f"keyring line {lineno}: kid must be an integer") from exc
        if not (0 <= kid <= 255):
            raise KeyringError(f"keyring line {lineno}: kid must be 0..255")
        try:
            psk = base64.b64decode(key_str.strip(), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise KeyringError(f"keyring line {lineno}: key is not valid base64") from exc
        if len(psk) != KEY_LEN:
            raise KeyringError(f"keyring line {lineno}: key must be {KEY_LEN} bytes")
        entries.append((kid, psk))
    if not entries:
        raise KeyringError(f"keyring is empty: {p}")
    kids = [k for k, _ in entries]
    if len(set(kids)) != len(kids):
        raise KeyringError(f"keyring has duplicate kids: {p}")
    return Keyring(entries)


def _require_crypto():
    """Import the optional primitive lazily; a missing extra is a named error,
    never an ImportError at module import time."""
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
        raise CryptoUnavailable(
            "the 'cryptography' package is required for LAN context blobs; "
            "install ask-fable[lan]"
        ) from exc
    return ChaCha20Poly1305, HKDF, hashes


def _hkdf(psk: bytes, salt: bytes) -> bytes:
    ChaCha20Poly1305, HKDF, hashes = _require_crypto()
    return HKDF(
        algorithm=hashes.SHA256(), length=KEY_LEN, salt=salt, info=_AAD_INFO
    ).derive(psk)


def _lp(b: bytes) -> bytes:
    return len(b).to_bytes(4, "big") + b


def _aad(ver: int, kid: int, key_name: str, writer: str, ts_ns: int) -> bytes:
    """Length-prefixed AAD. Every field is framed, so ``("a|b", "c")`` and
    ``("a", "b|c")`` cannot collide, and ``ts_ns`` is normalized to an integer
    so reconstruction is byte-stable across JSON/SQLite round-trips."""
    return b"".join(
        (
            _lp(bytes([ver])),
            _lp(bytes([kid])),
            _lp(key_name.encode("utf-8", "surrogatepass")),
            _lp(writer.encode("utf-8", "surrogatepass")),
            _lp(str(int(ts_ns)).encode("ascii")),
        )
    )


def _pack(
    ver: int, kid: int, writer: str, ts_ns: int, salt: bytes, nonce: bytes, ct: bytes
) -> bytes:
    w = writer.encode("utf-8", "surrogatepass")
    if len(w) > _WRITER_MAX:
        raise SealedError("writer id too long for the envelope header")
    return (
        bytes([ver, kid])
        + len(w).to_bytes(2, "big")
        + w
        + int(ts_ns).to_bytes(8, "big")
        + salt
        + nonce
        + ct
    )


def _parse(armored: object) -> tuple[int, int, str, int, bytes, bytes, bytes]:
    if not isinstance(armored, str) or not armored.startswith(MAGIC):
        raise CorruptEnvelopeError("missing afctx1: armor")
    try:
        env = base64.b64decode(armored[len(MAGIC):], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CorruptEnvelopeError("armor is not valid base64") from exc
    if len(env) < _MIN_ENVELOPE:
        raise CorruptEnvelopeError("envelope is too short")
    ver, kid = env[0], env[1]
    wlen = int.from_bytes(env[2:4], "big")
    off = 4
    if off + wlen + 8 + SALT_LEN + NONCE_LEN + TAG_LEN > len(env):
        raise CorruptEnvelopeError("envelope is truncated")
    writer = env[off:off + wlen].decode("utf-8", "surrogatepass")
    off += wlen
    ts_ns = int.from_bytes(env[off:off + 8], "big")
    off += 8
    salt = env[off:off + SALT_LEN]
    off += SALT_LEN
    nonce = env[off:off + NONCE_LEN]
    off += NONCE_LEN
    ct = env[off:]
    return ver, kid, writer, ts_ns, salt, nonce, ct


def is_sealed(value: object) -> bool:
    """True only for a string that decodes as an ``afctx1:`` envelope whose
    version byte is known — a plain value that merely starts with the magic
    (e.g. documentation quoting the format) is not mistaken for one."""
    if not isinstance(value, str) or not value.startswith(MAGIC):
        return False
    try:
        env = base64.b64decode(value[len(MAGIC):], validate=True)
    except (binascii.Error, ValueError):
        return False
    return len(env) >= _MIN_ENVELOPE and env[0] == VER


def envelope_meta(armored: str) -> tuple[int, int, str, int]:
    """Cleartext header metadata ``(ver, kid, writer, ts_ns)`` — no key needed.

    The header is inside the AEAD's associated data, so once *any* key holder has
    opened the envelope these values are authenticated. A keyless component (the
    daemon) can still parse them for anti-replay bookkeeping and per-writer
    eviction, and a reader can compare them against whatever the daemon claims —
    a mismatch is a lying/mixed-up store and must be refused. Raises
    ``CorruptEnvelopeError`` when the armor/header is malformed."""
    ver, kid, writer, ts_ns, _salt, _nonce, _ct = _parse(armored)
    return ver, kid, writer, ts_ns


def _seal_raw(
    value: str,
    description: str,
    *,
    key_name: str,
    writer: str,
    ts_ns: int,
    kid: int,
    psk: bytes,
    salt: bytes,
    nonce: bytes,
) -> str:
    """Deterministic sealer for tests/KATs. Production ``seal`` always draws a
    fresh salt and nonce from ``os.urandom``."""
    ChaCha20Poly1305, _, _ = _require_crypto()
    aad = _aad(VER, kid, key_name, writer, ts_ns)
    dk = _hkdf(psk, salt)
    plaintext = json.dumps({"v": value, "d": description}, ensure_ascii=False).encode("utf-8")
    ct = ChaCha20Poly1305(dk).encrypt(nonce, plaintext, aad)
    return MAGIC + base64.b64encode(
        _pack(VER, kid, writer, ts_ns, salt, nonce, ct)
    ).decode("ascii")


def seal(
    value: str,
    description: str = "",
    *,
    key_name: str,
    writer: str,
    ts_ns: int,
    keyring: Keyring | None = None,
) -> str:
    """Seal one blob; returns the armored string. Raises ``CryptoError``."""
    kr = keyring if keyring is not None else load_keyring()
    kid, psk = kr.active()
    return _seal_raw(
        value,
        description,
        key_name=key_name,
        writer=writer,
        ts_ns=ts_ns,
        kid=kid,
        psk=psk,
        salt=os.urandom(SALT_LEN),
        nonce=os.urandom(NONCE_LEN),
    )


def unseal(
    armored: str, *, key_name: str, keyring: Keyring | None = None
) -> tuple[str, str]:
    """Open an armored blob sealed under ``key_name``; returns (value, description).

    ``key_name`` is authenticated (it is in the AAD), so moving ciphertext to a
    different storage key fails the tag check rather than returning wrong data.
    """
    ChaCha20Poly1305, _, _ = _require_crypto()
    ver, kid, writer, ts_ns, salt, nonce, ct = _parse(armored)
    if ver != VER:
        raise CorruptEnvelopeError(f"unsupported envelope version {ver}")
    kr = keyring if keyring is not None else load_keyring()
    psk = kr.get(kid)
    if psk is None:
        raise UnknownKidError(f"envelope kid {kid} is not in the keyring")
    aad = _aad(ver, kid, key_name, writer, ts_ns)
    dk = _hkdf(psk, salt)
    try:
        plaintext = ChaCha20Poly1305(dk).decrypt(nonce, ct, aad)
    except Exception as exc:  # cryptography raises InvalidTag (and only that)
        raise CorruptEnvelopeError("authentication failed (wrong key, key name, or tampering)") from exc
    try:
        obj = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CorruptEnvelopeError("sealed plaintext is not valid JSON") from exc
    if not isinstance(obj, dict) or not isinstance(obj.get("v"), str):
        raise CorruptEnvelopeError("sealed plaintext has an unexpected shape")
    return obj["v"], str(obj.get("d") or "")
