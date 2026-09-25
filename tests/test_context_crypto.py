"""The LAN context-blob codec: armor, AAD binding, keyring, fail-closed paths."""

from __future__ import annotations

import base64
import os

import pytest

from ask_fable import context_crypto as cc

PSK_OLD = bytes(range(32))
PSK_NEW = bytes(range(32, 64))
KEY_NAME = "repo:auth"
WRITER = "hostA"
TS_NS = 1_760_000_000_123_456_789


@pytest.fixture
def keyring(tmp_path, monkeypatch):
    p = tmp_path / "context_keyring"
    p.write_text(
        f"2:{base64.b64encode(PSK_NEW).decode()}\n"
        f"1:{base64.b64encode(PSK_OLD).decode()}\n"
    )
    os.chmod(p, 0o600)
    monkeypatch.setenv("ASK_FABLE_CONTEXT_KEYRING", str(p))
    return cc.load_keyring()


def _seal(value: str = "def login(): ...", description: str = "auth module") -> str:
    kr = cc.load_keyring()
    kid, psk = kr.active()
    return cc._seal_raw(
        value, description, key_name=KEY_NAME, writer=WRITER, ts_ns=TS_NS,
        kid=kid, psk=psk, salt=b"\x01" * cc.SALT_LEN, nonce=b"\x02" * cc.NONCE_LEN,
    )


def _envelope_bytes(armored: str) -> bytearray:
    return bytearray(base64.b64decode(armored[len(cc.MAGIC):]))


def _rearmor(env: bytes) -> str:
    return cc.MAGIC + base64.b64encode(bytes(env)).decode("ascii")


# --- roundtrip / format -----------------------------------------------------

def test_roundtrip(keyring):
    armored = _seal("VALUE", "DESC")
    assert cc.is_sealed(armored)
    value, desc = cc.unseal(armored, key_name=KEY_NAME, keyring=keyring)
    assert value == "VALUE" and desc == "DESC"


def test_roundtrip_unicode_and_large_value(keyring):
    value = "héllo wörld \U0001f600\n" * 5000
    armored = cc.seal(value, "déscription", key_name=KEY_NAME, writer=WRITER,
                      ts_ns=TS_NS, keyring=keyring)
    got, desc = cc.unseal(armored, key_name=KEY_NAME, keyring=keyring)
    assert got == value and desc == "déscription"


def test_deterministic_test_seam(keyring):
    assert _seal() == _seal()  # fixed salt+nonce, same kid -> byte-identical armor
    env = _envelope_bytes(_seal())
    assert env[0] == cc.VER
    assert env[1] == 2  # active kid = first keyring line
    wlen = int.from_bytes(env[2:4], "big")
    assert env[4:4 + wlen].decode() == WRITER
    off = 4 + wlen
    assert int.from_bytes(env[off:off + 8], "big") == TS_NS


def test_production_seal_uses_fresh_salt_and_nonce(keyring):
    a = cc.seal("v", key_name=KEY_NAME, writer=WRITER, ts_ns=TS_NS, keyring=keyring)
    b = cc.seal("v", key_name=KEY_NAME, writer=WRITER, ts_ns=TS_NS, keyring=keyring)
    assert a != b  # fresh salt+nonce every seal
    assert cc.unseal(a, key_name=KEY_NAME, keyring=keyring)[0] == "v"
    assert cc.unseal(b, key_name=KEY_NAME, keyring=keyring)[0] == "v"


# --- AAD binding: nothing can be relabelled/re-dated/re-attributed ----------

def test_wrong_key_name_fails(keyring):
    armored = _seal()
    with pytest.raises(cc.CorruptEnvelopeError):
        cc.unseal(armored, key_name="repo:billing", keyring=keyring)


def test_writer_tamper_fails(keyring):
    armored = _seal()
    env = _envelope_bytes(armored)
    env[4] = ord("B")  # "hostA" -> "BostA"
    with pytest.raises(cc.CorruptEnvelopeError):
        cc.unseal(_rearmor(env), key_name=KEY_NAME, keyring=keyring)


def test_ts_tamper_fails(keyring):
    armored = _seal()
    env = _envelope_bytes(armored)
    wlen = int.from_bytes(env[2:4], "big")
    off = 4 + wlen
    env[off] ^= 0x01
    with pytest.raises(cc.CorruptEnvelopeError):
        cc.unseal(_rearmor(env), key_name=KEY_NAME, keyring=keyring)


def test_kid_tamper_to_another_known_kid_fails(keyring):
    armored = _seal()
    env = _envelope_bytes(armored)
    env[1] = 1  # known kid, but not the key it was sealed with
    with pytest.raises(cc.CorruptEnvelopeError):
        cc.unseal(_rearmor(env), key_name=KEY_NAME, keyring=keyring)


