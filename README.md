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

<img width="522" height="407" alt="Screenshot 2026-08-30 173821" src="https://github.com/user-attachments/assets/c499c53a-7225-419a-86cb-78e1d9dfcfa6" />

<img width="517" height="1166" alt="Screenshot 2026-08-30 191047" src="https://github.com/user-attachments/assets/356a8bcf-f9a0-4667-bb0a-f27f0a792181" />




**Restore**

- Browse any generation — the `latest/` mirror or any archive, read straight from the
  `.tar.gz`
- Every candidate is hashed against the live file, so by default you only see what actually
  differs
- Pick files from a paged list with **search** and sortable **Name / Size** columns, or type
  a path with wildcards
- A persistent queue that survives paging, filter changes and Home Assistant restarts
- Every overwritten file is copied to a timestamped rollback folder first
- Rollback folders and redaction sidecars are pruned automatically — kept while newer than a
  year *or* among the twelve most recent — and a **Cleanup** section on the export card clears
  them sooner, showing what is on disk and what it would remove before you confirm

<img width="515" height="486" alt="Screenshot 2026-08-30 191338" src="https://github.com/user-attachments/assets/7d516db9-39c5-4e5f-ab27-d82a271f0b80" />

<img width="515" height="1651" alt="Screenshot 2026-08-30 191447" src="https://github.com/user-attachments/assets/2efe8aa6-432a-4c63-9c16-7ae6e969256f" />



**Privacy** *(all optional, all off by default)*

- **Redact** credential-shaped values before archiving, to make an export shareable
- **Sidecar** recording what redaction removed, written *outside* the archive so a redacted
  backup can still be restored in full
- **Encrypt** the archive, and optionally the sidecar, with a passphrase
- Restore decrypts and un-redacts transparently

Typical export: ~190 files, ~120 converted to YAML, ~4 MB compressed, about 12 seconds.

---

## Browsing the backups from your desktop

The export writes to a Samba share, so the whole history is a network drive away — no
extracting, no Home Assistant, no tooling. Map `\\homeassistant\share` on Windows,
`smb://homeassistant/share` on macOS or Linux, and open `ha_config_backup/` in whatever you
already use.

<img width="1210" height="572" alt="Screenshot 2026-08-30 173617" src="https://github.com/user-attachments/assets/efa9ed2a-1cf0-4dd7-9ece-bbc8744189b8" />

**`latest/` is an uncompressed mirror of the newest export.** Open it directly: search across
every package with your editor's project-wide find, diff a file against what is live, or copy
a fragment out of a dashboard. This is the folder most people end up using day to day, and it
is why the export is not archives-only.

**`latest/yaml/` holds the converted `.storage`.** Everything Home Assistant keeps as opaque
JSON — helpers, the entity and device registries, energy config, exposed entities — as
readable YAML. Your UI dashboards are extracted here too, one file each, in the format a
YAML-mode dashboard expects. Copy a card definition out of it and paste it into the raw
configuration editor.

**Diffing two generations needs no extraction.** Every archive contains a `MANIFEST.txt` with
a SHA-256 and size per file. Diff two manifests and the rows that differ are exactly the files
that changed between those dates — a fast answer to "what did I touch in August?" across ~190
files.

**The card links straight into the folders.** The Daily / Weekly / Monthly / Yearly buttons on
the export card open `file://` links to each tier. Browsers block those by default; one policy
per platform re-enables them, in [docs/BROWSER_LINKS.md](docs/BROWSER_LINKS.md). If you would
rather not change a browser default, the card prints the path as selectable text instead.

One trade-off worth knowing before you enable it: **encryption removes the `latest/` mirror**,
because an uncompressed copy of the same content sitting beside an encrypted archive is not
encrypted. Browsing then happens through the restore card, which reads encrypted archives
directly, rather than from your file manager.

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
| `cards/export_card.yaml` | Status, generation counts, privacy options, cleanup, "Back up now" |
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
