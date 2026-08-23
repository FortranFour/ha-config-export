#!/usr/bin/env python3
"""
ha_config_backup.py
===================

Generational (GFS) export of every user-configurable Home Assistant
YAML/JSON configuration file, with UI-managed JSON (.storage) converted
to YAML.

Designed to live in the Samba share (e.g. /share/ha_config_backup/) so
that Home Assistant OS / Core updates can never touch it.  It writes its
generations next to itself, wherever it is placed.

Runtime requirements: python3 only.  PyYAML is used when available (it is,
inside the Home Assistant Core container); otherwise a built-in emitter
handles the JSON -> YAML conversion.

Retention (configurable below):
    daily   x 7
    weekly  x 4
    monthly x 12
    yearly  x 5

Output layout (BACKUP_ROOT):
    ha_config_backup.py
    latest/                       uncompressed mirror of the newest run
    daily/   ha-config-2026-08-16.tar.gz
    weekly/  ha-config-2026-W33.tar.gz
    monthly/ ha-config-2026-08.tar.gz
    yearly/  ha-config-2026.tar.gz
    logs/    backup.log, last_run.json

Snapshot layout (inside each archive / inside latest/):
    config/      verbatim copies of *.yaml, *.yml, *.json from the HA config dir
    storage/     verbatim copies of .storage/* (JSON, no extension)
    addon_configs/  add-on configs, when reachable (SSH add-on only)
    yaml/        YAML conversions of every JSON file above, same tree shape
    lovelace/    ready-to-use dashboard YAML, unwrapped from .storage/lovelace*
    MANIFEST.txt file inventory with sizes and SHA-256

NOTE: nothing is redacted, by design.  secrets.yaml, .storage/auth*,
API tokens and password hashes are all included, so the destination
folder deserves the same protection as the HA config directory itself.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tarfile
import traceback
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

_env_root = os.environ.get("HA_YAML_BACKUP_ROOT")
BACKUP_ROOT = Path(_env_root) if _env_root else Path(__file__).resolve().parent

KEEP = {"daily": 7, "weekly": 4, "monthly": 12, "yearly": 5}

# tar.gz keeps 28 generations small; set False to store plain folders instead.
COMPRESS = True

# Keep an uncompressed copy of the newest run for browsing/diffing from Windows.
KEEP_LATEST_MIRROR = True

# Where the HA configuration directory might be mounted, in priority order.
CONFIG_CANDIDATES = ["/config", "/homeassistant", "/usr/share/hassio/homeassistant"]

# Reachable only from the Advanced SSH & Web Terminal add-on; skipped silently
# when running as a shell_command inside the HA Core container.
ADDON_CONFIG_DIRS = ["/addon_configs"]

# Add-on configs the HA Core container cannot read from disk, but which the
# add-on will hand over via HTTP. Add-ons are reachable at their slug with
# underscores turned into dashes. Frigate's raw config includes camera and
# go2rtc credentials; nothing is redacted here, same as everywhere else.
# Each entry tries its urls in order and stops at the first that answers.
HTTP_SOURCES = [
    {
        "name": "frigate/config.yml",
        "urls": [
            "http://ccab4aaf-frigate-fa:5000/api/config/raw",   # Frigate Full Access
            "http://ccab4aaf-frigate:5000/api/config/raw",      # Frigate
        ],
        # Set only if your Frigate requires a login for API calls. Either paste
        # the token here or leave it and export FRIGATE_TOKEN in the environment.
        "token": "",
        "token_env": "FRIGATE_TOKEN",
    },
]
HTTP_TIMEOUT = 20

# File extensions collected from the config tree.
CONFIG_EXTENSIONS = {".yaml", ".yml", ".json"}

# Directory names skipped anywhere in the tree (code, caches, media, DBs).
SKIP_DIRS = {
    ".git", ".github", ".venv", "__pycache__", "node_modules", "deps",
    "custom_components", "www", "tts", "image", "tmp", ".cloud",
    "backups", "home-assistant_v2.db", ".storage",  # .storage handled separately
}

# .storage entries that are machine state, not configuration.
SKIP_STORAGE = {
    "core.restore_state",
    "trace.saved_traces",
}

# .storage scratch files: SQLite databases and their journals, temp files.
SKIP_STORAGE_SUFFIXES = (".db", ".db-shm", ".db-wal", ".db-journal")
SKIP_STORAGE_PREFIXES = ("tmp",)

# Skip anything larger than this (regenerable caches such as hacs.data).
MAX_FILE_MB = 25

FILE_PREFIX = "ha-config-"

# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

LOG_DIR = BACKUP_ROOT / "logs"
LOG_FILE = LOG_DIR / "backup.log"
MAX_LOG_LINES = 2000

_log_lines: list[str] = []


def log(msg: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    _log_lines.append(line)
    print(line, flush=True)


def flush_log() -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        old: list[str] = []
        if LOG_FILE.exists():
            old = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        lines = (old + _log_lines)[-MAX_LOG_LINES:]
        LOG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception as err:
        # Never let logging hide the real failure.
        print(f"Could not write {LOG_FILE}: {err}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# JSON -> YAML
# --------------------------------------------------------------------------

try:
    import yaml  # type: ignore

    def to_yaml(obj) -> str:
        return yaml.safe_dump(
            obj,
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
            width=1000,
        )

    YAML_ENGINE = "PyYAML"

except ImportError:  # pragma: no cover - fallback for minimal environments

    def _plain_ok(s: str) -> bool:
        """True when a string can be written as an unquoted YAML scalar."""
        if s == "" or s.strip() != s:
            return False
        if s[0] in "-?:,[]{}#&*!|>'\"%@`":
            return False
        if ": " in s or " #" in s or "\n" in s or "\t" in s or s.endswith(":"):
            return False
        if s.lower() in ("true", "false", "null", "yes", "no", "on", "off", "~"):
            return False
        try:
            float(s)
            return False
        except ValueError:
            return True

    def _scalar(v) -> str:
        if v is None:
            return "null"
        if v is True:
            return "true"
        if v is False:
            return "false"
        if isinstance(v, (int, float)):
            return repr(v)
        s = str(v)
        # json.dumps produces a valid YAML double-quoted scalar.
        return s if _plain_ok(s) else json.dumps(s, ensure_ascii=False)

    def _emit(obj, indent: int, out: list[str]) -> None:
        pad = "  " * indent
        if isinstance(obj, dict):
            if not obj:
                if out:
                    out[-1] += " {}"
                else:
                    out.append("{}")
                return
            for key, val in obj.items():
                k = _scalar(str(key))
                if isinstance(val, (dict, list)) and val:
                    out.append(f"{pad}{k}:")
                    _emit(val, indent + 1, out)
                elif isinstance(val, dict):
                    out.append(f"{pad}{k}: {{}}")
                elif isinstance(val, list):
                    out.append(f"{pad}{k}: []")
                else:
                    out.append(f"{pad}{k}: {_scalar(val)}")
        elif isinstance(obj, list):
            if not obj:
                if out:
                    out[-1] += " []"
                else:
                    out.append("[]")
                return
            for item in obj:
                if isinstance(item, dict) and not item:
                    out.append(f"{pad}- {{}}")
                elif isinstance(item, list) and not item:
                    out.append(f"{pad}- []")
                elif isinstance(item, (dict, list)):
                    out.append(f"{pad}-")
                    _emit(item, indent + 1, out)
                else:
                    out.append(f"{pad}- {_scalar(item)}")
        else:
            out.append(f"{pad}{_scalar(obj)}")

    def to_yaml(obj) -> str:
        out: list[str] = []
        _emit(obj, 0, out)
        return "\n".join(out) + "\n"

    YAML_ENGINE = "builtin"


# --------------------------------------------------------------------------
# Source discovery
# --------------------------------------------------------------------------


def resolve_config_dir() -> Path:
    override = os.environ.get("HA_CONFIG_DIR")
    if override:
        return Path(override)
    for cand in CONFIG_CANDIDATES:
        p = Path(cand)
        if (p / "configuration.yaml").is_file():
            return p
    raise RuntimeError(
        "Could not locate the Home Assistant configuration directory. Looked for "
        "configuration.yaml in: " + ", ".join(CONFIG_CANDIDATES)
        + ". Set HA_CONFIG_DIR to override."
    )


def iter_config_files(root: Path):
    """Yield (absolute_path, relative_path) for config YAML/JSON files."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in sorted(filenames):
            src = Path(dirpath) / name
            if src.suffix.lower() not in CONFIG_EXTENSIONS:
                continue
            if src.is_symlink() or not src.is_file():
                continue
            yield src, src.relative_to(root)


