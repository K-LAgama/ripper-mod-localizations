#!/usr/bin/env node
/**
 * Merge the open translation pull requests that are safe to accept unreviewed.
 *
 * A pull request is merged only when
 *   - it is not a draft and has no merge conflicts,
 *   - every changed path is inside translations/ and nothing touches the
 *     English source (en.json),
 *   - every changed .json file parses, keeps the same top level keys as that
 *     module's en.json, contains at least one real string, and is not just a
 *     copy of the English source.
 *
 * Everything else is reported and left for a human to look at. Used by
 * .github/workflows/cut-release.yml.
 *
 * Usage:
 *   node .github/scripts/merge-translations.mjs --merge-method squash [--dry-run] [--admin]
 */

import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import process from 'node:process';

const TRANSLATION_ROOT = 'translations/';
const SOURCE_NAME = 'en.json';
const LABEL = 'translations';

// A file with at least this many source strings, of which this share is still
// English, is a copy of en.json rather than a translation. Partly translated
// files are welcome, so the share is deliberately high: only near copies of the
// English source are rejected.
const STUB_MIN_STRINGS = 20;
const STUB_SHARE = 0.95;

function gh(args) {
  return execFileSync('gh', args, { encoding: 'utf8' });
}

function ghJson(args) {
  const output = gh(args).trim();
  return output ? JSON.parse(output) : null;
}

/** Yield [dotted path, value] for every scalar leaf of a JSON document. */
function* leaves(node, prefix = '') {
  if (Array.isArray(node)) {
    for (let index = 0; index < node.length; index += 1) {
      yield* leaves(node[index], `${prefix}[${index}]`);
    }
    return;
  }
  if (node && typeof node === 'object') {
    for (const [key, value] of Object.entries(node)) {
      yield* leaves(value, prefix ? `${prefix}.${key}` : key);
    }
    return;
  }
  yield [prefix, node];
}

function stringLeaves(data) {
  const found = new Map();
  for (const [path, value] of leaves(data)) {
    if (typeof value === 'string' && value.trim() !== '') found.set(path, value);
  }
  return found;
}

/**
 * Check one changed translation file. Exported so the rules can be tested
 * without touching GitHub.
 *
 * Returns { ok: true } or { ok: false, reason }.
 */
export function checkTranslationFile({ path, content, source }) {
  let data;
  try {
    data = JSON.parse(content);
  } catch (error) {
    return { ok: false, reason: `${path}: not valid JSON (${error.message})` };
  }
  if (!data || typeof data !== 'object' || Array.isArray(data)) {
    return { ok: false, reason: `${path}: not a JSON object` };
  }

  let sourceData;
  try {
    sourceData = JSON.parse(source);
  } catch {
    return { ok: false, reason: `${path}: the matching ${SOURCE_NAME} could not be read` };
  }
  if (!sourceData || typeof sourceData !== 'object' || Array.isArray(sourceData)) {
    return { ok: false, reason: `${path}: the matching ${SOURCE_NAME} is not a JSON object` };
  }

  const keys = Object.keys(data).sort().join(',');
  const sourceKeys = Object.keys(sourceData).sort().join(',');
  if (keys !== sourceKeys) {
    return { ok: false, reason: `${path}: top level keys differ from ${SOURCE_NAME}` };
  }

  const translated = stringLeaves(data);
  if (translated.size === 0) {
    return { ok: false, reason: `${path}: contains no translated strings` };
  }

  const sourceStrings = stringLeaves(sourceData);
  if (sourceStrings.size === 0) return { ok: true };

  let identical = 0;
  for (const [leafPath, value] of translated) {
    if (sourceStrings.get(leafPath) === value) identical += 1;
  }

  if (identical === sourceStrings.size) {
    return { ok: false, reason: `${path}: every string is still English` };
  }

  const share = identical / sourceStrings.size;
  if (sourceStrings.size >= STUB_MIN_STRINGS && share >= STUB_SHARE) {
    return { ok: false, reason: `${path}: ${Math.round(share * 100)}% of it is still English` };
  }

  return { ok: true, identical, strings: translated.size };
}

function readFileAt(repo, path, ref) {
  return gh(['api', '-H', 'Accept: application/vnd.github.raw', `repos/${repo}/contents/${path}?ref=${ref}`]);
}

