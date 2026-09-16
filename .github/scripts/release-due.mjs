#!/usr/bin/env node
/**
 * Decide whether a release is due.
 *
 * Prints "true" when the most recent release is at least --min-days old (default
 * 7), or when nothing has been released yet. Prints "false" when the last
 * release is more recent, so translations are merged without publishing.
 *
 * Progress lines go to stderr so stdout carries nothing but the answer, which
 * the workflow reads into an output.
 *
 * A release is the only way users receive updated translations, so an
 * unreadable release date counts as due: shipping a day early is better than
 * holding translations back for ever. A failure to read the release list at all
 * is different: nothing is printed and the exit code is non-zero, so the run is
 * marked failed instead of publishing a release whenever the API hiccups.
 *
 * Set FORCE_RELEASE=true to answer "true" regardless, which is what a manual run
 * with that box ticked does.
 *
 * Usage: node .github/scripts/release-due.mjs [--min-days 7]
 */

import { execFileSync } from 'node:child_process';
import process from 'node:process';

export const DEFAULT_MIN_DAYS = 7;
const DAY_MS = 24 * 60 * 60 * 1000;

/** True when a release may be published now. */
export function isReleaseDue({ publishedAt, now = new Date(), minDays = DEFAULT_MIN_DAYS } = {}) {
  if (!publishedAt) return true;
  const published = new Date(publishedAt);
  if (Number.isNaN(published.getTime())) return true;
  return now.getTime() - published.getTime() >= minDays * DAY_MS;
}

/**
 * The most recent published release as { tagName, publishedAt }, or null when
 * there is none. Throws when the release list cannot be read.
 */
export function latestRelease() {
  const output = execFileSync(
    'gh',
    ['release', 'list', '--limit', '1', '--exclude-drafts', '--json', 'tagName,publishedAt'],
    { encoding: 'utf8' },
  ).trim();
  const releases = output ? JSON.parse(output) : [];
  return releases[0] ?? null;
}

function main() {
  const args = process.argv.slice(2);
  const index = args.indexOf('--min-days');
  const minDays = index >= 0 ? Number(args[index + 1]) : DEFAULT_MIN_DAYS;
  if (!Number.isFinite(minDays) || minDays < 0) {
    console.error(`error: --min-days expects a number, got '${args[index + 1]}'`);
    process.exitCode = 2;
    return;
  }

  if (process.env.FORCE_RELEASE === 'true') {
    console.error('releasing on request');
    console.log('true');
    return;
  }

  let latest;
  try {
    latest = latestRelease();
  } catch (error) {
    console.error(`error: could not read the release list: ${error.message}`);
    process.exitCode = 1;
    return;
  }

  if (!latest) {
    console.error('nothing released yet');
    console.log('true');
    return;
  }

  const ageDays = (Date.now() - new Date(latest.publishedAt).getTime()) / DAY_MS;
  console.error(`latest release ${latest.tagName} is ${ageDays.toFixed(1)} days old, minimum ${minDays}`);
  console.log(isReleaseDue({ publishedAt: latest.publishedAt, minDays }) ? 'true' : 'false');
}

const invokedDirectly =
  process.argv[1] && import.meta.url.endsWith(process.argv[1].replace(/\\/g, '/'));

if (invokedDirectly) main();
