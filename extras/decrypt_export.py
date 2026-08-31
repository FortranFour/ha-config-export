#!/usr/bin/env python3
"""
decrypt_export.py — open an encrypted Home Assistant config export.

Standalone recovery tool. It needs nothing from Home Assistant: no add-on, no
share, no config. Copy this file and an archive onto any machine with Python 3
and it will decrypt, given the passphrase.

    pip install cryptography
    python3 decrypt_export.py ha-config-2026-08-30.tar.gz.enc

Works on Windows, macOS and Linux.

    python3 decrypt_export.py ARCHIVE.tar.gz.enc            decrypt in place
    python3 decrypt_export.py ARCHIVE.tar.gz.enc -o out.tar.gz
    python3 decrypt_export.py SIDECAR.sidecar.json.enc      same, for a sidecar
    python3 decrypt_export.py ARCHIVE.tar.gz.enc --list      list contents only
    python3 decrypt_export.py ARCHIVE.tar.gz.enc --extract DIR

The passphrase is read from --passphrase-file, the CE_PASSPHRASE environment
variable, or an interactive prompt — in that order. Typing it as a command
argument is deliberately not supported: that would put it in your shell history.

FORMAT, for anyone rebuilding this from scratch
-----------------------------------------------
    bytes 0-5    b"CEENC1"
    bytes 6-21   scrypt salt (16 random bytes, unique per file)
    bytes 22-    a Fernet token (AES-128-CBC + HMAC-SHA256)

    key = urlsafe_b64encode(scrypt(passphrase, salt, n=2**15, r=8, p=1, len=32))

Nothing is tied to the machine that wrote the file, so the same passphrase
opens it anywhere, forever.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import os
import sys
import tarfile
from pathlib import Path

HEADER = b"CEENC1"
SALT_LEN = 16


def read_passphrase(args: argparse.Namespace) -> str:
    if args.passphrase_file:
        text = Path(args.passphrase_file).read_text(encoding="utf-8").strip()
        if text:
            return text
        sys.exit(f"{args.passphrase_file} is empty.")
    env = os.environ.get("CE_PASSPHRASE", "").strip()
    if env:
        return env
    return getpass.getpass("Passphrase: ")


def decrypt(blob: bytes, passphrase: str) -> bytes:
    try:
        from cryptography.fernet import Fernet, InvalidToken
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    except ImportError:
        sys.exit("This needs the 'cryptography' package:  pip install cryptography")

    if not blob.startswith(HEADER):
        sys.exit(
            "Not a Configuration Export encrypted file (missing CEENC1 header).\n"
            "If the name ends .tar.gz without .enc, it is not encrypted — just "
            "extract it."
        )

    salt = blob[len(HEADER):len(HEADER) + SALT_LEN]
    token = blob[len(HEADER) + SALT_LEN:]
    kdf = Scrypt(salt=salt, length=32, n=2 ** 15, r=8, p=1)
    key = base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))
    try:
        return Fernet(key).decrypt(token)
    except InvalidToken:
        sys.exit(
            "Could not decrypt. Either the passphrase is wrong, or the file is "
            "damaged.\nA wrong passphrase and a corrupted file look identical "
            "here — that is how the format works, not a diagnosis."
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Decrypt a Configuration Export archive or sidecar.")
    parser.add_argument("path", help="the .enc file")
    parser.add_argument("-o", "--output", help="where to write (default: name without .enc)")
    parser.add_argument("--passphrase-file", help="file whose first line is the passphrase")
    parser.add_argument("--list", action="store_true", help="list archive contents, write nothing")
    parser.add_argument("--extract", metavar="DIR", help="extract the archive into DIR")
    args = parser.parse_args()

    source = Path(args.path)
    if not source.is_file():
        sys.exit(f"No such file: {source}")

    plain = decrypt(source.read_bytes(), read_passphrase(args))

    if args.list or args.extract:
        import io
        with tarfile.open(fileobj=io.BytesIO(plain), mode="r:gz") as tar:
            if args.list:
                for member in tar.getmembers():
                    if member.isfile():
                        print(f"{member.size:>10}  {member.name}")
                return 0
            target = Path(args.extract)
            target.mkdir(parents=True, exist_ok=True)
            tar.extractall(target)
            print(f"Extracted to {target.resolve()}")
            return 0

    out = Path(args.output) if args.output else source.with_suffix("")
    if out.exists():
        sys.exit(f"{out} already exists — pass -o to write somewhere else.")
    out.write_bytes(plain)
    print(f"Wrote {out}  ({len(plain):,} bytes)")
    if out.name.endswith(".tar.gz"):
        print(f"Extract it with:  tar -xzf {out.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