/** Report why a pull request cannot be merged automatically, or an empty list. */
function findProblems(repo, pull) {
  const problems = [];
  const files = pull.files ?? [];

  if (pull.isDraft) problems.push('it is a draft');
  if (pull.mergeable === 'CONFLICTING') problems.push('it has merge conflicts');
  if (!files.length) problems.push('it changes no files');
  if (typeof pull.changedFiles === 'number' && pull.changedFiles > files.length) {
    problems.push(`only ${files.length} of ${pull.changedFiles} changed files could be inspected`);
  }

  const paths = files.map((file) => file.path);
  const outside = paths.filter((path) => !path.startsWith(TRANSLATION_ROOT));
  if (outside.length) {
    problems.push(`it changes ${outside.slice(0, 3).join(', ')} outside ${TRANSLATION_ROOT}`);
  }
  const sources = paths.filter((path) => path.endsWith(`/${SOURCE_NAME}`));
  if (sources.length) problems.push(`it changes the English source ${sources.join(', ')}`);

  if (problems.length) return problems;

  for (const path of paths) {
    const match = /^translations\/([^/]+)\/.+\.json$/.exec(path);
    if (!match) continue; // CONTRIBUTORS.md and friends need no validation
    const folder = match[1];
    const check = checkTranslationFile({
      path,
      content: readFileAt(repo, path, pull.headRefOid),
      source: readFileAt(repo, `${TRANSLATION_ROOT}${folder}/${SOURCE_NAME}`, pull.headRefOid),
    });
    if (!check.ok) {
      problems.push(check.reason);
      break;
    }
  }

  return problems;
}

function parseArgs(argv) {
  const options = { dryRun: false, admin: false, mergeMethod: 'squash' };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === '--dry-run') options.dryRun = true;
    else if (arg === '--admin') options.admin = true;
    else if (arg === '--merge-method') {
      index += 1;
      options.mergeMethod = argv[index] ?? 'squash';
    }
  }
  if (!['squash', 'merge', 'rebase'].includes(options.mergeMethod)) {
    throw new Error(`unsupported merge method: ${options.mergeMethod}`);
  }
  return options;
}

function main() {
  const options = parseArgs(process.argv.slice(2));
  const repo = process.env.GITHUB_REPOSITORY;
  if (!repo) throw new Error('GITHUB_REPOSITORY is not set');

  const open = ghJson(['pr', 'list', '--state', 'open', '--limit', '100', '--json', 'number']) ?? [];
  if (!open.length) {
    console.log('No open pull requests.');
    return 0;
  }

  let merged = 0;
  let skipped = 0;
  let failed = 0;

  for (const { number } of open) {
    const pull = ghJson([
      'pr', 'view', String(number),
      '--json', 'number,title,isDraft,mergeable,changedFiles,files,headRefOid',
    ]);

    const problems = findProblems(repo, pull);
    if (problems.length) {
      skipped += 1;
      console.log(`skipped #${pull.number} ${pull.title}`);
      for (const problem of problems) console.log(`        ${problem}`);
      continue;
    }

    if (options.dryRun) {
      console.log(`would merge #${pull.number} ${pull.title}`);
      continue;
    }

    try {
      // Categorise the pull request for the generated release notes. Best effort:
      // a missing label permission must never block the merge.
      try {
        gh(['label', 'create', LABEL, '--color', 'fbca04',
          '--description', 'Translation pull requests', '--force']);
        gh(['pr', 'edit', String(pull.number), '--add-label', LABEL]);
      } catch {
        console.log(`        (could not label #${pull.number}, continuing)`);
      }

      const mergeArgs = ['pr', 'merge', String(pull.number), `--${options.mergeMethod}`];
      if (options.admin) mergeArgs.push('--admin');
      gh(mergeArgs);
      merged += 1;
      console.log(`merged  #${pull.number} ${pull.title} (${options.mergeMethod})`);
    } catch (error) {
      failed += 1;
      console.log(`FAILED  #${pull.number} ${pull.title}`);
      console.log(`        ${String(error.stderr ?? error.message).trim().split('\n').join('\n        ')}`);
    }
  }

  console.log(`\n${merged} merged, ${skipped} skipped, ${failed} failed`);
  if (options.dryRun) console.log('Dry run: nothing was merged.');
  setOutput('merged', merged);
  setOutput('skipped', skipped);
  setOutput('failed', failed);
  return 0;
}

/** Expose a value to the workflow, so it can skip the release when nothing merged. */
function setOutput(name, value) {
  const file = process.env.GITHUB_OUTPUT;
  if (!file) return;
  try {
    fs.appendFileSync(file, `${name}=${value}\n`);
  } catch (error) {
    console.log(`could not write ${name} to GITHUB_OUTPUT: ${error.message}`);
  }
}

const invokedDirectly = process.argv[1] && import.meta.url.endsWith(process.argv[1].replace(/\\/g, '/'));
if (invokedDirectly) {
  try {
    process.exitCode = main();
  } catch (error) {
    console.error(`error: ${error.message}`);
    process.exitCode = 1;
  }
}
