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
import re
import shutil
import sys
import tarfile
import time
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

# Sidecars are the only thing that would otherwise grow without limit: one per
# redacted generation, and they outlive the generation they belong to. Same
# rule as the restore rollbacks — kept while EITHER newer than a year OR among
# the twelve most recent.
KEEP_SIDECARS = 12
KEEP_SIDECAR_DAYS = 365

# Rollback folders are written by ha_config_restore.py, which prunes them on
# the same rule. These constants exist so the export can report and clear them
# from the dashboard; keep the two files in step if you change them.
KEEP_ROLLBACKS = 12
KEEP_ROLLBACK_DAYS = 365

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
# Privacy options. All off by default: the export is restore-grade as shipped.
# Every one of these can be set from the dashboard card instead of here.
# --------------------------------------------------------------------------

# Replace credential-shaped values with tokens before archiving.
REDACT = False

# Write the removed values to a sidecar OUTSIDE the archive, so a redacted
# archive can be fully restored later. Without it, redaction is one-way.
WRITE_SIDECAR = True

# Encrypt the archive (and optionally the sidecar) with a passphrase.
ENCRYPT = False
ENCRYPT_SIDECAR = False

# The passphrase is read from a file, never from a Home Assistant entity: an
# entity's value is written to .storage and the recorder database, both of
# which this script backs up — encrypting a backup with a key stored inside it
# is no encryption at all.
PASSPHRASE_FILE = BACKUP_ROOT / ".passphrase"

# Keys whose values are treated as credentials, matched case-insensitively
# anywhere in a key name.
SECRET_KEY_HINTS = (
    "password", "passwd", "token", "api_key", "apikey", "secret", "private_key",
    "client_secret", "access_token", "refresh_token", "session", "cookie",
    "credential", "auth", "salt", "hash", "pin", "license",
)

# Value-shaped things worth removing wherever they appear. Order matters:
# URL credentials go first, or the email pattern swallows "user:pass@host"
# and leaves half the credential behind.
VALUE_PATTERNS = (
    ("url_credentials", re.compile(r"(?<=//)[^/\s:@]+:[^/\s:@]+(?=@)")),
    # The TLD must be alphabetic, so "token@10.0.4.109" is not mistaken for an
    # address once the credential above has been tokenised.
    ("email", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}")),
)

# JSON keys redacted by exact name rather than by hint — location is personal
# but "latitude" contains none of the credential words.
SECRET_KEY_EXACT = ("latitude", "longitude")

TOKEN = "__CE_REDACTED_{:04d}__"

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
        self.redacted_files = 0
        self.redacted_values = 0
        self.encrypted = False
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
# Passphrase, encryption
# --------------------------------------------------------------------------


def read_passphrase() -> str | None:
    """Read the passphrase from its file. Never from an entity — see above."""
    try:
        if PASSPHRASE_FILE.is_file():
            value = PASSPHRASE_FILE.read_text(encoding="utf-8").strip()
            return value or None
    except OSError as err:
        log(f"  could not read passphrase file: {err}")
    return None


def encrypt_bytes(data: bytes, passphrase: str) -> bytes:
    """scrypt-derived key, Fernet (AES-128-CBC + HMAC). Salt is prepended.

    cryptography ships with Home Assistant, so there is nothing to install.
    """
    import base64

    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    salt = os.urandom(16)
    kdf = Scrypt(salt=salt, length=32, n=2 ** 15, r=8, p=1)
    key = base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))
    return b"CEENC1" + salt + Fernet(key).encrypt(data)


def encrypt_file(path: Path, passphrase: str) -> Path:
    """Encrypt in place, returning the new .enc path. The plaintext is removed."""
    target = path.with_suffix(path.suffix + ".enc")
    target.write_bytes(encrypt_bytes(path.read_bytes(), passphrase))
    path.unlink()
    return target


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------