def iter_storage_files(storage: Path):
    if not storage.is_dir():
        return
    for entry in sorted(storage.iterdir()):
        if not entry.is_file() or entry.is_symlink():
            continue
        if entry.name in SKIP_STORAGE:
            continue
        if entry.name.endswith(SKIP_STORAGE_SUFFIXES):
            continue
        if entry.name.startswith(SKIP_STORAGE_PREFIXES):
            continue
        if ".corrupt." in entry.name:
            continue
        yield entry, Path(entry.name)


def iter_addon_configs():
    for base in ADDON_CONFIG_DIRS:
        root = Path(base)
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in sorted(filenames):
                src = Path(dirpath) / name
                if src.suffix.lower() not in CONFIG_EXTENSIONS:
                    continue
                if src.is_symlink() or not src.is_file():
                    continue
                yield src, src.relative_to(root)


# --------------------------------------------------------------------------
# Snapshot construction
# --------------------------------------------------------------------------


class Stats:
    def __init__(self) -> None:
        self.copied = 0
        self.converted = 0
        self.non_json = 0
        self.yaml_files = 0   # sources that were already YAML
        self.json_files = 0   # sources that were JSON (and got a YAML twin)
        self.skipped_large = 0
        self.fetched = 0
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.bytes = 0


def looks_like_json(path: Path) -> bool:
    """.storage holds certificates, pickles and keys alongside its JSON.

    Those are copied verbatim; attempting to convert them is not an error.
    """
    try:
        with path.open("rb") as handle:
            head = handle.read(64).lstrip()
    except OSError:
        return False
    return head[:1] in (b"{", b"[")