def test_ciphertext_flip_fails(keyring):
    armored = _seal()
    env = _envelope_bytes(armored)
    env[-1] ^= 0x01
    with pytest.raises(cc.CorruptEnvelopeError):
        cc.unseal(_rearmor(env), key_name=KEY_NAME, keyring=keyring)


def test_unknown_kid_is_named(keyring):
    armored = cc._seal_raw(
        "v", "", key_name=KEY_NAME, writer=WRITER, ts_ns=TS_NS,
        kid=9, psk=PSK_NEW, salt=b"\x00" * cc.SALT_LEN, nonce=b"\x00" * cc.NONCE_LEN,
    )
    with pytest.raises(cc.UnknownKidError):
        cc.unseal(armored, key_name=KEY_NAME, keyring=keyring)


# --- is_sealed sniffing -----------------------------------------------------

@pytest.mark.parametrize(
    "value",
    [
        "",
        "plain text",
        "not afctx1: at the start",
        "afctx1:",
        "afctx1:not base64 !!!",
        "afctx1:" + base64.b64encode(b"\x01" * 10).decode(),  # too short
    ],
)
def test_is_sealed_negatives(value):
    assert cc.is_sealed(value) is False


def test_is_sealed_rejects_unknown_version(keyring):
    env = _envelope_bytes(_seal())
    env[0] = 2
    assert cc.is_sealed(_rearmor(env)) is False


def test_is_sealed_accepts_own_envelopes(keyring):
    assert cc.is_sealed(_seal()) is True


# --- keyring ---------------------------------------------------------------

def test_keyring_missing_is_refusal(tmp_path, monkeypatch):
    monkeypatch.setenv("ASK_FABLE_CONTEXT_KEYRING", str(tmp_path / "nope"))
    with pytest.raises(cc.KeyringError):
        cc.load_keyring()


def test_keyring_loose_mode_is_refusal(tmp_path, monkeypatch):
    p = tmp_path / "kr"
    p.write_text(f"1:{base64.b64encode(PSK_OLD).decode()}\n")
    os.chmod(p, 0o644)
    monkeypatch.setenv("ASK_FABLE_CONTEXT_KEYRING", str(p))
    with pytest.raises(cc.KeyringError):
        cc.load_keyring()


@pytest.mark.parametrize(
    "text",
    [
        "",                                              # empty
        "# only comments\n\n",                           # no entries
        "not-a-kid:AAAA\n",                              # bad kid
        "1:!!!not-base64!!!\n",                          # bad base64
        f"1:{base64.b64encode(b'short').decode()}\n",    # wrong key length
        f"1:{base64.b64encode(PSK_OLD).decode()}\n1:{base64.b64encode(PSK_NEW).decode()}\n",
        "300:AAAA\n",                                    # kid out of range
    ],
)
def test_keyring_malformed_is_refusal(tmp_path, monkeypatch, text):
    p = tmp_path / "kr"
    p.write_text(text)
    os.chmod(p, 0o600)
    monkeypatch.setenv("ASK_FABLE_CONTEXT_KEYRING", str(p))
    with pytest.raises(cc.KeyringError):
        cc.load_keyring()


def test_keyring_skips_comments_and_blank_lines(keyring):
    assert keyring.kids() == [2, 1]
    assert keyring.active()[0] == 2
    assert keyring.get(1) == PSK_OLD and keyring.get(2) == PSK_NEW
    assert keyring.get(3) is None


def test_rotation_old_kid_still_opens(keyring):
    armored = cc._seal_raw(
        "old blob", "", key_name=KEY_NAME, writer=WRITER, ts_ns=TS_NS,
        kid=1, psk=PSK_OLD, salt=b"\x03" * cc.SALT_LEN, nonce=b"\x04" * cc.NONCE_LEN,
    )
    assert cc.unseal(armored, key_name=KEY_NAME, keyring=keyring)[0] == "old blob"


# --- missing dependency -----------------------------------------------------

def test_crypto_unavailable_is_named(monkeypatch, keyring):
    def _boom():
        raise cc.CryptoUnavailable("no crypto")

    monkeypatch.setattr(cc, "_require_crypto", _boom)
    with pytest.raises(cc.CryptoUnavailable):
        cc.seal("v", key_name=KEY_NAME, writer=WRITER, ts_ns=TS_NS, keyring=keyring)
    with pytest.raises(cc.CryptoUnavailable):
        cc.unseal(_seal(), key_name=KEY_NAME, keyring=keyring)
