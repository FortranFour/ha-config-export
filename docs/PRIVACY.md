# Redaction and encryption

Both are optional and both are off by default. Turn them on from the **Privacy & encryption**
section of the export card.

Read this before you rely on either — they protect different things, and one of them protects
less than it looks like it does.

---

## Encryption

Ticking **Encrypt the backup** wraps each generation with AES via `cryptography`'s Fernet,
keyed by scrypt from your passphrase. Archives become `ha-config-2026-08-30.tar.gz.enc` and
show a padlock in the restore picker. Restore decrypts them in memory — never to a plaintext
file in the share.

This is a real boundary. An archive copied to your PC, or synced to a cloud provider, is
unreadable without the passphrase.

**Encryption removes the `latest/` mirror.** An uncompressed copy of the same content sitting
beside an encrypted archive is not encrypted, so the script deletes it and says so in the log.
You lose easy browsing and diffing; that is the trade.

## Redaction

> [!CAUTION]
> **Versions before 1.3.0 did not redact the readable copies.** The `yaml/` and `lovelace/`
> trees were generated from the original files before redaction ran, so a "redacted" export
> still carried every secret in its converted YAML — API keys, integration passwords,
> embedded camera credentials. Opaque files (keys, pickles) and Frigate's config were also
> copied unredacted. If you shared a redacted export made with an earlier version, treat
> every credential in it as exposed and rotate them.

Ticking **Redact personal info** redacts the raw files first and only then builds the
readable `yaml/` and `lovelace/` views from them, so no copy in the archive holds more than
the redacted originals. It replaces credential-shaped values with tokens:

- Values whose key name contains `password`, `token`, `api_key`, `secret`, `private_key`,
  `client_secret`, `access_token`, `refresh_token`, `session`, `cookie`, `credential`,
  `auth`, `salt`, `hash`, `pin`, `license`, `webhook`, `cloudhook`, `jwt`, `psk`, `passcode`
  and similar
- Keys named `key`, `ota` or `encryption` when the value is secret-shaped — ESPHome's
  `api: encryption: key:` — while leaving ordinary values such as a storage file's
  `"key": "lovelace"` alone
- Provider tokens recognisable by prefix wherever they appear, whatever key they sit under:
  OpenAI and Anthropic keys, GitHub tokens, Google API keys, Slack tokens, JWTs, Nabu Casa
  cloudhook IDs
- Private key blocks (`-----BEGIN … PRIVATE KEY-----`)
- Email addresses, and credentials embedded in URLs such as `rtsp://user:pass@camera/stream`
  — now inside JSON string values as well as YAML
- `latitude` and `longitude` in `.storage`

**Files it cannot read are withheld, not guessed at.** Pickles, certificates, ADB keys and
anything else that is not text are left out of a redacted archive entirely and stored in the
sidecar instead. A file of session cookies is one secret from end to end; there is no safe
partial redaction of it.

**Frigate's config is decoded first.** Its API returns the YAML config wrapped as a JSON
string. It is now unwrapped into the real YAML document before redaction, so it is both
redactable and restorable.

> [!WARNING]
> **Redaction is best-effort, not a security boundary.** It works on key names and value
> shapes, so a secret stored under an unusual key will survive it. Use it to make an export
> *shareable* — for posting a config excerpt, or handing a snapshot to someone debugging with
> you. Do not use it as the reason an export is safe to sync somewhere. Encryption is what
> makes an export safe.

## The sidecar

The sidecar holds every redacted value **and every withheld file**, so it is the complete
recovery set. It is written in plaintext unless **Encrypt the sidecar** is ticked, and the
export log warns every run while it is not. Treat an unencrypted sidecar as the most
sensitive file in the backup folder.

Withheld files are not in the archive, so the restore card cannot offer them. They can be
recovered from the sidecar's `withheld` section, which maps each path to its base64
contents.

Redaction on its own is one-way: the archive no longer contains the real values, so restoring
from it gives you files full of `__CE_REDACTED_0001__`.

**Keep sidecar of redacted values** fixes that. It writes what was removed to
`sidecars/ha-config-<date>.sidecar.json`, and restore puts the values back automatically.

**The sidecar is written outside the archive, deliberately.** If it lived inside, redaction
would be obfuscation with the key taped to the box. Kept separate, the archive can be shared
or synced while the sidecar stays behind.

Which also means: **if you sync the whole backup folder somewhere, sync `sidecars/`
separately, or encrypt it.** `sidecars/` next to `daily/` in the same cloud folder undoes the
point.

Sidecars are pruned on the same rule as rollback folders — kept while newer than a year or
among the twelve most recent — so they do not accumulate for generations that aged out long
ago. The **Cleanup** section of the export card clears them sooner if you want the space back.

Restore un-redacts by default. Pass `--no-unredact` to the script if you want the tokens left
in place.

## The passphrase

Type it into the card and press SAVE. The script writes it to
`/share/ha_config_backup/.passphrase` and immediately clears the box.

**It is remembered.** Scheduled exports and restores read it from that file, so you are not
prompted. It survives reboots and Home Assistant updates.

**It is not stored in a Home Assistant entity, deliberately.** Entity state is written to
`.storage` and the recorder database — both of which this export copies. A key stored inside
the thing it encrypts is not a key. The package also adds a `recorder:` exclusion for the text
box so the value never reaches the database on its way through.

So the security boundary is **filesystem access**. Anyone who can read `/share` can read both
the key and the archives it protects. That is fine for the case that matters — archives
leaving the machine — and no help at all against someone already on it.

> [!IMPORTANT]
> **Write the passphrase down somewhere physical.** The key file is the only copy. If the disk
> dies, every encrypted archive becomes permanently unreadable, and you will not discover it
> until the day you need a restore.

Encrypted archives are not tied to this machine: the passphrase is the only input, so an
archive opens anywhere. [RECOVERY.md](RECOVERY.md) covers opening one without Home Assistant,
including a route needing only `openssl` and `tar`.

If you would rather not leave a key on disk, delete `.passphrase`. Exports then fail loudly
with a message naming the file, and you create it by hand before a manual run. Scheduled
exports cannot work that way.

## Choosing

| Situation | What to turn on |
|:--|:--|
| Backups stay on the HA machine | Nothing. This is the default |
| Copied to a PC on the same network | Nothing, or encryption if the PC is shared |
| Synced to OneDrive, Dropbox, Google Drive | **Encrypt** |
| Posting a config excerpt for help | **Redact**, and keep the sidecar |
| Handing a full snapshot to someone else | **Redact** + sidecar kept back |
| Belt and braces | Redact + sidecar + encrypt both |
