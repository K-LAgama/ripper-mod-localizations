#!/usr/bin/env python3
"""Collect each sibling Foundry VTT package's translations into this project.

The script crawls every folder inside the parent directory of this project. For
each folder that contains a ``module.json`` it reads the ``"id"`` property and:

1. copies the module's English source (``lang/en.json`` or ``languages/en.json``)
   to ``translations/<id>/en.json``;
2. harvests the module's **existing human translations** for every language this
   project declares in its own ``module.json``, writing them to
   ``translations/<id>/<lang>.json`` (``de``, ``pt-br``, ``zh-hans``, ...).

Harvesting is worth doing before any machine translation: upstream translations
are free and better than DeepL. Values that are identical to the English source
(untranslated stubs) and empty strings are dropped, so those gaps are left for
``translate-modules.js`` to fill.

Existing content in a destination file is never overwritten unless
``--prefer-upstream`` is given, so harvested files and DeepL output can both be
re-run safely.

The source modules are only read, never modified.

Usage::

    python collect_translations.py                          # refresh the modules already collected
    python collect_translations.py --dry-run -v             # preview, listing every harvested file
    python collect_translations.py --only levels --only token-z
    python collect_translations.py --all-modules            # also add folders for new modules
    python collect_translations.py --no-harvest             # sources only

Only folders that already exist in ``translations/`` are touched by default, so
the hand-picked module selection is preserved. Pass ``--all-modules`` to crawl
and add new ones.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR
DEFAULT_PARENT_DIR = PROJECT_DIR.parent
DEFAULT_TRANSLATIONS_DIR = PROJECT_DIR / "translations"
DEFAULT_MANIFEST = PROJECT_DIR / "module.json"

# Folders that may hold the source language files, in order of preference.
LANG_DIR_NAMES = ("lang", "languages")
SOURCE_FILE_NAME = "en.json"
# Language codes in module.json that count as "the English source".
ENGLISH_LANG_CODES = {"en", "en-us", "en-gb"}
DEST_FILE_NAME = "en.json"

# Upstream file names that hold the same language under a different code, so a
# module shipping ``cn.json`` or ``zh_Hans.json`` still feeds ``zh-Hans``.
LANG_ALIASES = {
    "cn": {"cn", "zh-hans"},
    "zh-hans": {"zh-hans", "cn"},
    "zh-tw": {"zh-tw", "zh-hant"},
    "zh-hant": {"zh-hant", "zh-tw"},
    "pt": {"pt", "pt-pt"},
    "pt-pt": {"pt-pt", "pt"},
}


class ModuleError(Exception):
    """Raised when a module folder cannot be processed."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Crawl the sibling folders of this project, copy each Foundry module's "
            "en.json into translations/<module id>/ and harvest its existing "
            "translations for the languages this project declares."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python collect_translations.py --existing-only --dry-run -v\n"
            "  python collect_translations.py --only levels --only token-z\n"
            "  python collect_translations.py --prefer-upstream --lang de\n"
        ),
    )
    parser.add_argument(
        "--parent",
        type=Path,
        default=DEFAULT_PARENT_DIR,
        help="folder whose sub-folders are crawled (default: %(default)s)",
    )
    parser.add_argument(
        "--translations",
        type=Path,
        default=DEFAULT_TRANSLATIONS_DIR,
        help="destination folder for the collected files (default: %(default)s)",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help="module.json declaring the languages to harvest (default: %(default)s)",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="NAME",
        help=(
            "only process the given module folder name (repeatable); "
            "matching is case-insensitive"
        ),
    )
    parser.add_argument(
        "--lang",
        action="append",
        default=[],
        metavar="CODE",
        help="only harvest this language code (repeatable, case-insensitive)",
    )
    parser.add_argument(
        "--existing-only",
        dest="existing_only",
        action="store_true",
        default=True,
        help=argparse.SUPPRESS,  # this is the default, kept so old commands keep working
    )
    parser.add_argument(
        "--all-modules",
        dest="existing_only",
        action="store_false",
        help=(
            "crawl every sibling project and add folders for new modules too; by "
            "default only the modules already present in translations/ are touched, "
            "so a hand-picked selection is never re-expanded"
        ),
    )
    parser.add_argument(
        "--no-harvest",
        action="store_true",
        help="only copy the English sources, do not harvest existing translations",
    )
    parser.add_argument(
        "--prefer-upstream",
        action="store_true",
        help=(
            "let upstream values replace translations already in translations/ "
            "(default: only fill keys that have no translation yet)"
        ),
    )
    parser.add_argument(
        "--include-self",
        action="store_true",
        help="also process this project's own folder (skipped by default)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be copied without writing anything",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="only print the final summary",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="list every harvested language file",
    )
    return parser.parse_args(argv)