class Redactor:
    """Replaces credential-shaped values with tokens, remembering the originals.

    This is a best-effort filter for making an export shareable, not a security
    boundary. It works on key names and value shapes, so it will miss a secret
    stored under an unusual key. Encryption is what actually protects an export.
    """

    def __init__(self) -> None:
        self.entries: dict[str, dict[str, str]] = {}
        self._counter = 0

    def _token(self, rel: str, original, quoted: bool = True) -> str:
        """quoted=False records that the original was a bare JSON scalar.

        Coordinates are numbers; putting them back as "47.6062" would leave a
        string where Home Assistant expects a float.
        """
        self._counter += 1
        token = TOKEN.format(self._counter)
        entry = str(original) if quoted else {"value": str(original), "raw": True}
        self.entries.setdefault(rel, {})[token] = entry
        return token

    def _walk_json(self, node, rel: str):
        if isinstance(node, dict):
            out = {}
            for key, value in node.items():
                name = str(key).lower()
                if name in SECRET_KEY_EXACT and isinstance(value, (int, float, str)):
                    out[key] = self._token(
                        rel, value, quoted=isinstance(value, str)
                    )
                elif isinstance(value, str) and value and any(
                    hint in name for hint in SECRET_KEY_HINTS
                ):
                    out[key] = self._token(rel, value)
                else:
                    out[key] = self._walk_json(value, rel)
            return out
        if isinstance(node, list):
            return [self._walk_json(item, rel) for item in node]
        return node

    def redact_json(self, text: str, rel: str) -> str | None:
        try:
            data = json.loads(text)
        except Exception:
            return None
        return json.dumps(self._walk_json(data, rel), indent=2)

    def redact_text(self, text: str, rel: str) -> str:
        out_lines = []
        for line in text.splitlines(keepends=True):
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                match = re.match(r"^(\s*[\"']?)([\w.-]+)([\"']?\s*:\s*)(.+?)(\s*)$", line)
                if match and any(h in match.group(2).lower() for h in SECRET_KEY_HINTS):
                    value = match.group(4).strip()
                    if value and value not in ("{}", "[]", "null", "~"):
                        line = (match.group(1) + match.group(2) + match.group(3)
                                + self._token(rel, value) + match.group(5))
            for _name, pattern in VALUE_PATTERNS:
                line = pattern.sub(lambda m: self._token(rel, m.group(0)), line)
            out_lines.append(line)
        return "".join(out_lines)

    def apply(self, path: Path, rel: str) -> bool:
        """Redact one file in place. Returns True if anything changed."""
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False
        before = self._counter
        if path.suffix.lower() == ".json" or text.lstrip()[:1] in ("{", "["):
            new = self.redact_json(text, rel)
            if new is None:
                new = self.redact_text(text, rel)
        else:
            new = self.redact_text(text, rel)
        if self._counter == before:
            return False
        path.write_text(new, encoding="utf-8")
        return True


def redact_snapshot(snap: Path, stats: "Stats") -> Redactor:
    """Redact every verbatim copy in the staged snapshot."""
    redactor = Redactor()
    changed = 0
    for sub in ("config", "storage", "addon_configs", "external"):
        base = snap / sub
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            rel = f"{sub}/{path.relative_to(base).as_posix()}"
            if redactor.apply(path, rel):
                changed += 1
    stats.redacted_files = changed
    stats.redacted_values = redactor._counter  # noqa: SLF001
    log(f"Redacted {redactor._counter} value(s) across {changed} file(s)")
    return redactor


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


def prunable(items: list[Path], keep_n: int, keep_days: int | None) -> list[Path]:
    """Items beyond the newest keep_n, and older than keep_days if given.

    Two callers with deliberately different appetites:

      * automatic pruning passes keep_days, so an item has to fail BOTH tests.
        Conservative, because it happens without anyone watching.
      * the dashboard's Clear button passes None, so only the count applies.
        That makes manual clearing strictly more aggressive than automatic —
        otherwise the button would only ever do what the next run would have
        done anyway, which is no reason to have a button.

    Newest first by name, which is chronological given the date-stamped naming.
    """
    ordered = sorted(items, key=lambda p: p.name, reverse=True)
    cutoff = None if keep_days is None else time.time() - keep_days * 86400
    out = []
    for index, path in enumerate(ordered):
        if index < keep_n:
            continue
        if cutoff is None:
            out.append(path)
            continue
        try:
            if path.stat().st_mtime < cutoff:
                out.append(path)
        except OSError:
            pass
    return out


def dir_size(path: Path) -> int:
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                pass
    return total


