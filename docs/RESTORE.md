# Selective restore

Pick a generation, choose files, restore them. Everything overwritten is saved first.

## Using it

**Pick a source.** The dropdown lists every generation on disk, newest first: `Latest` (the
uncompressed mirror), then Daily, Weekly, Monthly, Yearly.

**File type** narrows a 190-file list to something usable: dashboards, top-level YAML,
packages, ESPHome, helpers and registries, or everything in `.storage`.

**Only files that differ** is on by default. Every candidate is hashed against the live file
before the list is drawn, and identical ones are hidden — restoring those would change
nothing. The subtitle shows the ratio, e.g. *3 of 190 differ from live*. Turn it off and they
appear dimmed with an `=` icon and cannot be ticked. A `+` icon means the file exists in the
archive but not live, so restoring creates it.

### Specify files → Find by path

Type or paste a path. It is forgiving about format — all of these resolve to the same file:

```
packages/mail.yaml
/config/packages/mail.yaml
config/packages/mail.yaml
Z:\ha_config_backup\latest\config\packages\mail.yaml
```

`.storage/lovelace` and `storage/lovelace` both work. Wildcards are supported, where `*`
stays inside one folder and `**` spans them:

| Pattern | Matches |
|:--|:--|
| `packages/*.yaml` | Every package, nothing deeper |
| `config/**/*.yaml` | Every YAML anywhere in the tree |
| `**/*mail*` | Anything with "mail" in the name |

On a miss the box keeps your text and a notification suggests near matches — type
`configuraton.yml` and it offers `config/configuration.yaml`.

### Specify files → Find by list

Twelve checkboxes per page, with **Search**, sortable **Name** and **Size** column headers
(tap the active one to flip direction), and paging.

Search is a case-insensitive substring match on the whole path, and `*` here *does* span
directories, so `core.*registry` finds `storage/core.entity_registry`.

Tick what you want, press **Add to queue**, then change page, filter, search or even
generation and tick more. The queue is a file on disk, so it survives all of that and a Home
Assistant restart. Queueing a file identical to the live one is refused rather than silently
accepted.

### Restoring

Open **Restore**, tap the **Arm restore** pill — it turns red and counts down from five
minutes — then press the red button that appears.

## What a restore does

For each queued file: copy the current file to `_restore_rollback/<timestamp>/`, write the
archived version in its place, empty the queue, and raise a notification saying what changed
and where the rollback went. An export runs first, so there is always a snapshot of the state
you are leaving.

**Nothing takes effect until you reload or restart.** YAML needs the relevant reload or a
restart. `.storage` always needs a full restart, and a prompt one — Home Assistant holds
those in memory and will rewrite your restored file on shutdown if you wait.

## Safety

**Arming.** The restore button does not exist until the timer is running, and it expires
after five minutes. A restore cancels it.

**A separate switch for `.storage`.** Restoring `core.entity_registry` or `auth` from three
weeks ago can rename half your entities or lock you out entirely. Those files are flagged red
with a warning triangle, and every `.storage` file stays skipped unless you deliberately turn
on "Allow .storage restores".

**Rollback always.** There is no mode where the previous version is not kept.

**Only verbatim files are offered.** The export also contains a `yaml/` tree of the JSON
converted to YAML. That is a reading room, not an archive — restoring it would put YAML where
Home Assistant expects JSON — so the restore engine only offers `config/` and `storage/`,
which are byte-exact copies.

## Worth knowing before you need it

**Restoring `storage/lovelace` replaces an entire dashboard**, not part of one. For a single
broken card, open `latest/lovelace/<name>.yaml` — the readable conversion — find the card and
paste it into the dashboard's raw configuration editor. Home Assistant converts it back to
storage JSON itself, which is the supported path and surgical rather than wholesale.

**Old generations may predate an integration.** Restoring a two-month-old
`core.config_entries` removes anything added since.

**Rollbacks are pruned, but generously.** Each restore makes a new timestamped folder under
`_restore_rollback/`. A folder is kept while it is *either* newer than a year *or* among the
twelve most recent — so a quiet year keeps everything, and a busy week of restores does not
evict last month's safety net. Only when both tests fail is a folder removed. Adjust
`KEEP_ROLLBACKS` and `KEEP_ROLLBACK_DAYS` at the top of `ha_config_restore.py`.

Sidecars follow the same rule, since they outlive the generation they belong to.

## If a restore makes things worse

Everything you overwrote is in `_restore_rollback/<timestamp>/`, in the same `config/` and
`storage/` layout. Copy it back and restart. If Home Assistant will not start, from the
Terminal add-on:

```bash
cp /share/ha_config_backup/_restore_rollback/2026-08-18_235011/config/configuration.yaml /config/
ha core restart
```

## Encrypted and redacted generations

Encrypted archives show a padlock in the generation picker and are decrypted in memory using
the passphrase from `/share/ha_config_backup/.passphrase`. Nothing is written to the share in
plaintext.

If a generation was redacted and its sidecar still exists, restore puts the original values
back automatically. Pass `--no-unredact` to leave the tokens in place instead. If the sidecar
is missing, the restored file keeps its `__CE_REDACTED_nnnn__` placeholders — which is worth
knowing before you restore a redacted `secrets.yaml` over a working one.

## Command line

Both scripts run standalone:

```
ha_config_backup.py                 export and rotate
ha_config_backup.py --check         diagnostics
ha_config_backup.py --report        JSON inventory

ha_config_restore.py --generations
ha_config_restore.py --browse --generation latest --group "All files" --page 1 --only-changed
ha_config_restore.py --queue-path "packages/*.yaml"
ha_config_restore.py --queue-list
ha_config_restore.py --restore-queue [--allow-storage]
```
