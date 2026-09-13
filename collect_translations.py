#!/usr/bin/env python3
"""Refresh the English source files of the modules collected in translations/.

For every folder inside the parent directory of this project that contains a
``module.json``, this copies the module's English language file
(``lang/en.json`` or ``languages/en.json``) to::

    translations/<module-id>/en.json

Only ``en.json`` is read or written. Translation files such as ``de.json`` or
``uk.json`` are left completely alone: they are written by contributors and by
``translate-modules.js``.

By default only modules that already have a folder in ``translations/`` are
processed, so a hand-picked selection is never re-expanded to every sibling
project. Pass ``--all-modules`` to add new ones.

Usage::

    python collect_translations.py                  # refresh changed sources
    python collect_translations.py --dry-run        # show what would change
    python collect_translations.py --dry-run -v     # also list unchanged modules
    python collect_translations.py --only levels    # a single module
    python collect_translations.py --all-modules    # also add new modules
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

# Folders that may hold the source language files, in order of preference.
LANG_DIR_NAMES = ("lang", "languages")
SOURCE_FILE_NAME = "en.json"
# Language codes in a module's manifest that count as "the English source".
ENGLISH_LANG_CODES = {"en", "en-us", "en-gb"}


class ModuleError(Exception):
    """Raised when a module folder cannot be processed."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Copy each sibling Foundry module's en.json into "
            "translations/<module id>/en.json when it changed."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python collect_translations.py --dry-run -v\n"
            "  python collect_translations.py --only levels --only token-z\n"
            "  python collect_translations.py --all-modules\n"
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
        help="destination folder holding one folder per module (default: %(default)s)",
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
        "--existing-only",
        dest="existing_only",
        action="store_true",
        default=True,
        help=argparse.SUPPRESS,  # the default, kept so older commands keep working
    )
    parser.add_argument(
        "--all-modules",
        dest="existing_only",
        action="store_false",
        help=(
            "also crawl modules that have no folder in translations/ yet and add "
            "them; by default only the collected modules are updated"
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
        help="only print the summary",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="also list the modules that are already up to date",
    )
    return parser.parse_args(argv)


def log(message: str, quiet: bool = False) -> None:
    if not quiet:
        print(message)


def number(value: int) -> str:
    return f"{value:,}"


def iter_package_dirs(parent_dir: Path, skip: set[Path]):
    """Yield the candidate module folders inside *parent_dir* (non-recursive)."""
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


def read_module_json(module_dir: Path) -> dict:
    path = module_dir / "module.json"
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ModuleError(f"unreadable module.json ({exc})") from exc
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


def read_json_file(path: Path):
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ModuleError(f"cannot read {path} ({exc})") from exc


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


def flatten_leaves(node, prefix: str = ""):
    """Yield ``(dotted path, value)`` for every leaf of a JSON document."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from flatten_leaves(value, f"{prefix}.{key}" if prefix else key)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from flatten_leaves(value, f"{prefix}[{index}]")
    else:
        yield prefix, node


def diff_sources(previous, current) -> dict:
    """Compare the collected English source with the refreshed one.

    ``characters`` counts the English text in keys that are new or whose text
    changed: the strings a translation pass would have to pay for.
    """
    old = dict(flatten_leaves(previous)) if isinstance(previous, dict) else {}
    new = dict(flatten_leaves(current)) if isinstance(current, dict) else {}

    added = [path for path in new if path not in old]
    removed = [path for path in old if path not in new]
    changed = [path for path in new if path in old and old[path] != new[path]]
    characters = sum(len(new[path]) for path in added + changed
                     if isinstance(new[path], str))

    return {
        "added": len(added),
        "changed": len(changed),
        "removed": len(removed),
        "characters": characters,
    }


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


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    parent_dir = args.parent.expanduser().resolve()
    translations_dir = args.translations.expanduser().resolve()
    only = {name.lower() for name in args.only}

    if not parent_dir.is_dir():
        print(f"error: parent folder not found: {parent_dir}", file=sys.stderr)
        return 2

    skip = set() if args.include_self else {PROJECT_DIR.resolve()}

    crawled = selected = copied = up_to_date = no_source = errors = 0
    skipped_missing = 0
    updated_sources = first_time = new_characters = 0
    shrinking: list[str] = []

    log(f"Crawling: {parent_dir}")
    log(f"Into:     {translations_dir}\n", quiet=args.quiet)

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

        destination = translations_dir / module_id / SOURCE_FILE_NAME

        # Only the modules already collected by this project are touched.
        if args.existing_only and not destination.parent.is_dir():
            skipped_missing += 1
            continue

        selected += 1
        try:
            source = find_source_file(module_dir, module_data)
        except OSError as exc:
            errors += 1
            print(f"[error]  {module_dir.name}: {exc}", file=sys.stderr)
            continue

        if source is None:
            no_source += 1
            log(f"[no en]  {module_dir.name}: no lang/en.json or languages/en.json "
                f"(wanted translations/{module_id}/{SOURCE_FILE_NAME})", quiet=args.quiet)
            continue

        previous = None
        if destination.is_file():
            try:
                loaded = read_json_file(destination)
                if isinstance(loaded, dict):
                    previous = loaded
            except ModuleError:
                previous = None

        try:
            written = copy_source(source, destination, args.dry_run)
        except OSError as exc:
            errors += 1
            print(f"[error]  {module_dir.name}: copy failed ({exc})", file=sys.stderr)
            continue

        if not written:
            up_to_date += 1
            log(f"[same]   {module_dir.name}: en.json is up to date", quiet=args.quiet)
            continue

        copied += 1

        # Describe what actually changed, so the cost of a translation pass is known.
        detail = None
        source_data = None
        try:
            source_data = read_json_file(source)
        except ModuleError:
            source_data = None

        if previous is None:
            first_time += 1
            detail = "en.json collected for the first time"
        elif source_data is not None:
            diff = diff_sources(previous, source_data)
            if diff["added"] or diff["changed"] or diff["removed"]:
                updated_sources += 1
                new_characters += diff["characters"]
                if diff["removed"]:
                    shrinking.append(f"{module_dir.name} (-{diff['removed']})")
                detail = (
                    f"en.json updated: +{diff['added']} new, ~{diff['changed']} changed, "
                    f"-{diff['removed']} removed, {number(diff['characters'])} chars to translate"
                )
            else:
                detail = "en.json updated (formatting only)"
        else:
            detail = "en.json updated"

        log(f"[ok]     {module_dir.name}: {detail}", quiet=args.quiet)

    print("\n" + "-" * 60)
    prefix = "Dry run: " if args.dry_run else ""
    print(
        f"{prefix}{selected} of {crawled} module folder(s) selected | {copied} copied | "
        f"{up_to_date} up to date | {no_source} without an en.json | {errors} error(s)"
    )
    if skipped_missing:
        print(f"{prefix}{skipped_missing} module(s) not in translations/ were left alone "
              f"(--all-modules to add them)")
    if updated_sources or first_time:
        print(f"{prefix}{updated_sources} English source(s) changed "
              f"({number(new_characters)} new/changed characters to translate)")
    if first_time:
        print(f"{prefix}{first_time} English source(s) collected for the first time")
    if updated_sources or first_time:
        print(f"{prefix}next: node translate-modules.js --dry-run")
    if shrinking:
        shown = ", ".join(shrinking[:8])
        more = "" if len(shrinking) <= 8 else f" (+{len(shrinking) - 8} more)"
        print(f"! {len(shrinking)} source(s) lost strings, check for an outdated checkout: "
              f"{shown}{more}")
    if args.dry_run:
        print("Nothing was written (--dry-run).")

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
