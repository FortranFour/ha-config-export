# Home Assistant Configuration Export & Restore

Generational backups of every user-configurable Home Assistant YAML **and JSON** file, with
the UI-managed JSON in `.storage` converted to readable YAML — plus selective, file-by-file
restore, optional redaction, and optional encryption.

Home Assistant's built-in backups restore a whole instance well. They are less good at
answering the question this exists for:

> *What did my dashboard look like three weeks ago, before I broke it?*

No custom integration, no add-on. Two Python scripts, two YAML packages, two Lovelace cards.

---

## What it does

**Export**

- Copies every `*.yaml`, `*.yml` and `*.json` from your config directory
- Copies all of `.storage` — helpers, entity/device/area/floor/label registries, energy
  config, exposed entities, config entries
- **Converts every JSON file to YAML** alongside the original, so you can read and diff them
- **Extracts each UI dashboard** from its `.storage` wrapper into a ready-to-use YAML-mode
  dashboard file
- Keeps **7 daily, 4 weekly, 12 monthly, 5 yearly** generations, hardlinked so 28
  generations cost roughly the size of one
- Keeps an uncompressed `latest/` mirror for browsing and diffing without extracting
- Writes a `MANIFEST.txt` with SHA-256 per file

**Restore**

- Browse any generation — the `latest/` mirror or any archive, read straight from the
  `.tar.gz`
- Every candidate is hashed against the live file, so by default you only see what actually
  differs
- Pick files from a paged list with **search** and sortable **Name / Size** columns, or type
  a path with wildcards
- A persistent queue that survives paging, filter changes and Home Assistant restarts
- Every overwritten file is copied to a timestamped rollback folder first

**Privacy** *(all optional, all off by default)*

- **Redact** credential-shaped values before archiving, to make an export shareable
- **Sidecar** recording what redaction removed, written *outside* the archive so a redacted
  backup can still be restored in full
- **Encrypt** the archive, and optionally the sidecar, with a passphrase
- Restore decrypts and un-redacts transparently

Typical export: ~190 files, ~120 converted to YAML, ~4 MB compressed, about 12 seconds.

---

## Requirements

- Home Assistant OS or Supervised
- The **Samba share** add-on, or any writable path the Core container can see
- `packages: !include_dir_named packages` in your `configuration.yaml`
- HACS frontend cards: `stack-in-card`, `mushroom`, `button-card`, `card-mod`

No Python dependencies. The scripts use PyYAML when available — it is, inside the HA Core
container — and fall back to a built-in emitter otherwise. Encryption uses `cryptography`,
which also ships with Home Assistant.

---

## Install

See **[docs/INSTALL.md](docs/INSTALL.md)** for the full walkthrough. In short:

1. Copy `scripts/*.py` into `/share/ha_config_backup/` on your Home Assistant machine
2. Copy `packages/*.yaml` into `/config/packages/` and reload
3. Add `cards/export_card.yaml` and `cards/restore_card.yaml` as Manual cards
4. Set a run time on the export card

---

## Repository layout

| Path | What it is |
|:--|:--|
| `scripts/ha_config_backup.py` | The export engine. Also `--check` and `--report` modes |
| `scripts/ha_config_restore.py` | The restore engine: browse, compare, queue, restore |
| `packages/config_yaml_export.yaml` | Entities and schedule for the export |
| `packages/config_restore.yaml` | Entities for the restore UI |
| `cards/export_card.yaml` | Status, generation counts, privacy options, "Back up now" |
| `cards/restore_card.yaml` | Generation picker, search, file list, restore controls |
| `extras/decrypt_export.py` | Standalone decryption tool — needs no Home Assistant |
| `extras/` | Optional Windows scripts: copy to PC, encrypted cloud copy |
| `docs/` | Install, restore, privacy, recovery, browser links, off-site copies |

---

## Security

**As shipped, nothing is redacted or encrypted.** `secrets.yaml`, `.storage/auth*`, API
tokens and any credentials your integrations hold all land in the destination in plaintext.
That is what makes the export restore-grade, but it means the destination folder deserves the
same protection as your config directory.

Two optional layers change that, and it is worth being precise about what each one buys:

- **Encryption** is a real boundary. An archive copied to your PC or a cloud provider is
  unreadable without the passphrase.
- **Redaction** is best-effort. It matches credential-shaped key names, emails, embedded URL
  credentials and coordinates, so it will miss a secret stored under an unusual key. It makes
  an export *shareable*; it does not make it *safe*.

The passphrase is stored in a key file the scripts read, not in a Home Assistant entity —
entity state lands in `.storage` and the recorder database, both of which this export copies,
and a key stored inside the backup is not a key. That means the boundary is filesystem
access: anyone who can read the share can read both the key and the archives.

See **[docs/PRIVACY.md](docs/PRIVACY.md)** before turning either on, and
**[docs/RECOVERY.md](docs/RECOVERY.md)** for opening an encrypted archive when the server no
longer exists — with a standalone tool, or with nothing but `openssl` and `tar`.

---

## Licence

MIT. See [LICENSE](LICENSE).

## Credits

Developed collaboratively with Claude (Anthropic) and debugged against a live instance.