def cleanup_candidates() -> dict:
    """What could be deleted right now, without deleting anything."""
    rollback_root = BACKUP_ROOT / "_restore_rollback"
    sidecar_dir = BACKUP_ROOT / "sidecars"

    rollbacks = [p for p in rollback_root.iterdir() if p.is_dir()] \
        if rollback_root.is_dir() else []
    sidecars = [p for p in sidecar_dir.glob("*.sidecar.json*") if p.is_file()] \
        if sidecar_dir.is_dir() else []

    # What the Clear button would remove: everything past the newest N.
    old_rollbacks = prunable(rollbacks, KEEP_ROLLBACKS, None)
    old_sidecars = prunable(sidecars, KEEP_SIDECARS, None)

    # What automatic pruning will remove on its own, without being asked.
    auto = (len(prunable(rollbacks, KEEP_ROLLBACKS, KEEP_ROLLBACK_DAYS))
            + len(prunable(sidecars, KEEP_SIDECARS, KEEP_SIDECAR_DAYS)))

    items = []
    for path in old_rollbacks + old_sidecars:
        items.append({
            "name": path.name,
            "kind": "rollback" if path in old_rollbacks else "sidecar",
            "mb": round(dir_size(path) / 1048576, 2),
            "age_days": int((time.time() - path.stat().st_mtime) / 86400),
        })
    items.sort(key=lambda i: i["name"])

    return {
        "eligible": len(items),
        "eligible_mb": round(sum(i["mb"] for i in items), 2),
        "auto_eligible": auto,
        "rollbacks_total": len(rollbacks),
        "rollbacks_eligible": len(old_rollbacks),
        "rollbacks_mb": round(sum(dir_size(p) for p in rollbacks) / 1048576, 2),
        "sidecars_total": len(sidecars),
        "sidecars_eligible": len(old_sidecars),
        "sidecars_mb": round(sum(dir_size(p) for p in sidecars) / 1048576, 2),
        "keep_recent": KEEP_ROLLBACKS,
        "keep_days": KEEP_ROLLBACK_DAYS,
        "oldest_kept": min((p.name for p in
                            sorted(rollbacks, key=lambda p: p.name,
                                   reverse=True)[:KEEP_ROLLBACKS]), default=None),
        "items": items[:40],
        "generated": datetime.now().isoformat(timespec="seconds"),
    }


def run_cleanup() -> dict:
    """Delete everything cleanup_candidates() lists. Nothing else is touched."""
    report = cleanup_candidates()
    removed, freed, errors = 0, 0.0, []

    rollback_root = BACKUP_ROOT / "_restore_rollback"
    sidecar_dir = BACKUP_ROOT / "sidecars"
    for item in report["items"]:
        base = rollback_root if item["kind"] == "rollback" else sidecar_dir
        path = base / item["name"]
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            removed += 1
            freed += item["mb"]
            log(f"  cleanup removed {item['kind']} {item['name']}")
        except OSError as err:
            errors.append(f"{item['name']}: {err}")

    result = {"removed": removed, "freed_mb": round(freed, 2), "errors": errors[:10]}
    log(f"Cleanup: removed {removed} item(s), freed {result['freed_mb']} MB")
    return result


def prune_sidecars() -> int:
    """Trim the sidecar folder. See KEEP_SIDECARS for the rule."""
    sidecar_dir = BACKUP_ROOT / "sidecars"
    if not sidecar_dir.is_dir():
        return 0

    files = sorted((p for p in sidecar_dir.glob("*.sidecar.json*") if p.is_file()),
                   key=lambda p: p.name, reverse=True)
    cutoff = time.time() - KEEP_SIDECAR_DAYS * 86400
    removed = 0
    for index, path in enumerate(files):
        if index < KEEP_SIDECARS:
            continue
        try:
            if path.stat().st_mtime >= cutoff:
                continue
            path.unlink()
            removed += 1
            log(f"  pruned old sidecar {path.name}")
        except OSError as err:
            log(f"  could not prune {path.name}: {err}")
    return removed


def existing_for_tag(tier_dir: Path, tag: str) -> list[Path]:
    """Every file already held for this period, whatever its extension.

    A generation can be .tar.gz, .tar.gz.enc or a plain folder depending on the
    settings in force when it was written. Matching on the tag rather than the
    full filename is what stops a run with different settings from leaving a
    second copy of the same period behind.
    """
    if not tier_dir.is_dir():
        return []
    stem = f"{FILE_PREFIX}{tag}"
    return [p for p in tier_dir.iterdir()
            if p.name == stem or p.name.startswith(f"{stem}.")]