def log(message: str, quiet: bool = False) -> None:
    if not quiet:
        print(message)


def number(value: int) -> str:
    return f"{value:,}"


def normalise_code(code: str) -> str:
    """Lowercase a language code and treat ``pt_BR`` and ``pt-br`` as equal."""
    return code.strip().lower().replace("_", "-")


def codes_for(code: str) -> set[str]:
    key = normalise_code(code)
    return {normalise_code(alias) for alias in LANG_ALIASES.get(key, {key})}


def load_target_languages(manifest_path: Path) -> list[str]:
    """Return the language codes declared by this project (minus the source)."""
    try:
        with manifest_path.open("r", encoding="utf-8-sig") as handle:
            manifest = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ModuleError(f"cannot read {manifest_path} ({exc})") from exc

    codes: list[str] = []
    seen: set[str] = set()
    for entry in manifest.get("languages") or []:
        if not isinstance(entry, dict):
            continue
        code = str(entry.get("lang", "")).strip()
        if not code:
            continue
        key = normalise_code(code)
        if key in ENGLISH_LANG_CODES or key in seen:
            continue
        seen.add(key)
        codes.append(code)
    return codes


def iter_package_dirs(parent_dir: Path, skip: set[Path]):
    """Yield the candidate package folders inside *parent_dir* (non-recursive)."""
    for entry in sorted(parent_dir.iterdir(), key=lambda p: p.name.lower()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        try:
            resolved = entry.resolve()
        except OSError:
            resolved = entry
        if resolved in skip:
            continue
        yield entry


def read_json_file(path: Path):
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ModuleError(f"cannot read {path} ({exc})") from exc


def read_module_json(module_dir: Path) -> dict:
    data = read_json_file(module_dir / "module.json")
    if not isinstance(data, dict):
        raise ModuleError("module.json does not contain a JSON object")
    return data


def extract_module_id(data: dict) -> str:
    module_id = data.get("id")
    if not isinstance(module_id, str) or not module_id.strip():
        raise ModuleError('module.json has no usable "id" property')
    module_id = module_id.strip()
    if module_id in {".", ".."} or "/" in module_id or "\\" in module_id:
        raise ModuleError(f'unsafe "id" value: {module_id!r}')
    return module_id


def _en_file_in(directory: Path) -> Path | None:
    """Return the ``en.json`` inside *directory*, ignoring file-name casing."""
    if not directory.is_dir():
        return None
    for candidate in sorted(directory.iterdir(), key=lambda p: p.name.lower()):
        if candidate.is_file() and candidate.name.lower() == SOURCE_FILE_NAME:
            return candidate
    return None


def find_source_file(module_dir: Path, module_data: dict) -> Path | None:
    """Locate a module's English source file.

    Preference order: the path declared in ``module.json`` for an English
    language entry, then ``lang/en.json``, then ``languages/en.json``, then a
    case-insensitive match on those folder names.
    """
    module_root = module_dir.resolve()
    languages = module_data.get("languages")
    if isinstance(languages, list):
        for entry in languages:
            if not isinstance(entry, dict):
                continue
            code = str(entry.get("lang", "")).strip().lower()
            rel_path = entry.get("path")
            if code not in ENGLISH_LANG_CODES or not isinstance(rel_path, str):
                continue
            if not rel_path.strip():
                continue
            declared = (module_dir / rel_path.strip()).resolve()
            try:
                declared.relative_to(module_root)
            except ValueError:
                continue  # declared path escapes the module folder
            if declared.is_file():
                return declared

    for dir_name in LANG_DIR_NAMES:
        found = _en_file_in(module_dir / dir_name)
        if found is not None:
            return found

    for entry in sorted(module_dir.iterdir(), key=lambda p: p.name.lower()):
        if entry.is_dir() and entry.name.lower() in LANG_DIR_NAMES:
            found = _en_file_in(entry)
            if found is not None:
                return found

    return None


def language_dirs(module_dir: Path) -> list[Path]:
    """The lang/ and languages/ folders of a module, however they are cased."""
    found: list[Path] = []
    for dir_name in LANG_DIR_NAMES:
        candidate = module_dir / dir_name
        if candidate.is_dir():
            found.append(candidate)
    for entry in sorted(module_dir.iterdir(), key=lambda p: p.name.lower()):
        if entry.is_dir() and entry.name.lower() in LANG_DIR_NAMES and entry not in found:
            found.append(entry)
    return found


def find_upstream_file(module_dir: Path, code: str) -> Path | None:
    """Find a module's existing translation for *code*, honouring code aliases."""
    wanted = codes_for(code)
    for directory in language_dirs(module_dir):
        for candidate in sorted(directory.iterdir(), key=lambda p: p.name.lower()):
            if candidate.is_file() and normalise_code(candidate.stem) in wanted:
                return candidate
    return None


def harvest_subset(source, target, stats: dict):
    """Return the part of *target* that really translates *source*.

    Values identical to the English source (untranslated stubs), empty strings
    and branches without any translation are dropped, so those keys stay pending
    for ``translate-modules.js``.
    """
    if isinstance(source, str):
        if source.strip() == "":
            return None
        if not isinstance(target, str) or target.strip() == "":
            return None
        if target == source:
            stats["stubs"] += 1
            return None
        stats["keys"] += 1
        stats["characters"] += len(source)
        return target

    if isinstance(source, dict):
        if not isinstance(target, dict):
            return None
        out = {}
        for key, value in source.items():
            if key not in target:
                continue
            sub = harvest_subset(value, target[key], stats)
            if sub is not None:
                out[key] = sub
        return out or None

    if isinstance(source, list):
        if not isinstance(target, list):
            return None
        items = []
        for index, value in enumerate(source):
            if index >= len(target):
                return None  # partially translated list, keep it for the translator
            items.append(harvest_subset(value, target[index], stats))
        if any(item is None for item in items):
            return None
        return items or None

    return None  # numbers, booleans and nulls are not translatable


def merge_harvest(destination: dict, harvested: dict, prefer_upstream: bool) -> int:
    """Merge *harvested* into *destination*; returns how many values changed."""
    changed = 0
    for key, value in harvested.items():
        if isinstance(value, dict):
            current = destination.get(key)
            if not isinstance(current, dict):
                current = {}
                destination[key] = current
            changed += merge_harvest(current, value, prefer_upstream)
            continue
        current = destination.get(key)
        if prefer_upstream or not (isinstance(current, str) and current.strip()):
            if current != value:
                destination[key] = value
                changed += 1
    return changed


def serialise(data: dict) -> str:
    return json.dumps(data, indent=4, ensure_ascii=False, sort_keys=True) + "\n"


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def copy_source(source: Path, destination: Path, dry_run: bool) -> bool:
    """Copy *source* to *destination*.

    Returns ``True`` when the file was written, ``False`` when the destination
    already contained identical content.
    """
    try:
        if destination.is_file() and destination.read_bytes() == source.read_bytes():
            return False
    except OSError:
        pass  # fall through and copy anyway

    if dry_run:
        return True

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return True


def harvest_module(module_dir: Path, module_id: str, translations_dir: Path, source_data,
                   languages: list[str], args) -> dict:
    """Harvest every declared language of one module. Returns per-language stats."""
    harvested: dict[str, dict] = {}
    for code in languages:
        upstream = find_upstream_file(module_dir, code)
        if upstream is None:
            continue

        stats = {"keys": 0, "characters": 0, "stubs": 0}
        try:
            target_data = read_json_file(upstream)
        except ModuleError:
            # Upstream placeholder files are sometimes empty or broken; that just
            # means there is nothing to harvest for this language.
            harvested[code] = {"unreadable": True, "upstream": upstream}
            continue
        if not isinstance(target_data, dict):
            harvested[code] = {"unreadable": True, "upstream": upstream}
            continue

        subset = harvest_subset(source_data, target_data, stats)
        if not subset:
            harvested[code] = {"empty": True, "stubs": stats["stubs"], "upstream": upstream}
            continue

        destination = translations_dir / module_id / f"{code.lower()}.json"
        existing: dict = {}
        if destination.is_file():
            try:
                loaded = read_json_file(destination)
                if isinstance(loaded, dict):
                    existing = loaded
            except ModuleError:
                existing = {}

        changed = merge_harvest(existing, subset, args.prefer_upstream)
        written = changed > 0 or not destination.is_file()
        if written and not args.dry_run:
            write_text(destination, serialise(existing))

        harvested[code] = {
            "upstream": upstream,
            "destination": destination,
            "keys": stats["keys"],
            "characters": stats["characters"],
            "stubs": stats["stubs"],
            "changed": changed,
            "written": written,
        }
    return harvested


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    parent_dir = args.parent.expanduser().resolve()
    translations_dir = args.translations.expanduser().resolve()
    manifest_path = args.manifest.expanduser().resolve()
    only = {name.lower() for name in args.only}
    wanted_langs = {normalise_code(code) for code in args.lang}

    if not parent_dir.is_dir():
        print(f"error: parent folder not found: {parent_dir}", file=sys.stderr)
        return 2

    languages: list[str] = []
    if not args.no_harvest:
        try:
            languages = load_target_languages(manifest_path)
        except ModuleError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if wanted_langs:
            declared = {normalise_code(code) for code in languages}
            missing = sorted(wanted_langs - declared)
            if missing:
                print(
                    f"error: {', '.join(missing)} is not declared in {manifest_path.name}",
                    file=sys.stderr,
                )
                return 2
            languages = [code for code in languages if normalise_code(code) in wanted_langs]

    skip = set() if args.include_self else {PROJECT_DIR.resolve()}

    copied = up_to_date = no_source = errors = processed = crawled = 0
    skipped_existing = 0
    harvest_files = 0
    unreadable: list[str] = []
    per_language: dict[str, dict] = {code: {"modules": 0, "files": 0, "keys": 0,
                                            "characters": 0, "stubs": 0} for code in languages}

    log(f"Crawling: {parent_dir}")
    log(f"Into:     {translations_dir}")
    if languages:
        log(f"Harvest:  {len(languages)} language(s) declared in {manifest_path.name}\n")
    else:
        log("Harvest:  disabled\n")

    for module_dir in iter_package_dirs(parent_dir, skip):
        if only and module_dir.name.lower() not in only:
            continue
        if not (module_dir / "module.json").is_file():
            continue
        crawled += 1

        try:
            module_data = read_module_json(module_dir)
            module_id = extract_module_id(module_data)
        except (ModuleError, OSError) as exc:
            errors += 1
            print(f"[error]  {module_dir.name}: {exc}", file=sys.stderr)
            continue

        destination = translations_dir / module_id / DEST_FILE_NAME

        # Only touch the modules that are already part of the project, so a
        # hand-picked selection is never re-expanded to every sibling project.
        if args.existing_only and not destination.parent.is_dir():
            skipped_existing += 1
            continue

        processed += 1
        try:
            source = find_source_file(module_dir, module_data)
        except OSError as exc:
            errors += 1
            print(f"[error]  {module_dir.name}: {exc}", file=sys.stderr)
            continue

        if source is None:
            no_source += 1
            log(f"[no en]  {module_dir.name}: no lang/en.json or languages/en.json "
                f"(wanted translations/{module_id}/{DEST_FILE_NAME})")
            continue

        try:
            written = copy_source(source, destination, args.dry_run)
        except OSError as exc:
            errors += 1
            print(f"[error]  {module_dir.name}: copy failed ({exc})", file=sys.stderr)
            continue

        if written:
            copied += 1
        else:
            up_to_date += 1

        harvested = {}
        if languages:
            try:
                source_data = read_json_file(source)
            except ModuleError as exc:
                errors += 1
                print(f"[error]  {module_dir.name}: {exc}", file=sys.stderr)
                continue
            harvested = harvest_module(module_dir, module_id, translations_dir, source_data,
                                       languages, args)
            for code, info in harvested.items():
                if info.get("unreadable"):
                    unreadable.append(f"{module_dir.name}/{code}")
                    continue
                entry = per_language[code]
                entry["modules"] += 1
                entry["stubs"] += info.get("stubs", 0)
                if info.get("written"):
                    entry["files"] += 1
                    entry["keys"] += info.get("keys", 0)
                    entry["characters"] += info.get("characters", 0)
                    harvest_files += 1
                    if args.verbose:
                        log(f"         + {code} <- {info['upstream'].relative_to(module_dir)} "
                            f"({number(info['keys'])} keys)")

        parts = []
        if written:
            parts.append("en.json copied")
        else:
            parts.append("en.json up to date")
        if harvested:
            kept = [code for code, info in harvested.items() if info.get("written")]
            if kept:
                parts.append(f"harvested {len(kept)} language file(s)")
            else:
                parts.append("nothing new to harvest")
        log(f"[ok]     {module_dir.name}: {' | '.join(parts)}")

    # ---- summary ----------------------------------------------------------
    if languages:
        active = [code for code in languages if per_language[code]["modules"]]
        if active:
            width = max(8, *(len(code) for code in active))
            print(f"\n{'language'.ljust(width)}  {'modules'.rjust(7)}  {'files'.rjust(6)}  "
                  f"{'keys'.rjust(7)}  {'chars saved'.rjust(11)}  {'stubs'.rjust(6)}")
            total_keys = total_chars = total_files = total_stubs = 0
            for code in active:
                entry = per_language[code]
                total_keys += entry["keys"]
                total_chars += entry["characters"]
                total_files += entry["files"]
                total_stubs += entry["stubs"]
                print(f"{code.ljust(width)}  {number(entry['modules']).rjust(7)}  "
                      f"{number(entry['files']).rjust(6)}  {number(entry['keys']).rjust(7)}  "
                      f"{number(entry['characters']).rjust(11)}  {number(entry['stubs']).rjust(6)}")
            print(f"{'total'.ljust(width)}  {'':>7}  {number(total_files).rjust(6)}  "
                  f"{number(total_keys).rjust(7)}  {number(total_chars).rjust(11)}  "
                  f"{number(total_stubs).rjust(6)}")
            print("(chars saved = English characters that no longer need translating)")

    print("\n" + "-" * 60)
    prefix = "Dry run: " if args.dry_run else ""
    print(
        f"{prefix}{processed} of {crawled} module folder(s) selected | "
        f"{copied} en.json copied | {up_to_date} up to date | "
        f"{no_source} without an en.json | {errors} error(s)"
    )
    if skipped_existing:
        print(f"{prefix}{skipped_existing} module(s) not in translations/ were left alone "
              f"(--all-modules to add them)")
    if languages:
        print(f"{prefix}{harvest_files} harvested language file(s) written")
    if unreadable:
        shown = ", ".join(unreadable[:6])
        more = "" if len(unreadable) <= 6 else f" (+{len(unreadable) - 6} more)"
        print(f"{prefix}{len(unreadable)} upstream language file(s) were empty or "
              f"unreadable, nothing to harvest there: {shown}{more}")
    if args.dry_run:
        print("Nothing was written (--dry-run).")

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
