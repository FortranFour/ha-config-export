# Installation

About 20 minutes. Home Assistant OS or Supervised.

## 1. A place to write

The export needs a folder the Home Assistant **Core container** can write to, outside
`/config` so updates cannot touch it. `/share` is the usual answer.

Install the **Samba share** add-on (Settings → Add-ons → Add-on Store), set a username and
password in its Configuration tab, start it and enable "Start on boot".

Map it from your desktop:

- **Windows** — File Explorer → This PC → Map network drive → `\\homeassistant\share`
- **macOS** — Finder → Go → Connect to Server → `smb://homeassistant/share`
- **Linux** — `smb://homeassistant/share`, or an `/etc/fstab` cifs entry

If the hostname does not resolve, use the IP address.

## 2. The scripts

Create a folder `ha_config_backup` in the share and copy both scripts into it:

```
\\homeassistant\share\ha_config_backup\ha_config_backup.py
\\homeassistant\share\ha_config_backup\ha_config_restore.py
```

They locate their own directory, so if you put them somewhere else the generations follow —
just update the paths in the packages.

## 3. The packages

Requires `packages: !include_dir_named packages` in your `configuration.yaml`:

```yaml
homeassistant:
  packages: !include_dir_named packages
```

Copy both files into `/config/packages/`, then Developer Tools → YAML → **Reload all**, or
restart.

Before reloading, open `config_yaml_export.yaml` and either replace
`notify.mobile_app_YOUR_PHONE` with your own notify service or delete that action. Failures
also raise a persistent notification, so nothing is lost if you delete it.

## 4. Check it before scheduling it

Developer Tools → Actions → `shell_command.config_yaml_export_check`, run it in YAML mode
and read the response. It reports the interpreter, whether the destination is writable, which
config directory it resolved, how many files it would copy, and whether Frigate's API
answered.

That one command tells you what is wrong before anything is scheduled. If it says the config
directory resolved to NONE, add your path to `CONFIG_CANDIDATES` at the top of the script.

## 5. The cards

Add both as Manual cards. Requires these HACS frontend cards: `stack-in-card`, `mushroom`,
`button-card`, `card-mod`.

Then **set a run time** on the export card — `input_datetime.config_export_time` starts empty
and the schedule will not fire until it has a value.

Press **Back up now** and check `\\homeassistant\share\ha_config_backup\latest\`.

## 6. Optional: clickable folder buttons

The Daily / Weekly / Monthly / Yearly buttons open `file://` links, which browsers block by
default. See [BROWSER_LINKS.md](BROWSER_LINKS.md).

## Optional: redaction and encryption

Both are off by default. The **Privacy & encryption** section of the export card turns on
redaction, the sidecar that makes redaction reversible, and encryption of the archive and/or
the sidecar, and holds the passphrase box.

Read [PRIVACY.md](PRIVACY.md) first — encryption is a real boundary, redaction is not, and the
passphrase is stored as a key file rather than a prompt.

## What gets skipped

`custom_components/`, `www/`, `deps/`, the database, logs, `.storage/core.restore_state`,
saved traces, SQLite scratch files, and anything over 25 MB. All tunable at the top of
`ha_config_backup.py`.

## Retention

7 daily, 4 weekly, 12 monthly, 5 yearly, in `KEEP` at the top of the script.

Weeks are ISO 8601, so they start Monday. The first run of a new period is promoted into that
tier and then frozen, so a weekly is genuinely a snapshot from that week rather than a rolling
duplicate of the newest daily. Pruning is by count, not age: if Home Assistant is down for a
week, nothing is deleted to make room — the tier just reaches further back.

Promoted copies are hardlinks, so a snapshot living in four tiers occupies disk once. That is
why the card can say "28 generations (7 unique)".
