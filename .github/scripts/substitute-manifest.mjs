#!/usr/bin/env node
/**
 * Point module.json at the versioned manifest and download URLs before a release
 * is packaged.
 *
 * Replaces microsoft/variable-substitution@v1, which still declares node12. Only
 * four fields are touched; the rest of the manifest, its key order and its
 * indentation are left exactly as they are.
 *
 * Usage:
 *   node .github/scripts/substitute-manifest.mjs <tag> [file] [owner/repo]
 *
 *   tag        release tag, also used as the module version, for example 1.0.1
 *   file       manifest to rewrite, default module.json
 *   owner/repo defaults to $GITHUB_REPOSITORY
 *
 * Exits non-zero if the rewrite cannot be verified, so a release can never ship
 * a manifest with stale URLs.
 */

import fs from 'node:fs';
import process from 'node:process';

/** Return the manifest with the four versioned fields replaced. */
export function substitute(manifest, { tag, repository }) {
  return {
    ...manifest,
    version: tag,
    url: `https://github.com/${repository}`,
    manifest: `https://github.com/${repository}/releases/latest/download/module.json`,
    download: `https://github.com/${repository}/releases/download/${tag}/module.zip`,
  };
}

/** Guess the indentation used by a JSON file, defaulting to two spaces. */
function detectIndent(text) {
  const match = /^[{\[]\r?\n([ \t]+)"/m.exec(text);
  return match ? match[1] : '  ';
}

function fail(message, code) {
  console.error(`error: ${message}`);
  process.exitCode = code;
}

function main() {
  const [tag, file = 'module.json', repositoryArg] = process.argv.slice(2);
  const repository = repositoryArg || process.env.GITHUB_REPOSITORY;

  if (!tag) {
    fail('a release tag is required, for example 1.0.1', 2);
    return;
  }
  if (!repository) {
    fail('a repository is required: pass owner/repo or set GITHUB_REPOSITORY', 2);
    return;
  }

  let text;
  try {
    text = fs.readFileSync(file, 'utf8');
  } catch (error) {
    fail(`cannot read ${file}: ${error.message}`, 1);
    return;
  }

  let manifest;
  try {
    manifest = JSON.parse(text);
  } catch (error) {
    fail(`${file} is not valid JSON: ${error.message}`, 1);
    return;
  }
  if (!manifest || typeof manifest !== 'object' || Array.isArray(manifest)) {
    fail(`${file} does not contain a JSON object`, 1);
    return;
  }

  const updated = substitute(manifest, { tag, repository });
  const before = Object.keys(manifest).length;
  const after = Object.keys(updated).length;
  if (after !== before) {
    fail(`the rewrite changed the number of keys (${before} became ${after})`, 1);
    return;
  }

  fs.writeFileSync(file, `${JSON.stringify(updated, null, detectIndent(text))}\n`, 'utf8');

  // Read it back: a release must never go out with stale URLs.
  let written;
  try {
    written = JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch (error) {
    fail(`${file} is not valid JSON after writing: ${error.message}`, 1);
    return;
  }
  for (const [key, expected] of Object.entries(substitute({}, { tag, repository }))) {
    if (written[key] !== expected) {
      fail(`${file}: ${key} is ${JSON.stringify(written[key])}, expected ${JSON.stringify(expected)}`, 1);
      return;
    }
  }

  console.log(`Rewrote ${file} for tag ${tag}:`);
  console.log(`  version:  ${written.version}`);
  console.log(`  url:      ${written.url}`);
  console.log(`  manifest: ${written.manifest}`);
  console.log(`  download: ${written.download}`);
}

const invokedDirectly =
  process.argv[1] && import.meta.url.endsWith(process.argv[1].replace(/\\/g, '/'));

if (invokedDirectly) main();
