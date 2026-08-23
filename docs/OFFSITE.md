# Copies beyond the Home Assistant machine

By default every generation lives on the same disk as Home Assistant. That covers bad config
edits, botched updates and wrecked dashboards. It does not cover disk failure.

`extras/` has two optional Windows scripts.

## backup_ha_exports.bat — copy to your PC

Robocopies the export folder from the Samba share to a local drive, with logging and log
rotation. Schedule it with Task Scheduler for a little after your export runs.

Two things that are easy to get wrong:

- **Use UNC paths, not a mapped drive letter.** A mapped drive only exists inside your
  interactive session, so a task set to "run whether user is logged on or not" cannot see
  `Z:`. The script uses `\\homeassistant\share`.
- **Store the share credentials in Windows Credential Manager** for the account running the
  task, or it works while you are logged in and fails overnight.

`MODE` is the decision worth thinking about. `/E` (default) never deletes, so your PC becomes
a deeper archive than the server. `/MIR` mirrors exactly, including deletions — so when the
server prunes, your copy loses it too.

## cloud_offsite_ha_backups.ps1 — encrypted copy to the cloud

Wraps each new generation in a 7-Zip archive with AES-256 and encrypted headers, and drops it
in your OneDrive folder. The sync client does the uploading, retrying and resumption.

**Encryption is not optional here.** These exports contain `secrets.yaml`, `.storage/auth*`,
API tokens and any credentials your integrations hold. Syncing that in the clear puts your
home's credentials in someone else's datacentre.

The passphrase is stored with Windows DPAPI, tied to your user on that machine, so the script
can read it unattended. **Write the passphrase down somewhere physical.** DPAPI is
machine-bound: if the PC dies, the key file dies with it and the cloud archives become
permanently unreadable.

Generations are immutable once written, so each is encrypted once and skipped forever after.
