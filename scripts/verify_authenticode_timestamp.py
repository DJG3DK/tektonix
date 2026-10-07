#!/usr/bin/env python3
"""Verify an Authenticode signature's RFC 3161 timestamp with OpenSSL.

    verify_authenticode_timestamp.py <signature.der> <root.pem>

<signature.der> is the PKCS#7 signature osslsigncode extracts from the
installer. Checks that its timestamp token is signed by a timestamping
certificate that chains to <root.pem>, and that the token covers this
signature (its message imprint is the hash of the signature value), then
prints the time it vouches for.

osslsigncode does this itself, but 2.8 (Ubuntu 24.04) reports "Timestamp is
not available" for Microsoft's timestamping service since it began writing
sha256WithRSAEncryption as the token's signature algorithm. v0.9.2's
installer failed the release check that way on 2026-10-07 while OpenSSL
verified its timestamp completely. Standard library and the openssl binary
only, so it runs on any CI runner.
"""
import hashlib
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

TIMESTAMP_ATTR = "1.3.6.1.4.1.311.3.3.1"   # Microsoft's RFC 3161 countersignature
HASHES = {"2.16.840.1.101.3.4.2.1": "sha256", "2.16.840.1.101.3.4.2.2": "sha384",
          "2.16.840.1.101.3.4.2.3": "sha512"}


def tlv(b: bytes, i: int):
    """(tag, start of contents, end of contents) of the DER element at i."""
    tag, n = b[i], b[i + 1]
    i += 2
    if n & 0x80:
        k = n & 0x7F
        n = int.from_bytes(b[i:i + k], "big")
        i += k
    return tag, i, i + n


def children(b: bytes, start: int, end: int):
    i = start
    while i < end:
        tag, s, e = tlv(b, i)
        yield tag, i, s, e
        i = e


def oid(b: bytes) -> str:
    first = b[0]
    parts = [first // 40, first % 40]
    v = 0
    for byte in b[1:]:
        v = (v << 7) | (byte & 0x7F)
        if not byte & 0x80:
            parts.append(v)
            v = 0
    return ".".join(map(str, parts))


def signed_data(b: bytes, i: int = 0):
    """The SignedData SEQUENCE inside a ContentInfo at i: (start, end)."""
    _, s, e = tlv(b, i)
    kids = list(children(b, s, e))
    _, _, cs, ce = kids[1]              # [0] EXPLICIT content
    _, _, ss, se = next(children(b, cs, ce))
    return ss, se


def first_signer(b: bytes):
    """The fields of SignedData's first SignerInfo: {tag: (elem start, start, end)}."""
    s, e = signed_data(b)
    sets = [k for k in children(b, s, e) if k[0] == 0x31]
    _, _, si_s, si_e = sets[-1]         # signerInfos is the last SET
    _, _, f_s, f_e = next(children(b, si_s, si_e))
    return list(children(b, f_s, f_e))


def tstinfo_unverified(token: bytes) -> bytes:
    """The TSTInfo a token carries: its encapsulated content, unchecked."""
    s, e = signed_data(token)
    encap = next(k for k in children(token, s, e) if k[0] == 0x30)  # encapContentInfo
    _, _, cs, ce = list(children(token, encap[2], encap[3]))[1]       # [0] EXPLICIT
    _, os_, oe = tlv(token, cs)                                      # OCTET STRING
    return token[os_:oe]


def gen_time(info: bytes) -> str:
    _, s, e = tlv(info, 0)
    kids = list(children(info, s, e))
    _, gs, ge = tlv(info, kids[4][1])
    return info[gs:ge].decode()


def die(msg: str):
    print(f"timestamp: {msg}", file=sys.stderr)
    sys.exit(1)


def main(sig_path: str, root: str) -> None:
    b = Path(sig_path).read_bytes()
    fields = first_signer(b)
    signature = next((b[s:e] for t, _, s, e in fields if t == 0x04), None)
    unsigned = next(((s, e) for t, _, s, e in fields if t == 0xA1), None)
    if signature is None or unsigned is None:
        die("the signature carries no timestamp")
    token = None
    for _, _, s, e in children(b, *unsigned):
        kids = list(children(b, s, e))
        if oid(b[kids[0][2]:kids[0][3]]) == TIMESTAMP_ATTR:
            _, vs, ve = tlv(b, kids[1][1])          # the SET of values
            ts, _, te = tlv(b, vs)
            token = b[vs:te]
    if token is None:
        die("the signature has no RFC 3161 timestamp")
    # The token is judged as of the time it vouches for, as Windows judges
    # it: its certificates only had to be valid then. Read from the token
    # before it is verified, and verified against below.
    claimed = gen_time(tstinfo_unverified(token))
    when = int(datetime.strptime(claimed[:14], "%Y%m%d%H%M%S").replace(tzinfo=UTC).timestamp())
    with tempfile.TemporaryDirectory() as tmp:
        tok, tst = Path(tmp, "token.der"), Path(tmp, "tstinfo.der")
        tok.write_bytes(token)
        r = subprocess.run(["openssl", "cms", "-verify", "-inform", "DER", "-in", str(tok),
                            "-CAfile", root, "-purpose", "timestampsign", "-attime", str(when),
                            "-out", str(tst)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            die("the timestamp token does not verify: " + r.stderr.strip().splitlines()[-1])
        info = tst.read_bytes()
    _, s, e = tlv(info, 0)                          # TSTInfo
    kids = list(children(info, s, e))
    _, _, ms, me = kids[2]                          # messageImprint
    alg_elem, hashed = list(children(info, ms, me))
    _, _, a_s, a_e = next(children(info, alg_elem[2], alg_elem[3]))
    name = HASHES.get(oid(info[a_s:a_e]))
    if name is None:
        die("the timestamp uses a hash this check does not know")
    if info[hashed[2]:hashed[3]] != hashlib.new(name, signature).digest():
        die("the timestamp does not cover this signature")
    verified = gen_time(info)
    if verified != claimed:
        die("the timestamp's time changed between reading and verifying it")
    print(verified)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
