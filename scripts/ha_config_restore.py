#!/usr/bin/env python3
"""
ha_config_restore.py
====================

Selective restore companion to ha_config_backup.py. Browses generations,
maintains a queue of files to restore, and puts them back — always saving
what it overwrites first.

Lives beside ha_config_backup.py in the Samba share and is driven by the
"Restore" dashboard card through shell_command.

Modes
-----
  --browse --generation X --group G --page N [--only-changed]
              [--sort name|size] [--desc] [--search TEXT]
        Writes _restore_state.json for the command_line sensor: one page of
        candidate files plus the current queue. Every file is compared against
        the live copy, so the UI can hide or grey out ones where restoring
        would change nothing.

  --queue-add --slots 1,3,4      Add those slots from the current page
  --queue-path "<path>"          Add by typed/pasted path, wildcards allowed
  --queue-remove-all             Empty the queue
  --queue-list                   Print the queue as JSON

  --restore-queue [--allow-storage]
        Restore everything queued. Every file it overwrites is copied to
        _restore_rollback/<timestamp>/ first.

  --generations                  List available generations as JSON

Safety
------
* Nothing is restored without an explicit --restore-queue.
* Files under .storage need --allow-storage as well; restoring registries or
  auth data can lock you out of Home Assistant.
* Overwritten files are always preserved under _restore_rollback/.
* Only files the export took verbatim can be restored: config/ and storage/.
  The yaml/ tree is a converted *view* of the JSON, so restoring it would put
  YAML where Home Assistant expects JSON. It is deliberately not offered.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import shutil
import sys
import tarfile
from datetime import datetime
from pathlib import Path

BACKUP_ROOT = Path(__file__).resolve().parent
STATE_FILE = BACKUP_ROOT / "_restore_state.json"
DIFF_CACHE = BACKUP_ROOT / "_restore_diff_cache.json"
QUEUE_FILE = BACKUP_ROOT / "_restore_queue.json"
ROLLBACK_ROOT = BACKUP_ROOT / "_restore_rollback"
LOG_FILE = BACKUP_ROOT / "logs" / "restore.log"

CONFIG_CANDIDATES = ["/config", "/homeassistant", "/usr/share/hassio/homeassistant"]
TIERS = ("daily", "weekly", "monthly", "yearly")
PAGE_SIZE = 12
# Hashing a whole generation takes a second or two; reuse the result while the
# user is paging around, but not so long that edits go unnoticed.
DIFF_CACHE_SECONDS = 120

# Restoring these can lock you out or confuse the instance. Flagged in the UI.
RISKY_PREFIXES = ("auth", "onboarding", "core.uuid", "cloud", "http",
                  "core.config_entries", "core.device_registry", "core.entity_registry")

GROUPS = {
    "All files": lambda p: True,
    "Dashboards": lambda p: p.startswith("storage/lovelace"),
    "Top-level YAML": lambda p: p.startswith("config/") and "/" not in p[7:],
    "Packages": lambda p: p.startswith("config/packages/"),
    "ESPHome": lambda p: p.startswith("config/esphome/"),
    "Helpers & registries": lambda p: p.startswith("storage/") and not p.startswith("storage/lovelace"),
    "Everything in .storage": lambda p: p.startswith("storage/"),
}


def log(message: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {message}"
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def resolve_config_dir() -> Path:
    override = os.environ.get("HA_CONFIG_DIR")
    if override:
        return Path(override)
    for candidate in CONFIG_CANDIDATES:
        if (Path(candidate) / "configuration.yaml").is_file():
            return Path(candidate)
    raise RuntimeError("Could not locate the Home Assistant configuration directory.")


# --------------------------------------------------------------------------
# Generations
# --------------------------------------------------------------------------


def list_generations() -> list[dict]:
    out = []
    latest = BACKUP_ROOT / "latest"
    if latest.is_dir():
        stamp = datetime.fromtimestamp(latest.stat().st_mtime)
        out.append({"id": "latest", "label": f"Latest · {stamp:%b %d %H:%M}"})

    for tier in TIERS:
        tier_dir = BACKUP_ROOT / tier
        if not tier_dir.is_dir():
            continue
        for path in sorted(tier_dir.glob("ha-config-*.tar.gz"), reverse=True):
            tag = path.name.replace("ha-config-", "").replace(".tar.gz", "")
            out.append({"id": f"{tier}/{path.name}", "label": f"{tier.title()} · {tag}"})
    return out


class Generation:
    """Uniform read access to a generation, archive or folder."""

    def __init__(self, ident: str):
        self.ident = ident
        self.tar: tarfile.TarFile | None = None
        self.root: Path | None = None
        self._prefix = ""

        if ident == "latest":
            self.root = BACKUP_ROOT / "latest"
            if not self.root.is_dir():
                raise RuntimeError("The latest/ mirror does not exist.")
        else:
            path = BACKUP_ROOT / ident
            if not path.is_file():
                raise RuntimeError(f"Generation not found: {ident}")
            self.tar = tarfile.open(path, "r:gz")
            # The archive wraps everything in a single top-level directory. Its
            # first member may be that directory itself, with no trailing
            # slash, so take the first path component rather than looking for
            # a "/" in the name.
            first = next((m.name for m in self.tar.getmembers() if m.name), "")
            root = first.split("/")[0]
            self._prefix = f"{root}/" if root else ""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self.tar:
            self.tar.close()

    def members(self) -> list[str]:
        """Restorable paths, relative to the snapshot root."""
        found = []
        if self.tar:
            for member in self.tar.getmembers():
                if not member.isfile():
                    continue
                name = member.name[len(self._prefix):] if self._prefix else member.name
                if name.startswith(("config/", "storage/")):
                    found.append(name)
        else:
            for sub in ("config", "storage"):
                base = self.root / sub
                if not base.is_dir():
                    continue
                for path in base.rglob("*"):
                    if path.is_file():
                        found.append(f"{sub}/{path.relative_to(base).as_posix()}")
        return sorted(found)

    def sizes(self) -> dict[str, int]:
        """Byte size of every restorable file, without extracting anything."""
        out: dict[str, int] = {}
        if self.tar:
            for member in self.tar.getmembers():
                if not member.isfile():
                    continue
                name = member.name[len(self._prefix):] if self._prefix else member.name
                if name.startswith(("config/", "storage/")):
                    out[name] = member.size
        else:
            for sub in ("config", "storage"):
                base = self.root / sub
                if not base.is_dir():
                    continue
                for path in base.rglob("*"):
                    if path.is_file():
                        try:
                            out[f"{sub}/{path.relative_to(base).as_posix()}"] = path.stat().st_size
                        except OSError:
                            pass
        return out

    def read(self, name: str) -> bytes:
        if self.tar:
            handle = self.tar.extractfile(self._prefix + name)
            if handle is None:
                raise RuntimeError(f"Not in archive: {name}")
            return handle.read()
        return (self.root / name).read_bytes()


def target_for(name: str, config_dir: Path) -> Path:
    """Map a snapshot path back to where it belongs in the config directory."""
    if name.startswith("config/"):
        return config_dir / name[len("config/"):]
    if name.startswith("storage/"):
        return config_dir / ".storage" / name[len("storage/"):]
    raise RuntimeError(f"Refusing to restore outside config/ or storage/: {name}")


def human_size(num: int) -> str:
    if num < 1024:
        return f"{num} B"
    if num < 1024 * 1024:
        return f"{num / 1024:.1f} kB"
    return f"{num / 1048576:.1f} MB"


def is_risky(name: str) -> bool:
    return name.startswith("storage/") and name[len("storage/"):].startswith(RISKY_PREFIXES)


# --------------------------------------------------------------------------
# Comparison against what is live right now
# --------------------------------------------------------------------------


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def compare_generation(gen: "Generation", names: list[str], config_dir: Path) -> dict[str, str]:
    """Status per file: changed, identical, or new.

    Compares the archived bytes against the live file directly. There is no
    need to run an export first — the live file IS the thing being compared,
    and hashing it is instant.
    """
    status: dict[str, str] = {}
    for name in names:
        try:
            target = target_for(name, config_dir)
        except Exception:
            continue
        try:
            archived = gen.read(name)
        except Exception:
            status[name] = "missing"
            continue
        if not target.exists():
            status[name] = "new"
            continue
        try:
            live = target.read_bytes()
        except Exception:
            status[name] = "unreadable"
            continue
        status[name] = "identical" if sha256_bytes(live) == sha256_bytes(archived) else "changed"
    return status


def get_status_map(gen: "Generation", ident: str, names: list[str], config_dir: Path) -> dict[str, str]:
    """Cached comparison, keyed on the generation and refreshed periodically."""
    now = datetime.now().timestamp()
    if DIFF_CACHE.is_file():
        try:
            cached = json.loads(DIFF_CACHE.read_text(encoding="utf-8"))
            fresh = now - cached.get("computed", 0) < DIFF_CACHE_SECONDS
            if fresh and cached.get("generation") == ident:
                have = cached.get("status", {})
                if all(n in have for n in names):
                    return have
        except Exception:
            pass

    status = compare_generation(gen, gen.members(), config_dir)
    try:
        DIFF_CACHE.write_text(json.dumps(
            {"generation": ident, "computed": now, "status": status}), encoding="utf-8")
    except Exception:
        pass
    return status


# --------------------------------------------------------------------------
# Queue
# --------------------------------------------------------------------------


def read_queue() -> list[dict]:
    if QUEUE_FILE.is_file():
        try:
            return json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def write_queue(items: list[dict]) -> None:
    QUEUE_FILE.write_text(json.dumps(items, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------
# Browse
# --------------------------------------------------------------------------


def browse(generation: str, group: str, page: int, only_changed: bool = True,
           sort: str = "name", desc: bool = False, search: str = "") -> dict:
    generations = list_generations()
    if not generations:
        state = {"error": "No generations found. Has the export run?",
                 "files": [], "total": 0, "page": 1, "pages": 1,
                 "queue_count": 0, "queue": []}
        STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
        return state

    ident = generation
    if ident not in [g["id"] for g in generations]:
        # Label was passed instead of id, or the generation has aged out.
        match = next((g for g in generations if g["label"] == generation), None)
        ident = match["id"] if match else generations[0]["id"]

    matches = GROUPS.get(group, GROUPS["All files"])

    with Generation(ident) as gen:
        names = [n for n in gen.members() if matches(n)]
        config_dir = resolve_config_dir()
        status = get_status_map(gen, ident, names, config_dir)
        size_map = gen.sizes()

    counts = {"changed": 0, "identical": 0, "new": 0, "other": 0}
    for name in names:
        state = status.get(name, "other")
        counts[state if state in counts else "other"] += 1

    in_group = len(names)

    # Search narrows within the chosen file type. Plain text is a
    # case-insensitive substring match on the whole path; * and ? switch it to
    # pattern matching, so "core.*registry" and "*.yaml" both work.
    needle = (search or "").strip()
    if needle and needle.lower() not in ("unknown", "unavailable", "none"):
        low = needle.lower()
        if any(ch in low for ch in "*?"):
            # Search, not path matching: * spans directories here and the match
            # is a substring one, so "core.*registry" finds
            # storage/core.entity_registry.
            expr = "".join(
                ".*" if ch == "*" else "." if ch == "?" else re.escape(ch) for ch in low
            )
            rx = re.compile(expr)
            names = [n for n in names if rx.search(n.lower())]
        else:
            names = [n for n in names if low in n.lower()]
    matched = len(names)

    if only_changed:
        names = [n for n in names if status.get(n) in ("changed", "new")]

    # Sorting happens before paging, so page 1 really is the largest or the
    # first alphabetically across the whole filtered set, not just this page.
    if sort == "size":
        names.sort(key=lambda n: (size_map.get(n, 0), n.lower()), reverse=desc)
    else:
        names.sort(key=lambda n: n.lower(), reverse=desc)

    total = len(names)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(page, pages))
    chunk = names[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]

    queue = read_queue()
    queued = {(item["generation"], item["name"]) for item in queue}

    files = []
    for offset, name in enumerate(chunk, start=1):
        short = name.split("/", 1)[1]
        files.append({
            "slot": offset,
            "name": name,
            "label": (short[:44] + "…") if len(short) > 45 else short,
            "risky": is_risky(name),
            "queued": (ident, name) in queued,
            "status": status.get(name, "other"),
            "size": human_size(size_map.get(name, 0)),
            "bytes": size_map.get(name, 0),
        })

    state = {
        "generation": ident,
        "generation_label": next((g["label"] for g in generations if g["id"] == ident), ident),
        "group": group,
        "page": page,
        "pages": pages,
        "total": total,
        "in_group": in_group,
        "matched": matched,
        "search": needle,
        "only_changed": only_changed,
        "sort": sort,
        "sort_desc": desc,
        "changed_count": counts["changed"],
        "identical_count": counts["identical"],
        "new_count": counts["new"],
        "files": files,
        "queue_count": len(queue),
        "queue": [item["name"].split("/", 1)[1] for item in queue[:25]],
        "queue_generations": sorted({item["generation"] for item in queue}),
        # Carried here so one sensor can feed both the file list and the
        # generation dropdown's options.
        "generations": [g["label"] for g in generations],
        "updated": datetime.now().isoformat(timespec="seconds"),
    }
    STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    return state


def queue_add(slots: str) -> dict:
    if not STATE_FILE.is_file():
        return {"error": "Nothing browsed yet."}
    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    wanted = {int(s) for s in slots.replace(" ", "").split(",") if s.isdigit()}

    queue = read_queue()
    existing = {(item["generation"], item["name"]) for item in queue}
    added = 0
    identical = 0
    for entry in state.get("files", []):
        if entry["slot"] in wanted:
            if entry.get("status") == "identical":
                identical += 1
                continue
            key = (state["generation"], entry["name"])
            if key not in existing:
                queue.append({"generation": state["generation"], "name": entry["name"]})
                existing.add(key)
                added += 1
    write_queue(queue)
    log(f"queue +{added} (now {len(queue)}), {identical} identical ignored")
    return {"added": added, "identical_ignored": identical, "queue_count": len(queue)}


def normalise_path(raw: str) -> str:
    """Turn whatever the user pasted into a snapshot-relative path.

    Accepts, among others:
        packages/mail.yaml
        /config/packages/mail.yaml
        config/packages/mail.yaml
        .storage/lovelace
        storage/lovelace
        Z:\\ha_config_backup\\latest\\config\\packages\\mail.yaml
        \\\\homeassistant\\share\\ha_config_backup\\latest\\config\\packages\\mail.yaml
    Wildcards (* and ?) are preserved for matching.
    """
    text = raw.strip().strip('"').strip("'").replace("\\", "/")
    while "//" in text:
        text = text.replace("//", "/")

    # Anything pasted from Explorer or a terminal carries a prefix; keep only
    # the part from the first config/ or storage/ segment onwards.
    lowered = text.lower()
    for marker in ("/config/", "/.storage/", "/storage/"):
        idx = lowered.find(marker)
        if idx != -1:
            text = text[idx + 1:]
            break
    else:
        for marker in ("config/", ".storage/", "storage/"):
            if lowered.startswith(marker):
                break
        else:
            # A bare name or a path relative to the config directory
            text = "config/" + text.lstrip("/")

    if text.startswith(".storage/"):
        text = "storage/" + text[len(".storage/"):]
    if text.startswith("/"):
        text = text[1:]
    return text


def pattern_to_regex(pattern: str) -> "re.Pattern[str]":
    """Glob matching where * stops at a directory boundary.

    fnmatch lets * cross "/", so config/*.yaml would reach into
    config/packages/. Here * and ? stay within one segment and ** spans them,
    which is what people expect from a path pattern.
    """
    out = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "*":
            if pattern[i:i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(ch))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def queue_path(raw: str, generation: str) -> dict:
    generations = list_generations()
    if not generations:
        return {"error": "No generations found."}

    ident = generation
    ids = [g["id"] for g in generations]
    if ident not in ids:
        match = next((g for g in generations if g["label"] == generation), None)
        ident = match["id"] if match else generations[0]["id"]

    wanted = normalise_path(raw)
    if not wanted or wanted in ("config/", "storage/"):
        return {"error": "Nothing entered.", "added": 0}

    with Generation(ident) as gen:
        members = gen.members()

    rx = pattern_to_regex(wanted)
    matches = [m for m in members if rx.match(m)]
    if not matches and not any(ch in wanted for ch in "*?"):
        # Case-insensitive exact retry, then fall back to suggestions.
        low = wanted.lower()
        matches = [m for m in members if m.lower() == low]

    if not matches:
        # Substring first, then fuzzy — catches both "mail" and "configuraton".
        needle = wanted.rsplit("/", 1)[-1].lower().strip("*?")
        near = [m for m in members if needle and needle in m.rsplit("/", 1)[-1].lower()]
        if not near and needle:
            names = {m.rsplit("/", 1)[-1]: m for m in members}
            close = difflib.get_close_matches(needle, [n.lower() for n in names], n=8, cutoff=0.6)
            near = [names[n] for n in names if n.lower() in close]
        return {
            "added": 0,
            "input": raw,
            "resolved": wanted,
            "error": f"No file matching '{wanted}' in this generation.",
            "suggestions": near[:8],
        }

    with Generation(ident) as gen:
        status = get_status_map(gen, ident, matches, resolve_config_dir())
    identical = [m for m in matches if status.get(m) == "identical"]
    matches = [m for m in matches if status.get(m) != "identical"]

    if not matches:
        return {
            "added": 0,
            "matched": len(identical),
            "resolved": wanted,
            "identical_ignored": len(identical),
            "error": "Every match is already identical to the live file — restoring would change nothing.",
            "suggestions": [],
        }

    queue = read_queue()
    existing = {(item["generation"], item["name"]) for item in queue}
    added = 0
    for name in matches:
        if (ident, name) not in existing:
            queue.append({"generation": ident, "name": name})
            existing.add((ident, name))
            added += 1
    write_queue(queue)
    log(f"queue by path '{raw}' -> {added} added (now {len(queue)})")
    return {
        "added": added,
        "matched": len(matches),
        "resolved": wanted,
        "identical_ignored": len(identical),
        "files": matches[:20],
        "queue_count": len(queue),
    }


# --------------------------------------------------------------------------
# Restore
# --------------------------------------------------------------------------


def restore_queue(allow_storage: bool) -> dict:
    queue = read_queue()
    if not queue:
        return {"status": "empty", "restored": 0, "message": "Queue is empty."}

    config_dir = resolve_config_dir()
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    rollback = ROLLBACK_ROOT / stamp

    restored, skipped, errors = [], [], []
    needs_restart = False

    by_generation: dict[str, list[str]] = {}
    for item in queue:
        by_generation.setdefault(item["generation"], []).append(item["name"])

    for ident, names in by_generation.items():
        try:
            gen = Generation(ident)
        except Exception as err:
            errors.append(f"{ident}: {err}")
            continue

        with gen:
            for name in names:
                if name.startswith("storage/") and not allow_storage:
                    skipped.append(f"{name} (.storage restores not enabled)")
                    continue
                try:
                    data = gen.read(name)
                    target = target_for(name, config_dir)

                    if target.exists():
                        keep = rollback / name
                        keep.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(target, keep)

                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                    restored.append(name)
                    if name.startswith("storage/"):
                        needs_restart = True
                    log(f"restored {name} from {ident}")
                except Exception as err:
                    errors.append(f"{name}: {err}")

    if restored:
        write_queue([])

    result = {
        "status": "error" if errors else ("ok" if restored else "skipped"),
        "restored": len(restored),
        "restored_files": restored[:40],
        "skipped": skipped[:20],
        "errors": errors[:20],
        "rollback": str(rollback) if restored else None,
        "needs_restart": needs_restart,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }
    log(f"restore finished: {result['status']}, {len(restored)} file(s), "
        f"{len(skipped)} skipped, {len(errors)} error(s)")
    return result


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--generations", action="store_true")
    parser.add_argument("--browse", action="store_true")
    parser.add_argument("--generation", default="latest")
    parser.add_argument("--group", default="All files")
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--only-changed", action="store_true")
    parser.add_argument("--sort", default="name", choices=["name", "size"])
    parser.add_argument("--desc", action="store_true")
    parser.add_argument("--search", default="")
    parser.add_argument("--queue-add")
    parser.add_argument("--queue-path")
    parser.add_argument("--queue-remove-all", action="store_true")
    parser.add_argument("--queue-list", action="store_true")
    parser.add_argument("--restore-queue", action="store_true")
    parser.add_argument("--allow-storage", action="store_true")
    args = parser.parse_args()

    if args.generations:
        print(json.dumps(list_generations()))
    elif args.browse:
        print(json.dumps(browse(args.generation, args.group, args.page,
                                args.only_changed, args.sort, args.desc,
                                args.search)))
    elif args.queue_add:
        print(json.dumps(queue_add(args.queue_add)))
    elif args.queue_path:
        print(json.dumps(queue_path(args.queue_path, args.generation)))
    elif args.queue_remove_all:
        write_queue([])
        log("queue cleared")
        print(json.dumps({"queue_count": 0}))
    elif args.queue_list:
        print(json.dumps(read_queue()))
    elif args.restore_queue:
        print(json.dumps(restore_queue(args.allow_storage)))
    else:
        parser.print_help()
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as err:
        log(f"FATAL: {err}")
        print(json.dumps({"status": "error", "message": str(err)}))
        sys.exit(2)
