# Opening an encrypted export without Home Assistant

The scenario this exists for: the server is gone, you have an archive from a PC copy or cloud
sync, and you need what is inside it.

Nothing about an encrypted archive is tied to the machine that wrote it. The passphrase is
the only input. Given that, any of the three routes below will open it.

---

## Route 1 — the standalone tool (easiest)

`extras/decrypt_export.py` needs nothing from Home Assistant. Copy it and the archive onto
any machine with Python 3.

```bash
pip install cryptography
python3 decrypt_export.py ha-config-2026-08-30.tar.gz.enc
tar -xzf ha-config-2026-08-30.tar.gz
```

Works the same on Windows, macOS and Linux. Other modes:

```bash
python3 decrypt_export.py ARCHIVE.enc --list             # look inside, write nothing
python3 decrypt_export.py ARCHIVE.enc --extract restored # straight to a folder
python3 decrypt_export.py SIDECAR.json.enc               # decrypt a sidecar
```

The passphrase comes from `--passphrase-file`, the `CE_PASSPHRASE` environment variable, or a
prompt. Passing it as an argument is deliberately unsupported — it would land in your shell
history.

## Route 2 — openssl, no Python at all

Verified against OpenSSL 3.0. Useful if you are on a rescue system with nothing installed.

```bash
PASS='your passphrase'
FILE=ha-config-2026-08-30.tar.gz.enc

# 1. the salt is the 16 bytes after the 6-byte CEENC1 header
SALT=$(dd if=$FILE bs=1 skip=6 count=16 2>/dev/null | od -An -tx1 | tr -d " \n")

# 2. derive 32 bytes with scrypt; the second half is the AES key
KEY=$(openssl kdf -keylen 32 -kdfopt pass:"$PASS" -kdfopt hexsalt:$SALT \
        -kdfopt n:32768 -kdfopt r:8 -kdfopt p:1 SCRYPT | tr -d ":" | tr "A-Z" "a-z")
ENC=${KEY:32:32}

# 3. the rest of the file is a base64url Fernet token:
#    version(1) timestamp(8) IV(16) ciphertext HMAC(32)
tail -c +23 $FILE | base64 -d 2>/dev/null > token.bin ||
tail -c +23 $FILE | tr '_-' '/+' | base64 -d > token.bin

dd if=token.bin bs=1 skip=9 count=16 of=iv.bin 2>/dev/null
SIZE=$(stat -c%s token.bin)
dd if=token.bin bs=1 skip=25 count=$((SIZE-57)) of=ct.bin 2>/dev/null
IV=$(od -An -tx1 iv.bin | tr -d " \n")

# 4. decrypt
openssl enc -d -aes-128-cbc -K $ENC -iv $IV -in ct.bin -out archive.tar.gz
tar -xzf archive.tar.gz
```

This skips the HMAC check, so it will happily produce garbage from a corrupted file rather
than telling you. Fine for recovery; use Route 1 when you have the choice.

## Route 3 — a new Home Assistant

Install the project on the new server, put the archive in `daily/`, set the same passphrase on
the card, and the restore card opens it like any other generation. Best when you want the
selective restore rather than the raw files.

---

## The format, if this ever needs rebuilding from scratch

```
bytes 0-5     "CEENC1"
bytes 6-21    scrypt salt, 16 random bytes, unique per file
bytes 22-end  Fernet token (AES-128-CBC + HMAC-SHA256)

key = urlsafe_b64encode(scrypt(passphrase, salt, n=2^15, r=8, p=1, dklen=32))
```

Fernet splits that 32-byte key: first 16 bytes sign, last 16 encrypt.

---

## What to keep where

Encryption only helps if you can still decrypt years later. Two things must survive
independently of the server:

**The passphrase.** Written down somewhere physical, or in a password manager. The key file
on the server is not a backup of it — it dies with the disk.

**A copy of `decrypt_export.py`.** Or this page, since Route 2 lets you rebuild without it.
Storing it beside the archives is fine; it is not a secret.

The one arrangement that fails is encrypted archives in cloud storage with the passphrase
only in the key file on a server that no longer exists. At that point the archives are
permanently unreadable, and nothing in this document helps.

## Redacted archives

If the export was redacted, restored files contain `__CE_REDACTED_0001__` placeholders. The
originals are in the matching sidecar under `sidecars/`. Decrypt it the same way; it is JSON
mapping each token to its original value, per file:

```json
{
  "entries": {
    "config/secrets.yaml": {
      "__CE_REDACTED_0001__": "the original value"
    },
    "storage/core.config": {
      "__CE_REDACTED_0005__": {"value": "47.6062", "raw": true}
    }
  }
}
```

`"raw": true` means the original was a bare JSON number or boolean, so the quotes around the
token go too when substituting. The restore card does all of this automatically; this is for
doing it by hand.