def copy_one(src: Path, dest: Path, stats: Stats) -> bool:
    try:
        size = src.stat().st_size
        if size > MAX_FILE_MB * 1024 * 1024:
            stats.skipped_large += 1
            log(f"  skip (>{MAX_FILE_MB}MB): {src}")
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        stats.copied += 1
        stats.bytes += size
        return True
    except Exception as err:
        stats.errors.append(f"copy {src}: {err}")
        return False


def convert_one(src: Path, yaml_dest: Path, stats: Stats):
    """Convert a JSON file to YAML. Returns the parsed object, or None."""
    try:
        raw = src.read_text(encoding="utf-8", errors="replace")
        data = json.loads(raw)
    except Exception as err:
        stats.errors.append(f"parse {src}: {err}")
        return None
    try:
        yaml_dest.parent.mkdir(parents=True, exist_ok=True)
        header = (
            f"# Converted from JSON: {src}\n"
            f"# {datetime.now():%Y-%m-%d %H:%M:%S} via {YAML_ENGINE}\n"
        )
        yaml_dest.write_text(header + to_yaml(data), encoding="utf-8")
        stats.converted += 1
    except Exception as err:
        stats.errors.append(f"convert {src}: {err}")
    return data


def extract_lovelace(name: str, data, snap: Path, stats: Stats) -> None:
    """Unwrap .storage/lovelace* into a directly usable dashboard YAML."""
    if not isinstance(data, dict):
        return
    config = data.get("data", {}).get("config") if isinstance(data.get("data"), dict) else None
    if config is None:
        return
    label = "default" if name == "lovelace" else name.split(".", 1)[-1]
    dest = snap / "lovelace" / f"{label}.yaml"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        header = (
            f"# Dashboard '{label}' unwrapped from .storage/{name}\n"
            "# This is the YAML-mode equivalent: paste under a lovelace:\n"
            "# dashboards: entry, or use directly as a dashboard file.\n"
        )
        dest.write_text(header + to_yaml(config), encoding="utf-8")
        log(f"  lovelace dashboard extracted: {label}.yaml")
    except Exception as err:
        stats.errors.append(f"lovelace {name}: {err}")


