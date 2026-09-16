#!/usr/bin/env node
/**
 * Print the tag to use for the next release.
 *
 * Bumps the last numeric part of the most recent release tag, so 1.0 becomes
 * 1.1 and 1.2.3 becomes 1.2.4. If there is no usable tag, or the latest one is
 * not numeric, it falls back to today's date as YYYY.MM.DD, which is also a
 * valid module version.
 *
 * Used by .github/workflows/cut-release.yml, where there is nobody around to
 * type a version.
 */

import { execFileSync } from 'node:child_process';
import process from 'node:process';

export function nextTag(latest, today = new Date()) {
  const trimmed = String(latest ?? '').trim();
  if (/^\d+(\.\d+)*$/.test(trimmed)) {
    const parts = trimmed.split('.');
    parts[parts.length - 1] = String(Number(parts[parts.length - 1]) + 1);
    return parts.join('.');
  }
  return today.toISOString().slice(0, 10).replace(/-/g, '.');
}

export function latestReleaseTag() {
  try {
    return execFileSync(
      'gh',
      ['release', 'list', '--limit', '1', '--exclude-drafts', '--json', 'tagName',
        '--jq', '.[0].tagName // empty'],
      { encoding: 'utf8' },
    ).trim();
  } catch {
    return '';
  }
}

const invokedDirectly =
  process.argv[1] && import.meta.url.endsWith(process.argv[1].replace(/\\/g, '/'));

if (invokedDirectly) {
  process.stdout.write(`${nextTag(latestReleaseTag())}\n`);
}
