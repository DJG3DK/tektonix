"""The release's Windows installer check verifies the RFC 3161 timestamp
with OpenSSL (scripts/verify_authenticode_timestamp.py). osslsigncode 2.8
could not read Microsoft's newer timestamp tokens and failed v0.9.2's
correctly signed installer (2026-10-07). The fixtures are the real
signatures of v0.9.2 and v0.9.2-rc2, one token of each kind."""

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_authenticode_timestamp.py"
FIX = ROOT / "tests" / "fixtures" / "authenticode"
MS_ROOT = FIX / "microsoft-identity-verification-root-2020.pem"
pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")


def _check(sig: Path, root: Path = MS_ROOT):
    return subprocess.run([sys.executable, str(SCRIPT), str(sig), str(root)],
                          capture_output=True, text=True, timeout=60)


def _module():
    spec = importlib.util.spec_from_file_location("vat", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("name,when", [
    ("v0.9.2-signature.der", "20261007111050.572Z"),       # the newer token osslsigncode 2.8 cannot read
    ("v0.9.2-rc2-signature.der", "20261006124235.094Z"),
])
def test_a_real_microsoft_timestamp_verifies_and_says_when(name, when):
    r = _check(FIX / name)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == when


def test_a_token_that_does_not_chain_to_the_root_fails():
    r = _check(FIX / "v0.9.2-signature.der", FIX / "not-a-root.pem")
    assert r.returncode == 1 and "does not verify" in r.stderr


def test_a_timestamp_borrowed_by_another_signature_fails(tmp_path):
    """Flip one byte of the signature value: the token is still Microsoft's
    and still valid, but it no longer vouches for this signature."""
    vat = _module()
    b = bytearray((FIX / "v0.9.2-signature.der").read_bytes())
    _, s, e = next((t, s, e) for t, _, s, e in vat.first_signer(bytes(b)) if t == 0x04)
    b[e - 1] ^= 0x01
    tampered = tmp_path / "tampered.der"
    tampered.write_bytes(bytes(b))
    r = _check(tampered)
    assert r.returncode == 1 and "does not cover this signature" in r.stderr


def test_a_signature_without_a_timestamp_fails(tmp_path):
    """Retag the unsigned attributes so nothing is found where the token was."""
    vat = _module()
    b = bytearray((FIX / "v0.9.2-signature.der").read_bytes())
    start = next(i for t, i, _, _ in vat.first_signer(bytes(b)) if t == 0xA1)
    b[start] = 0xA2
    bare = tmp_path / "bare.der"
    bare.write_bytes(bytes(b))
    r = _check(bare)
    assert r.returncode == 1 and "no timestamp" in r.stderr