def store(snap: Path, dest: Path) -> None:
    """Write the staged snapshot to dest as an archive or a folder."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Clear the period, not just this exact filename: switching encryption or
    # compression on or off changes the extension, and leaving the old file
    # would give the tier two copies of the same day.
    tag = dest.name[len(FILE_PREFIX):].split(".")[0]
    for stale in existing_for_tag(dest.parent, tag):
        if stale.is_dir():
            shutil.rmtree(stale)
        else:
            stale.unlink()
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

        passphrase = read_passphrase() if (ENCRYPT or ENCRYPT_SIDECAR) else None
        if (ENCRYPT or ENCRYPT_SIDECAR) and not passphrase:
            raise RuntimeError(
                f"Encryption is on but no passphrase was found in {PASSPHRASE_FILE}. "
                "Set one from the card, or create that file with the passphrase as "
                "its only line."
            )

        tags = tier_tags(started)

        redactor = redact_snapshot(staging, stats) if REDACT else None

        if redactor and WRITE_SIDECAR and redactor.entries:
            # Deliberately outside the archive. A sidecar sitting next to a
            # redacted backup would reduce redaction to obfuscation — the point
            # is that the archive can be shared or synced while this file stays
            # behind or travels separately.
            sidecar_dir = BACKUP_ROOT / "sidecars"
            sidecar_dir.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(
                {"generation": f"{FILE_PREFIX}{tags['daily']}",
                 "created": started.isoformat(timespec="seconds"),
                 "entries": redactor.entries}, indent=2).encode("utf-8")
            sidecar = sidecar_dir / f"{FILE_PREFIX}{tags['daily']}.sidecar.json"
            if ENCRYPT_SIDECAR:
                sidecar = sidecar.with_suffix(".json.enc")
                sidecar.write_bytes(encrypt_bytes(payload, passphrase))
            else:
                sidecar.write_bytes(payload)
            try:
                os.chmod(sidecar, 0o600)
            except OSError:
                pass
            log(f"Sidecar written: {sidecar.name}"
                f"{' (encrypted)' if ENCRYPT_SIDECAR else ''}")
            prune_sidecars()

        daily_dest = BACKUP_ROOT / "daily" / f"{FILE_PREFIX}{tags['daily']}{suffix()}"
        store(staging, daily_dest)
        if ENCRYPT and daily_dest.is_file():
            daily_dest = encrypt_file(daily_dest, passphrase)
            stats.encrypted = True
        log(f"Wrote {daily_dest.relative_to(BACKUP_ROOT)}")

        # Promotions must carry the same extension as the daily they link to,
        # or an encrypted file ends up named .tar.gz and nothing can open it.
        ext = suffix() + (".enc" if stats.encrypted else "")
        for tier in ("weekly", "monthly", "yearly"):
            dest = BACKUP_ROOT / tier / f"{FILE_PREFIX}{tags[tier]}{ext}"
            # Held already if any file covers this period, regardless of
            # extension — otherwise a settings change promotes a duplicate.
            if existing_for_tag(dest.parent, tags[tier]):
                continue
            duplicate(daily_dest, dest)
            log(f"Promoted to {tier}/{dest.name}")

        for tier, keep in KEEP.items():
            prune(tier, keep)

        if KEEP_LATEST_MIRROR and ENCRYPT:
            # An encrypted archive beside a plaintext mirror of the same content
            # is not encrypted. Drop the mirror instead of pretending.
            latest = BACKUP_ROOT / "latest"
            if latest.exists():
                shutil.rmtree(latest, ignore_errors=True)
            log("Skipped latest/ mirror: encryption is enabled")
        elif KEEP_LATEST_MIRROR:
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
            f"{stats.fetched} fetched, "
            f"{('redacted ' + str(stats.redacted_values) + ' values, ') if REDACT else ''}"
            f"{'encrypted, ' if stats.encrypted else ''}"
            f"{len(stats.warnings)} warning(s), "
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
                    "redacted_files": stats.redacted_files,
                    "redacted_values": stats.redacted_values,
                    "encrypted": stats.encrypted,
                    "redacted": REDACT,
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


def apply_cli_options(argv: list[str]) -> None:
    """Options come from the dashboard card via shell_command flags."""
    global REDACT, WRITE_SIDECAR, ENCRYPT, ENCRYPT_SIDECAR, PASSPHRASE_FILE
    if "--redact" in argv:
        REDACT = True
    if "--no-sidecar" in argv:
        WRITE_SIDECAR = False
    if "--encrypt" in argv:
        ENCRYPT = True
    if "--encrypt-sidecar" in argv:
        ENCRYPT_SIDECAR = True
    if "--passphrase-file" in argv:
        PASSPHRASE_FILE = Path(argv[argv.index("--passphrase-file") + 1])


if __name__ == "__main__":
    apply_cli_options(sys.argv)
    if "--cleanup-report" in sys.argv:
        print(json.dumps(cleanup_candidates()))
        raise SystemExit(0)
    if "--cleanup" in sys.argv:
        print(json.dumps(run_cleanup()))
        raise SystemExit(0)
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