def fetch_http_source(source: dict) -> tuple[bytes | None, str]:
    """Return (content, note). Tries each url until one answers."""
    token = source.get("token") or os.environ.get(source.get("token_env", ""), "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    attempts = []
    for url in source["urls"]:
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                return response.read(), url
        except urllib.error.HTTPError as err:
            hint = " (needs a token — see HTTP_SOURCES)" if err.code in (401, 403) else ""
            attempts.append(f"{url} -> HTTP {err.code}{hint}")
        except Exception as err:
            attempts.append(f"{url} -> {type(err).__name__}: {err}")
    return None, "; ".join(attempts)


def fetch_http_sources(snap: Path, stats: Stats) -> None:
    for source in HTTP_SOURCES:
        content, note = fetch_http_source(source)
        if content is None:
            stats.warnings.append(f"{source['name']}: {note}")
            log(f"  could not fetch {source['name']} — {note}")
            continue
        dest = snap / "external" / source["name"]
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(content)
            stats.fetched += 1
            stats.bytes += len(content)
            log(f"  fetched {source['name']} from {note} ({len(content)} bytes)")
        except Exception as err:
            stats.warnings.append(f"{source['name']}: {err}")


def build_snapshot(snap: Path, config_dir: Path) -> Stats:
    stats = Stats()

    log(f"Config directory: {config_dir}")

    # 1. YAML/JSON from the config tree
    for src, rel in iter_config_files(config_dir):
        if not copy_one(src, snap / "config" / rel, stats):
            continue
        if src.suffix.lower() == ".json":
            stats.json_files += 1
            convert_one(src, snap / "yaml" / "config" / rel.with_suffix(".yaml"), stats)
        else:
            stats.yaml_files += 1

    # 2. .storage (UI-managed configuration, JSON without extensions)
    storage = config_dir / ".storage"
    for src, rel in iter_storage_files(storage):
        if not copy_one(src, snap / "storage" / rel, stats):
            continue
        if not looks_like_json(src):
            # Certificates, keys, pickles: kept verbatim, nothing to convert.
            stats.non_json += 1
            continue
        stats.json_files += 1
        data = convert_one(src, snap / "yaml" / "storage" / f"{rel.name}.yaml", stats)
        if data is not None and (rel.name == "lovelace" or rel.name.startswith("lovelace.")):
            extract_lovelace(rel.name, data, snap, stats)

    # 3. Add-on configs (ESPHome etc.) when this process can reach them
    addon_count = 0
    for src, rel in iter_addon_configs():
        if copy_one(src, snap / "addon_configs" / rel, stats):
            addon_count += 1
            if src.suffix.lower() == ".json":
                convert_one(
                    src, snap / "yaml" / "addon_configs" / rel.with_suffix(".yaml"), stats
                )
    if addon_count:
        log(f"Add-on config files: {addon_count}")
    else:
        log("Add-on configs not reachable from this context — trying HTTP instead")

    # 4. Configs only reachable over the add-on's own API (Frigate)
    fetch_http_sources(snap, stats)

    return stats


def write_manifest(snap: Path, config_dir: Path, stats: Stats) -> None:
    lines = [
        "Home Assistant configuration export",
        f"Generated : {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"Host      : {os.uname().nodename}",
        f"Source    : {config_dir}",
        f"YAML via  : {YAML_ENGINE}",
        f"Files     : {stats.copied} copied "
        f"({stats.yaml_files} YAML, {stats.json_files} JSON, {stats.non_json} other), "
        f"{stats.converted} JSON converted to YAML, {stats.fetched} fetched over HTTP",
        f"Size      : {stats.bytes / 1024 / 1024:.1f} MiB (source bytes)",
        "",
        "SHA-256                                                           SIZE  PATH",
        "-" * 100,
    ]
    for path in sorted(snap.rglob("*")):
        if not path.is_file():
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.stat().st_size:>9}  {path.relative_to(snap)}")
    if stats.warnings:
        lines += ["", "WARNINGS", "-" * 100] + stats.warnings
    if stats.errors:
        lines += ["", "PROBLEMS", "-" * 100] + stats.errors
    (snap / "MANIFEST.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# Generations
# --------------------------------------------------------------------------


def tier_tags(now: datetime) -> dict[str, str]:
    return {
        "daily": now.strftime("%Y-%m-%d"),
        "weekly": now.strftime("%G-W%V"),
        "monthly": now.strftime("%Y-%m"),
        "yearly": now.strftime("%Y"),
    }


def suffix() -> str:
    return ".tar.gz" if COMPRESS else ""


def store(snap: Path, dest: Path) -> None:
    """Write the staged snapshot to dest as an archive or a folder."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        if dest.is_dir():
            shutil.rmtree(dest)
        else:
            dest.unlink()
    if COMPRESS:
        tmp = dest.with_suffix(dest.suffix + ".part")
        with tarfile.open(tmp, "w:gz") as tar:
            tar.add(snap, arcname=dest.name.replace(".tar.gz", ""))
        tmp.replace(dest)
    else:
        shutil.copytree(snap, dest)


def duplicate(source: Path, dest: Path) -> None:
    """Hardlink where possible so promoted generations cost no extra space."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        return
    try:
        if source.is_dir():
            shutil.copytree(source, dest, copy_function=os.link)
        else:
            os.link(source, dest)
    except OSError:
        if source.is_dir():
            shutil.copytree(source, dest)
        else:
            shutil.copy2(source, dest)


def prune(tier: str, keep: int) -> None:
    tier_dir = BACKUP_ROOT / tier
    if not tier_dir.is_dir():
        return
    entries = sorted(p for p in tier_dir.iterdir() if p.name.startswith(FILE_PREFIX))
    for old in entries[:-keep] if keep else entries:
        try:
            if old.is_dir():
                shutil.rmtree(old)
            else:
                old.unlink()
            log(f"  pruned {tier}/{old.name}")
        except Exception as err:
            log(f"  could not prune {old}: {err}")


# --------------------------------------------------------------------------
# Inventory report (--report), consumed by the dashboard sensor
# --------------------------------------------------------------------------


def _dir_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def build_report() -> dict:
    """Inventory every generation. Printed as JSON for a command_line sensor."""
    report: dict = {
        "root": str(BACKUP_ROOT),
        "generated": datetime.now().isoformat(timespec="seconds"),
    }
    seen_inodes: set[int] = set()
    unique_bytes = 0
    unique_count = 0

    for tier, keep in KEEP.items():
        tier_dir = BACKUP_ROOT / tier
        entries = []
        if tier_dir.is_dir():
            entries = sorted(
                p for p in tier_dir.iterdir() if p.name.startswith(FILE_PREFIX)
            )
        files = []
        tier_bytes = 0
        for p in entries:
            try:
                st = p.stat()
            except OSError:
                continue
            size = st.st_size if p.is_file() else _dir_size(p)
            tier_bytes += size
            # Promoted generations are hardlinks; count each inode once.
            if p.is_file():
                if st.st_ino not in seen_inodes:
                    seen_inodes.add(st.st_ino)
                    unique_bytes += size
                    unique_count += 1
            else:
                unique_bytes += size
                unique_count += 1
            files.append(
                {
                    "name": p.name,
                    "mb": round(size / 1048576, 2),
                    "modified": datetime.fromtimestamp(st.st_mtime).strftime(
                        "%Y-%m-%d %H:%M"
                    ),
                }
            )
        report[tier] = {
            "count": len(files),
            "keep": keep,
            "mb": round(tier_bytes / 1048576, 2),
            "path": str(tier_dir),
            "newest": files[-1]["name"] if files else None,
            "newest_modified": files[-1]["modified"] if files else None,
            "oldest": files[0]["name"] if files else None,
            "files": files,
        }

    report["total_mb"] = round(unique_bytes / 1048576, 2)
    # Tier entries vs. actual snapshots: a run promoted into several tiers is
    # one file hardlinked N ways, so these differ until the tiers diverge.
    report["total_generations"] = sum(report[tier]["count"] for tier in KEEP)
    report["unique_generations"] = unique_count

    last_run_file = LOG_DIR / "last_run.json"
    if last_run_file.is_file():
        try:
            report["last_run"] = json.loads(last_run_file.read_text(encoding="utf-8"))
        except Exception:
            report["last_run"] = {}
    else:
        report["last_run"] = {}

    report["status"] = report["last_run"].get("status", "never")
    return report


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main() -> int:
    started = datetime.now()
    # Created first so that even an immediate failure leaves a log behind.
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log("=" * 68)
    log(f"Starting configuration export -> {BACKUP_ROOT}")

    config_dir = resolve_config_dir()
    staging = BACKUP_ROOT / ".staging"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)

    try:
        stats = build_snapshot(staging, config_dir)
        write_manifest(staging, config_dir, stats)

        tags = tier_tags(started)
        daily_dest = BACKUP_ROOT / "daily" / f"{FILE_PREFIX}{tags['daily']}{suffix()}"
        store(staging, daily_dest)
        log(f"Wrote {daily_dest.relative_to(BACKUP_ROOT)}")

        for tier in ("weekly", "monthly", "yearly"):
            dest = BACKUP_ROOT / tier / f"{FILE_PREFIX}{tags[tier]}{suffix()}"
            if dest.exists():
                continue
            duplicate(daily_dest, dest)
            log(f"Promoted to {tier}/{dest.name}")

        for tier, keep in KEEP.items():
            prune(tier, keep)

        if KEEP_LATEST_MIRROR:
            latest = BACKUP_ROOT / "latest"
            tmp_latest = BACKUP_ROOT / ".latest.new"
            if tmp_latest.exists():
                shutil.rmtree(tmp_latest)
            shutil.copytree(staging, tmp_latest)
            if latest.exists():
                shutil.rmtree(latest)
            tmp_latest.replace(latest)
            log("Refreshed latest/ mirror")

        elapsed = (datetime.now() - started).total_seconds()
        archived = daily_dest.stat().st_size if daily_dest.is_file() else 0
        log(
            f"Done in {elapsed:.1f}s — {stats.copied} files "
            f"({stats.yaml_files} YAML, {stats.json_files} JSON, {stats.non_json} other), "
            f"{stats.converted} converted, "
            f"{stats.fetched} fetched, {len(stats.warnings)} warning(s), "
            f"{len(stats.errors)} problem(s), "
            f"archive {archived / 1024 / 1024:.1f} MiB"
        )

        LOG_DIR.mkdir(parents=True, exist_ok=True)
        (LOG_DIR / "last_run.json").write_text(
            json.dumps(
                {
                    "timestamp": started.isoformat(timespec="seconds"),
                    "status": "error" if stats.errors else "ok",
                    "files": stats.copied,
                    "converted": stats.converted,
                    "yaml_files": stats.yaml_files,
                    "json_files": stats.json_files,
                    "non_json": stats.non_json,
                    "fetched": stats.fetched,
                    "skipped_large": stats.skipped_large,
                    "warnings": len(stats.warnings),
                    "warning_detail": stats.warnings[:10],
                    "errors": len(stats.errors),
                    "error_detail": stats.errors[:20],
                    "archive_bytes": archived,
                    "duration_seconds": round(elapsed, 1),
                    "yaml_engine": YAML_ENGINE,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return 1 if stats.errors else 0

    finally:
        shutil.rmtree(staging, ignore_errors=True)


def diagnostics() -> str:
    """Environment check. Prints to stdout so it shows up in the HA action response."""
    lines = [
        f"python      : {sys.version.split()[0]} at {sys.executable}",
        f"yaml engine : {YAML_ENGINE}",
        f"script path : {Path(__file__).resolve()}",
        f"backup root : {BACKUP_ROOT}  (exists={BACKUP_ROOT.is_dir()})",
    ]

    probe = BACKUP_ROOT / ".write_test"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        lines.append("writable    : yes")
    except Exception as err:
        lines.append(f"writable    : NO — {err}")

    lines.append("config dir candidates:")
    found = None
    for cand in CONFIG_CANDIDATES:
        p = Path(cand)
        marker = p / "configuration.yaml"
        state = (
            "configuration.yaml found" if marker.is_file()
            else ("directory exists, no configuration.yaml" if p.is_dir() else "missing")
        )
        lines.append(f"  {cand:<36} {state}")
        if found is None and marker.is_file():
            found = p
    lines.append(f"HA_CONFIG_DIR env: {os.environ.get('HA_CONFIG_DIR') or '(unset)'}")

    if found:
        storage = found / ".storage"
        n_storage = len(list(storage.iterdir())) if storage.is_dir() else 0
        n_yaml = sum(1 for _ in iter_config_files(found))
        lines.append(f"resolved    : {found}")
        lines.append(f"  YAML/JSON files that would be copied: {n_yaml}")
        lines.append(f"  .storage entries visible: {n_storage}")
    else:
        lines.append("resolved    : NONE — this is why the export produces nothing")

    for extra in ADDON_CONFIG_DIRS:
        lines.append(f"{extra:<12}: {'reachable' if Path(extra).is_dir() else 'not reachable (normal from HA Core)'}")

    for source in HTTP_SOURCES:
        content, note = fetch_http_source(source)
        if content is None:
            lines.append(f"http source : {source['name']} UNREACHABLE — {note}")
        else:
            lines.append(f"http source : {source['name']} ok ({len(content)} bytes from {note})")

    try:
        usage = shutil.disk_usage(BACKUP_ROOT if BACKUP_ROOT.is_dir() else Path("/"))
        lines.append(f"free space  : {usage.free / 1024 / 1024 / 1024:.1f} GiB")
    except Exception as err:
        lines.append(f"free space  : unknown — {err}")

    return "\n".join(lines)


if __name__ == "__main__":
    if "--check" in sys.argv:
        print(diagnostics())
        sys.exit(0)
    if "--report" in sys.argv:
        # Read-only inventory: no logging, no side effects.
        print(json.dumps(build_report()))
        sys.exit(0)
    try:
        code = main()
    except KeyboardInterrupt:
        log("Interrupted")
        code = 130
    except BaseException:
        # BaseException, not Exception: SystemExit used to escape and skip the log.
        log("FATAL\n" + traceback.format_exc())
        code = 2
    flush_log()
    sys.exit(code)
