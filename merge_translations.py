#!/usr/bin/env python3

from __future__ import annotations

import argparse
import copy
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_TRANSLATIONS_DIR = SCRIPT_DIR / "translations"
DEFAULT_LANGUAGES_DIR = SCRIPT_DIR / "languages"
DEFAULT_MANIFEST = SCRIPT_DIR / "module.json"

INDENT = 4
MAX_CONFLICTS_SHOWN = 25


class MergeError(Exception):
    """Raised when a file cannot be read or does not hold a JSON object."""


@dataclass
class LanguageResult:
    """Outcome of merging one language."""

    lang: str
    base_path: Path
    keys: int = 0
    modules: list[str] = field(default_factory=list)
    conflicts: list[tuple[str, list[tuple[str, object]]]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    changed: bool = False


class Contributions:
    """Tracks which module supplied which value for every leaf path.

    Used to report the paths where two or more translation files disagree, which
    is stable across re-runs because the aggregate file's own content is not a
    contribution.
    """

    def __init__(self) -> None:
        self._by_path: dict[str, list[tuple[str, object]]] = {}

    def record(self, path: str, origin: str, value) -> None:
        entries = self._by_path.setdefault(path, [])
        entries[:] = [(name, val) for name, val in entries if name != origin]
        entries.append((origin, value))

    def conflicts(self) -> list[tuple[str, list[tuple[str, object]]]]:
        found = []
        for path, entries in self._by_path.items():
            if len(entries) < 2:
                continue
            if len({serialise_value(value) for _, value in entries}) > 1:
                found.append((path, entries))
        return sorted(found, key=lambda item: item[0])


def serialise_value(value) -> str:
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return repr(value)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Merge translations/<module-id>/<lang>.json into languages/<lang>.json.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python merge_translations.py\n"
            "  python merge_translations.py --lang ca --lang de\n"
            "  python merge_translations.py --dry-run -v\n"
        ),
    )
    parser.add_argument(
        "--translations",
        type=Path,
        default=DEFAULT_TRANSLATIONS_DIR,
        help="folder holding the per-module translation folders (default: %(default)s)",
    )
    parser.add_argument(
        "--languages",
        type=Path,
        default=DEFAULT_LANGUAGES_DIR,
        help="folder holding the aggregate language files (default: %(default)s)",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help="module.json used for the declared-language checks (default: %(default)s)",
    )
    parser.add_argument(
        "--lang",
        action="append",
        default=[],
        metavar="CODE",
        help="only merge this language code (repeatable, case-insensitive)",
    )
    parser.add_argument(
        "--create-missing",
        action="store_true",
        help=(
            "also create languages/<lang>.json for languages that only appear in "
            "translations/ and are not declared in module.json yet"
        ),
    )
    parser.add_argument(
        "--source-lang",
        action="append",
        default=None,
        metavar="CODE",
        help=(
            "language treated as the translation source: it is expected to live in "
            "translations/ only, so it is never created by --create-missing and is "
            "not reported as undeclared (default: en; repeatable, pass an empty "
            "value to disable)"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be merged without writing any file",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="only print notices, conflicts and the final summary",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="list the modules that contribute to each language",
    )
    return parser.parse_args(argv)


def log(message: str, quiet: bool = False) -> None:
    if not quiet:
        print(message)


def load_json_object(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise MergeError(f"{path}: {exc}") from exc
    if not isinstance(data, dict):
        raise MergeError(f"{path}: expected a JSON object at the top level")
    return data


def iter_leaf_items(node, prefix: str):
    """Yield ``(dotted path, value)`` for every leaf (non-dict) value in *node*."""
    if isinstance(node, dict) and node:
        for key, value in node.items():
            yield from iter_leaf_items(value, f"{prefix}.{key}")
    else:
        yield prefix, node


def merge_into(
    base: dict,
    overlay: dict,
    origin: str,
    prefix: str = "",
    tracker: Contributions | None = None,
) -> None:
    """Deep-merge *overlay* into *base*, deep-copying every value from *overlay*.

    Overlay values always win, so a later module overrides an earlier one. When
    a *tracker* is given, every leaf the overlay contributes is recorded so that
    disagreements between modules can be reported.
    """
    for key, value in overlay.items():
        path = f"{prefix}.{key}" if prefix else key
        existing = base.get(key) if key in base else None

        if key in base and isinstance(existing, dict) and isinstance(value, dict):
            merge_into(existing, value, origin, path, tracker)
            continue

        if tracker is not None:
            for leaf_path, leaf_value in iter_leaf_items(value, path):
                tracker.record(leaf_path, origin, leaf_value)

        base[key] = copy.deepcopy(value)


def index_translation_files(translations_dir: Path) -> dict[str, list[tuple[str, Path]]]:
    """Map ``lang`` (lowercase) -> sorted ``[(module folder name, file path), ...]``."""
    index: dict[str, list[tuple[str, Path]]] = {}
    if not translations_dir.is_dir():
        return index

    module_dirs = sorted(
        (entry for entry in translations_dir.iterdir() if entry.is_dir()),
        key=lambda entry: entry.name.lower(),
    )
    for module_dir in module_dirs:
        for path in sorted(module_dir.glob("*.json"), key=lambda p: p.name.lower()):
            index.setdefault(path.stem.lower(), []).append((module_dir.name, path))
    return index


def count_leaves(node) -> int:
    if isinstance(node, dict):
        return sum(count_leaves(value) for value in node.values())
    return 1


def serialise(data: dict) -> str:
    return json.dumps(data, indent=INDENT, ensure_ascii=False, sort_keys=True) + "\n"


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def manifest_languages(manifest_path: Path) -> tuple[set[str], set[str], list[str]]:
    """Return ``(declared codes lowercase, declared file names lowercase, errors)``."""
    codes: set[str] = set()
    files: set[str] = set()
    errors: list[str] = []

    if not manifest_path.is_file():
        return codes, files, [f"{manifest_path} not found, skipping manifest checks"]

    try:
        manifest = load_json_object(manifest_path)
    except MergeError as exc:
        return codes, files, [str(exc)]

    for entry in manifest.get("languages") or []:
        if not isinstance(entry, dict):
            continue
        code = str(entry.get("lang", "")).strip()
        rel_path = str(entry.get("path", "")).strip()
        if code:
            codes.add(code.lower())
        if rel_path:
            files.add(Path(rel_path).name.lower())
    return codes, files, errors


def merge_language(
    lang: str, base_path: Path, entries: list[tuple[str, Path]], dry_run: bool
) -> LanguageResult:
    """Merge every translation file for *lang* into the aggregate file."""
    result = LanguageResult(lang=lang, base_path=base_path)

    base: dict = {}
    if base_path.is_file():
        try:
            base = load_json_object(base_path)
        except MergeError as exc:
            result.errors.append(str(exc))
            return result

    tracker = Contributions()
    for module_name, path in entries:
        try:
            overlay = load_json_object(path)
        except MergeError as exc:
            result.errors.append(str(exc))
            continue
        result.modules.append(module_name)
        merge_into(base, overlay, module_name, "", tracker)

    result.conflicts = tracker.conflicts()
    result.keys = count_leaves(base)
    text = serialise(base)

    try:
        result.changed = (
            not base_path.is_file() or base_path.read_text(encoding="utf-8") != text
        )
    except OSError as exc:
        result.errors.append(f"{base_path}: {exc}")
        return result

    if result.changed and not dry_run:
        try:
            write_text(base_path, text)
        except OSError as exc:
            result.errors.append(f"{base_path}: {exc}")

    return result


def collect_notices(
    targets: list[tuple[str, Path]],
    index: dict[str, list[tuple[str, Path]]],
    declared_codes: set[str],
    declared_files: set[str],
    source_langs: set[str],
) -> list[str]:
    notices: list[str] = []

    for _lang, base_path in targets:
        file_declared = not declared_files or base_path.name.lower() in declared_files
        code_declared = not declared_codes or base_path.stem.lower() in declared_codes
        if not file_declared:
            notices.append(
                f"languages/{base_path.name} is not declared in module.json, "
                f"so it will not be shipped"
            )
        elif not code_declared:
            notices.append(
                f"languages/{base_path.name} holds '{base_path.stem}', but module.json "
                f"declares a different language code for that file"
            )

    target_langs = {lang.lower() for lang, _ in targets}
    for lang in sorted(index):
        if lang in source_langs:
            continue
        if declared_codes and lang not in declared_codes:
            names = ", ".join(name for name, _ in index[lang][:5])
            extra = "" if len(index[lang]) <= 5 else f" (+{len(index[lang]) - 5} more)"
            notices.append(
                f"translations contain '{lang}' (from {names}{extra}) but module.json "
                f"declares no such language"
            )

    for code in sorted(declared_codes):
        if code not in target_langs:
            notices.append(
                f"module.json declares '{code}' but there is no languages/{code}.json "
                f"to merge into"
            )

    return notices


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    translations_dir = args.translations.expanduser().resolve()
    languages_dir = args.languages.expanduser().resolve()
    manifest_path = args.manifest.expanduser().resolve()
    only = {code.lower() for code in args.lang}
    source_langs = (
        {"en"} if args.source_lang is None else {c.strip().lower() for c in args.source_lang}
    )
    source_langs.discard("")

    if not translations_dir.is_dir():
        print(f"error: translations folder not found: {translations_dir}", file=sys.stderr)
        return 2
    if not languages_dir.is_dir():
        print(f"error: languages folder not found: {languages_dir}", file=sys.stderr)
        return 2

    index = index_translation_files(translations_dir)
    declared_codes, declared_files, errors = manifest_languages(manifest_path)

    language_files = sorted(
        (path for path in languages_dir.glob("*.json") if path.is_file()),
        key=lambda path: path.name.lower(),
    )
    all_targets: list[tuple[str, Path]] = [(path.stem, path) for path in language_files]
    known = {lang.lower() for lang, _ in all_targets}
    if args.create_missing:
        for lang in sorted(index):
            if lang not in known and lang not in source_langs:
                all_targets.append((lang, languages_dir / f"{lang}.json"))
                known.add(lang)

    # --lang narrows what is merged, but the manifest notices still describe the
    # whole languages/ folder, so they are computed from all_targets.
    targets = all_targets
    if only:
        unknown = sorted(code for code in only if code not in known)
        if unknown:
            print(
                f"error: no language file for {', '.join(unknown)} in {languages_dir}",
                file=sys.stderr,
            )
            return 2
        targets = [target for target in all_targets if target[0].lower() in only]

    log(f"Translations: {translations_dir}")
    log(f"Languages:    {languages_dir}\n", quiet=args.quiet)

    merged = written = unchanged = no_source = 0
    conflicts_by_lang: list[tuple[str, list[tuple[str, list[tuple[str, object]]]]]] = []

    for lang, base_path in targets:
        entries = index.get(lang.lower(), [])
        if not entries:
            no_source += 1
            log(
                f"[skip]   {lang}: no translations/*/{lang}.json found, "
                f"languages/{base_path.name} left unchanged",
                quiet=args.quiet,
            )
            continue

        result = merge_language(lang, base_path, entries, args.dry_run)
        errors.extend(result.errors)
        merged += 1
        if result.changed:
            written += 1
        else:
            unchanged += 1

        if args.verbose:
            for name in result.modules:
                log(f"         + {name}", quiet=args.quiet)

        if result.changed:
            verb = "would be written" if args.dry_run else "written"
        else:
            verb = "already up to date"
        log(
            f"[ok]     {lang}: {len(result.modules)} module file(s) merged, "
            f"{result.keys} keys, languages/{base_path.name} {verb}",
            quiet=args.quiet,
        )

        if result.conflicts:
            conflicts_by_lang.append((lang, result.conflicts))

    notices = collect_notices(all_targets, index, declared_codes, declared_files, source_langs)

    total_conflicts = sum(len(conflicts) for _, conflicts in conflicts_by_lang)
    if total_conflicts:
        print(f"\nModules disagreeing on the same key ({total_conflicts}), last one wins:")
        shown = 0
        for lang, conflicts in conflicts_by_lang:
            if shown >= MAX_CONFLICTS_SHOWN:
                break
            for path, entries in conflicts:
                if shown >= MAX_CONFLICTS_SHOWN:
                    print(f"  ... {total_conflicts - shown} more")
                    break
                origins = ", ".join(origin for origin, _ in entries)
                winner = entries[-1][0]
                print(f"  [{lang}] {path}: {origins} ({winner} wins)")
                shown += 1

    if notices:
        print("\nNotices:")
        for notice in notices:
            print(f"  - {notice}")

    if errors:
        print("\nErrors:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)

    print("\n" + "-" * 60)
    prefix = "Dry run: " if args.dry_run else ""
    verb = "to write" if args.dry_run else "written"
    print(
        f"{prefix}{len(targets)} language file(s) | {merged} merged "
        f"({written} {verb}, {unchanged} unchanged) | "
        f"{no_source} without translations | {total_conflicts} conflict(s) | "
        f"{len(errors)} error(s)"
    )
    if args.dry_run:
        print("Nothing was written (--dry-run).")

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
