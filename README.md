# Home Assistant Configuration Export & Restore

Generational backups of every user-configurable Home Assistant YAML **and JSON** file, with
the UI-managed JSON in `.storage` converted to readable YAML — plus selective, file-by-file
restore from any generation.

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
- Pick files from a paged, sortable, searchable list, or type a path with wildcards
- A persistent queue that survives paging, filter changes and Home Assistant restarts
- Every overwritten file is copied to a timestamped rollback folder first

Typical export here: ~190 files, ~120 converted to YAML, ~4 MB compressed, about 12 seconds.

---

## Screenshots

<img width="512" height="367" alt="Screenshot 2026-08-22 174840" src="https://github.com/user-attachments/assets/c749e24a-6c39-4a59-8097-2760b29248a5" />
<img width="511" height="657" alt="Screenshot 2026-08-22 174601" src="https://github.com/user-attachments/assets/91a2015e-b264-4150-9686-6a780df1b92d" />
<img width="507" height="432" alt="Screenshot 2026-08-22 174728" src="https://github.com/user-attachments/assets/34de3446-f1d8-4e54-87a3-263b91777cb3" />
<img width="512" height="1590" alt="Screenshot 2026-08-22 174652" src="https://github.com/user-attachments/assets/bdc17a2c-2463-44c2-bee4-ff4337d262a9" />
<img width="1207" height="507" alt="Screenshot 2026-08-22 185634" src="https://github.com/user-attachments/assets/7f44e59d-df7c-40ed-b195-a197c397eaa0" />

---

## Requirements

- Home Assistant OS or Supervised
- The **Samba share** add-on (or any writable path the Core container can see)
- `packages: !include_dir_named packages` in your `configuration.yaml`
- HACS frontend cards: `stack-in-card`, `mushroom`, `button-card`, `card-mod`

No Python dependencies. The scripts use PyYAML when available — it is, inside the HA Core
container — and fall back to a built-in emitter otherwise.

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
| `cards/export_card.yaml` | Status, generation counts, "Back up now" |
| `cards/restore_card.yaml` | Generation picker, search, file list, restore controls |
| `extras/` | Optional Windows scripts: copy to PC, encrypted cloud copy |
| `docs/` | Install, restore, browser links, off-site copies |

---

## Security

> [!WARNING]
> **Nothing is redacted.** `secrets.yaml`, `.storage/auth*`, API tokens and any credentials
> stored by your integrations all land in the destination in plaintext. That is what makes
> the export restore-grade, but the destination folder deserves the same protection as your
> config directory. Do not point it at anything world-readable, and if you sync it to a
> cloud provider, encrypt it first — `extras/cloud_offsite_ha_backups.ps1` does that.

---

## Licence

MIT. See [LICENSE](LICENSE).

## Credits

Developed collaboratively with Claude (Anthropic) and debugged against a live instance.
