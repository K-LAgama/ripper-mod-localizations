#!/usr/bin/env node
/**
 * translate-modules.js
 *
 * Creates and updates every language file inside this project's translations/
 * collection, using DeepL and each module's English source as the reference:
 *
 *     translations/<module-id>/en.json   ->   translations/<module-id>/<lang>.json
 *
 * For every folder in translations/ the script walks its en.json and fills in
 * the target languages declared in this project's module.json. Existing
 * non-empty translations are reused, so re-running only translates what is new
 * or changed (use --force to retranslate everything).
 *
 * The DeepL key is read from ../SECRETS.json (same path as translate-all.js).
 *
 * Usage:
 *     node translate-modules.js --dry-run            # plan + quota estimate only
 *     node translate-modules.js                      # translate everything missing
 *     node translate-modules.js --module levels      # one module
 *     node translate-modules.js --lang ca --lang de  # some languages
 *     node translate-modules.js --max-characters 400000
 *     node translate-modules.js --force --lang ja    # retranslate Japanese
 */

import fs from 'fs/promises';
import path from 'path';
import { fileURLToPath, pathToFileURL } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const SECRETS_PATH = path.join(__dirname, '..', 'SECRETS.json');
const DEFAULT_MANIFEST = path.join(__dirname, 'module.json');
const DEFAULT_TRANSLATIONS_DIR = path.join(__dirname, 'translations');
const SOURCE_FILE = 'en.json';

const DEFAULT_CONCURRENCY = 3;
const DEFAULT_BATCH_SIZE = 50; // DeepL accepts at most 50 texts per request
const DEFAULT_RETRIES = 2;
const INDENT = 4;

// A file with at least this many strings, of which this share is identical to the
// English source, looks untranslated and is reported as such (see printPlan).
const ENGLISH_STUB_MIN_STRINGS = 20;
const ENGLISH_STUB_SHARE = 0.3;

/**
 * Foundry language code (lowercase) -> DeepL target language code.
 *
 * Only languages DeepL can actually produce are listed here; module.json must
 * not declare anything else. Check with:
 *     curl -H "Authorization: DeepL-Auth-Key $KEY" https://api-free.deepl.com/v2/languages?type=target
 */
export const DEEPL_TARGETS = {
  ja: 'JA',
  de: 'DE',
  fr: 'FR',
  es: 'ES',
  it: 'IT',
  pt: 'PT-PT',
  'pt-br': 'PT-BR',
  'zh-hans': 'ZH-HANS',
  'zh-tw': 'ZH-HANT',
  ca: 'CA',
  gl: 'GL',
  eu: 'EU',
  sv: 'SV',
  pl: 'PL',
  ru: 'RU',
  uk: 'UK',
};

class UsageError extends Error {}

// ---------------------------------------------------------------------------
// small helpers
// ---------------------------------------------------------------------------

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const number = (value) => value.toLocaleString('en-US');

function log(message, { quiet = false } = {}) {
  if (!quiet) console.log(message);
}

function warn(message) {
  console.warn(message);
}

/** Parse a JSON file, tolerating a UTF-8 BOM. */
export async function readJson(file) {
  const raw = await fs.readFile(file, 'utf-8');
  return JSON.parse(raw.replace(/^\uFEFF/, ''));
}

/** Read a JSON object, returning null (and the error) when it cannot be used. */
export async function readJsonObject(file) {
  try {
    const data = await readJson(file);
    if (data && typeof data === 'object' && !Array.isArray(data)) return { data, error: null };
    return { data: null, error: 'not a JSON object' };
  } catch (error) {
    if (error.code === 'ENOENT') return { data: null, error: null, missing: true };
    return { data: null, error: error.message };
  }
}

export function serialize(data) {
  return `${JSON.stringify(data, null, INDENT)}\n`;
}

export function chunk(items, size) {
  const chunks = [];
  for (let i = 0; i < items.length; i += size) chunks.push(items.slice(i, i + size));
  return chunks;
}

/** Run `worker` over `items` with at most `limit` in flight. */
export async function runPool(items, limit, worker) {
  const results = new Array(items.length);
  let next = 0;
  const runners = Array.from({ length: Math.max(1, Math.min(limit, items.length)) }, async () => {
    for (;;) {
      const index = next;
      next += 1;
      if (index >= items.length) return;
      results[index] = await worker(items[index], index);
    }
  });
  await Promise.all(runners);
  return results;
}

/**
 * Order `{ task, plan }` entries cheapest-first.
 *
 * Used when --max-characters is set: a partial budget then covers as many
 * modules as possible, instead of being spent on the few largest files.
 */
export function orderByCost(entries) {
  return [...entries].sort((a, b) => (a.plan?.characters ?? 0) - (b.plan?.characters ?? 0));
}

// ---------------------------------------------------------------------------
// planning: compare a source file with its translation
// ---------------------------------------------------------------------------

/**
 * Walk the English source and build the translated document skeleton.
 *
 * A non-empty value in the target file is a translation and is reused, even when
 * it is identical to the English source: plenty of strings stay the same across
 * languages (module names, abbreviations, "OK"). Only keys with no value yet are
 * queued in `pending`. Numbers, booleans, nulls and empty strings are copied
 * as-is.
 */
export function buildPlan(source, target, { force = false } = {}) {
  const pending = [];
  const stats = { reused: 0, strings: 0, identical: 0 };
  const output = planNode(source, target, [], pending, stats, force);
  return {
    output,
    pending,
    reused: stats.reused,
    strings: stats.strings,
    identical: stats.identical,
  };
}

function planNode(sourceNode, targetNode, keyPath, pending, stats, force) {
  if (typeof sourceNode === 'string') {
    if (sourceNode.trim() === '') return sourceNode;

    stats.strings += 1;
    if (typeof targetNode === 'string' && targetNode === sourceNode) stats.identical += 1;

    if (!force && typeof targetNode === 'string' && targetNode.trim() !== '') {
      stats.reused += 1;
      return targetNode;
    }

    pending.push({ path: keyPath, text: sourceNode });
    return sourceNode; // placeholder, replaced by applyTranslations()
  }

  if (Array.isArray(sourceNode)) {
    const targetArray = Array.isArray(targetNode) ? targetNode : [];
    return sourceNode.map((item, index) =>
      planNode(item, targetArray[index], [...keyPath, String(index)], pending, stats, force),
    );
  }

  if (sourceNode && typeof sourceNode === 'object') {
    const targetObject =
      targetNode && typeof targetNode === 'object' && !Array.isArray(targetNode) ? targetNode : {};
    const output = {};
    for (const [key, value] of Object.entries(sourceNode)) {
      output[key] = planNode(value, targetObject[key], [...keyPath, key], pending, stats, force);
    }
    return output;
  }

  return sourceNode;
}

function readPath(node, keyPath) {
  let current = node;
  for (const key of keyPath) {
    if (!current || typeof current !== 'object') return undefined;
    current = current[key];
  }
  return current;
}

function removePath(node, keyPath) {
  const parent = keyPath.length > 1 ? readPath(node, keyPath.slice(0, -1)) : node;
  if (parent && typeof parent === 'object') delete parent[keyPath[keyPath.length - 1]];
}

/**
 * Write translated strings into the skeleton produced by buildPlan().
 *
 * A string DeepL did not answer for is left out of the file entirely, or kept as
 * whatever it was before, so a failed request can never be mistaken for a
 * translation and the key stays queued for the next run.
 */
export function applyTranslations(output, pending, translations, previous = null) {
  pending.forEach((item, index) => {
    const value = translations[index];
    if (typeof value === 'string' && value !== '') {
      let node = output;
      for (let i = 0; i < item.path.length - 1; i += 1) node = node[item.path[i]];
      node[item.path[item.path.length - 1]] = value;
      return;
    }

    const old = readPath(previous, item.path);
    if (typeof old === 'string' && old.trim() !== '') {
      let node = output;
      for (let i = 0; i < item.path.length - 1; i += 1) node = node[item.path[i]];
      node[item.path[item.path.length - 1]] = old;
      return;
    }

    removePath(output, item.path);
  });
  return output;
}

// ---------------------------------------------------------------------------
// command line
// ---------------------------------------------------------------------------

function parsePositiveInt(value, flag) {
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed <= 0) {
    throw new UsageError(`${flag} expects a positive integer, got '${value}'`);
  }
  return parsed;
}

export function parseArgs(argv) {
  const options = {
    translations: DEFAULT_TRANSLATIONS_DIR,
    manifest: DEFAULT_MANIFEST,
    langs: [],
    modules: [],
    sourceLang: 'EN',
    dryRun: false,
    force: false,
    maxCharacters: null,
    concurrency: DEFAULT_CONCURRENCY,
    batchSize: DEFAULT_BATCH_SIZE,
    quiet: false,
    verbose: false,
    help: false,
  };

  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    const next = () => {
      i += 1;
      if (i >= argv.length) throw new UsageError(`missing value for ${arg}`);
      return argv[i];
    };

    switch (arg) {
      case '--translations': options.translations = next(); break;
      case '--manifest': options.manifest = next(); break;
      case '--lang': options.langs.push(next()); break;
      case '--module':
      case '--only': options.modules.push(next()); break;
      case '--source-lang': {
        const value = next();
        options.sourceLang = value.toLowerCase() === 'auto' ? null : value;
        break;
      }
      case '--max-characters': options.maxCharacters = parsePositiveInt(next(), arg); break;
      case '--concurrency': options.concurrency = parsePositiveInt(next(), arg); break;
      case '--batch-size': options.batchSize = Math.min(50, parsePositiveInt(next(), arg)); break;
      case '--dry-run': options.dryRun = true; break;
      case '--force': options.force = true; break;
      case '--quiet': options.quiet = true; break;
      case '-v':
      case '--verbose': options.verbose = true; break;
      case '-h':
      case '--help': options.help = true; break;
      default: throw new UsageError(`unknown argument: ${arg}`);
    }
  }

  return options;
}

const HELP = `Create and update the language files in translations/ using DeepL.

Usage: node translate-modules.js [options]

Options:
  --translations DIR    folder holding the per-module folders (default: ./translations)
  --manifest FILE       module.json that declares the target languages (default: ./module.json)
  --lang CODE           only this language, repeatable, case-insensitive (e.g. --lang pt-BR)
  --module NAME         only this translations folder, repeatable (alias: --only)
  --source-lang CODE    DeepL source language (default: EN, use 'auto' to auto-detect)
  --max-characters N    stop translating once N source characters have been sent
                        (cheapest files first, so the budget covers the most modules)
  --concurrency N       language files translated in parallel (default: ${DEFAULT_CONCURRENCY})
  --batch-size N        strings per DeepL request, max 50 (default: ${DEFAULT_BATCH_SIZE})
  --force               retranslate strings that already have a translation
  --dry-run             only report what would be translated; needs no API key
  --quiet               suppress the plan and progress output
  -v, --verbose         log every language file that is written
  -h, --help            show this help

The DeepL key is read from ../SECRETS.json (key: DEEPL_API).`;

// ---------------------------------------------------------------------------
// discovery
// ---------------------------------------------------------------------------

export function declaredLanguages(manifest) {
  const codes = [];
  for (const entry of manifest?.languages ?? []) {
    if (entry && typeof entry.lang === 'string' && entry.lang.trim()) codes.push(entry.lang.trim());
  }
  return codes;
}

/** Map declared Foundry codes to DeepL codes, honouring a --lang filter. */
export function resolveTargets(codes, filter = []) {
  const wanted = new Set(filter.map((code) => code.toLowerCase()));
  const targets = [];
  const unmapped = [];
  const unusedFilter = new Set(wanted);

  for (const code of codes) {
    const key = code.toLowerCase();
    if (wanted.size && !wanted.has(key)) continue;
    unusedFilter.delete(key);
    const deepl = DEEPL_TARGETS[key];
    if (!deepl) {
      unmapped.push(code);
      continue;
    }
    targets.push({ code, deepl, key });
  }
  return { targets, unmapped, unusedFilter: [...unusedFilter] };
}

export async function listModules(translationsDir, filter = []) {
  const entries = await fs.readdir(translationsDir, { withFileTypes: true });
  const wanted = new Set(filter.map((name) => name.toLowerCase()));
  return entries
    .filter((entry) => entry.isDirectory() && !entry.name.startsWith('.'))
    .map((entry) => entry.name)
    .filter((name) => !wanted.size || wanted.has(name.toLowerCase()))
    .sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase()));
}

// ---------------------------------------------------------------------------
// translation engine
// ---------------------------------------------------------------------------

/**
 * Translate every planned string of one language file.
 *
 * `send(texts, deeplCode)` must resolve to `[{ text, billed }]`; the CLI wires
 * it to DeepL, tests can inject a stub.
 */
export async function translateTask({ task, source, target, send, options, onProgress }) {
  const { pending, output, reused } = buildPlan(source, target.data, {
    force: options.force,
  });
  const result = {
    module: task.module,
    lang: task.code,
    deepl: task.deepl,
    file: task.file,
    exists: !target.missing,
    invalidTarget: Boolean(target.error),
    created: false,
    updated: false,
    unchanged: false,
    translated: 0,
    reused,
    unresolved: 0,
    planned: pending.length,
    characters: 0,
    errors: [],
  };

  const translations = new Array(pending.length);
  let sent = 0;

  for (const batch of chunk(pending, options.batchSize)) {
    try {
      const answers = await sendWithRetry(send, batch.map((item) => item.text), task.deepl, options);
      answers.forEach((answer, index) => {
        const slot = sent + index;
        translations[slot] = answer.text;
        result.characters += Number(answer.billed) || 0;
      });
      result.translated += answers.length;
    } catch (error) {
      result.errors.push(`[${task.module}/${task.code}] ${error.message}`);
      break; // the rest of the strings stay queued for the next run
    }
    sent += batch.length;
    if (onProgress) onProgress(batch.length);
  }

  result.unresolved = 0;
  for (let index = 0; index < translations.length; index += 1) {
    const value = translations[index];
    if (typeof value !== 'string' || value === '') result.unresolved += 1;
  }
  applyTranslations(output, pending, translations, target.data);

  const text = serialize(output);
  let previous = null;
  try {
    previous = await fs.readFile(task.file, 'utf-8');
  } catch (error) {
    if (error.code !== 'ENOENT') result.errors.push(`[${task.module}/${task.code}] ${error.message}`);
  }

  const needsWrite = previous === null || previous !== text;
  if (needsWrite) {
    await fs.mkdir(path.dirname(task.file), { recursive: true });
    await fs.writeFile(task.file, text, 'utf-8');
  }
  if (previous === null) result.created = true;
  else if (needsWrite) result.updated = true;
  else result.unchanged = true;

  if (result.created || result.updated) result.written = true;
  return result;
}

async function sendWithRetry(send, texts, deeplCode, options) {
  const retries = options.retries ?? DEFAULT_RETRIES;
  let lastError;
  for (let attempt = 0; attempt <= retries; attempt += 1) {
    try {
      return await send(texts, deeplCode);
    } catch (error) {
      lastError = error;
      if (attempt < retries) {
        const wait = 1000 * 2 ** attempt;
        warn(`  ! ${deeplCode} request failed (${error.message}), retrying in ${wait}ms`);
        await sleep(wait);
      }
    }
  }
  throw lastError;
}

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------

async function loadAuthKey() {
  try {
    const secrets = await readJson(SECRETS_PATH);
    return secrets.DEEPL_API ?? null;
  } catch (error) {
    throw new UsageError(`could not read ${SECRETS_PATH}: ${error.message}`);
  }
}

async function createDeepLClient(authKey) {
  let deepl;
  try {
    deepl = await import('deepl-node');
  } catch {
    throw new UsageError(
      "the 'deepl-node' package is not installed - run 'npm install deepl-node'",
    );
  }
  const Client = deepl.DeepLClient ?? deepl.Translator ?? deepl.default?.DeepLClient;
  if (!Client) throw new UsageError('deepl-node does not export DeepLClient/Translator');
  return new Client(authKey, { appInfo: { appName: 'ripper-mod-localizations', appVersion: '1.0' } });
}

function formatUsage(usage) {
  const character = usage?.character;
  if (!character) return null;
  const remaining = Math.max(0, character.limit - character.count);
  return `${number(character.count)} / ${number(character.limit)} characters used (${number(remaining)} remaining)`;
}

/** Drop DeepL codes this account/API does not support as a target. */
async function filterSupportedTargets(client, targets, logs) {
  let supported;
  try {
    const languages = await client.getTargetLanguages();
    supported = new Set(languages.map((language) => String(language.code).toUpperCase()));
  } catch (error) {
    logs.push(`could not load DeepL target languages (${error.message}), using all codes as-is`);
    return { targets, skipped: [] };
  }

  const kept = [];
  const skipped = [];
  for (const target of targets) {
    const code = target.deepl.toUpperCase();
    const ok = supported.has(code) || (code.startsWith('ZH-') && supported.has('ZH'));
    if (ok) kept.push(target);
    else skipped.push(target.code);
  }
  return { targets: kept, skipped };
}

async function planTasks({ tasks, sources, options }) {
  const plans = new Array(tasks.length);
  await runPool(tasks, options.concurrency, async (task, index) => {
    const source = sources.get(task.module);
    if (source.error || !source.data) {
      plans[index] = { error: `${task.module}: ${source.error ?? 'no source'}` };
      return;
    }
    const target = await readJsonObject(task.file);
    const { pending, strings, identical } = buildPlan(source.data, target.data, {
      force: options.force,
    });
    plans[index] = {
      pending: pending.length,
      characters: pending.reduce((total, item) => total + item.text.length, 0),
      strings,
      identical,
      missing: Boolean(target.missing),
      invalid: Boolean(target.error),
    };
  });
  return plans;
}

function printPlan({ tasks, plans, targets, options }) {
  const perLanguage = new Map();
  let totalCreate = 0;
  let totalUpdate = 0;
  let totalUnchanged = 0;
  let totalStrings = 0;
  let totalCharacters = 0;
  let badTargets = 0;

  tasks.forEach((task, index) => {
    const plan = plans[index];
    if (!plan || plan.error) return;
    if (plan.invalid) badTargets += 1;
    const entry = perLanguage.get(task.code) ?? {
      code: task.code,
      create: 0,
      update: 0,
      unchanged: 0,
      strings: 0,
      characters: 0,
    };
    if (plan.missing) {
      entry.create += 1;
      totalCreate += 1;
    } else if (plan.pending > 0) {
      entry.update += 1;
      totalUpdate += 1;
    } else {
      entry.unchanged += 1;
      totalUnchanged += 1;
    }
    entry.strings += plan.pending;
    entry.characters += plan.characters;
    totalStrings += plan.pending;
    totalCharacters += plan.characters;
    perLanguage.set(task.code, entry);
  });

  const rows = [...perLanguage.values()].sort((a, b) => a.code.localeCompare(b.code));
  const width = Math.max(8, ...rows.map((row) => row.code.length));
  const pad = (value, size) => String(value).padEnd(size);
  const num = (value, size) => number(value).padStart(size);

  log(`\n${pad('language', width)}  ${num('create', 7)}  ${num('update', 7)}  `
    + `${num('same', 7)}  ${num('strings', 9)}  ${num('characters', 11)}`, { quiet: options.quiet });
  for (const row of rows) {
    log(`${pad(row.code, width)}  ${num(row.create, 7)}  ${num(row.update, 7)}  `
      + `${num(row.unchanged, 7)}  ${num(row.strings, 9)}  ${num(row.characters, 11)}`,
    { quiet: options.quiet });
  }
  log(`${pad('', width)}  ${num(totalCreate, 7)}  ${num(totalUpdate, 7)}  `
    + `${num(totalUnchanged, 7)}  ${num(totalStrings, 9)}  ${num(totalCharacters, 11)}`,
  { quiet: options.quiet });

  const brokenSources = plans.filter((plan) => plan?.error).length;
  if (brokenSources) warn(`\n! ${brokenSources} module(s) have an unreadable en.json source and were skipped`);
  if (badTargets) warn(`! ${badTargets} existing language file(s) are not valid JSON and will be rebuilt`);

  // Identical values are normal (module names, abbreviations), but a file that is
  // mostly English has simply never been translated; surface those instead of
  // silently treating them as done.
  const mostlyEnglish = [];
  tasks.forEach((task, index) => {
    const plan = plans[index];
    if (!plan || plan.error || plan.missing || plan.strings < ENGLISH_STUB_MIN_STRINGS) return;
    const share = plan.identical / plan.strings;
    if (share >= ENGLISH_STUB_SHARE) {
      mostlyEnglish.push(`${task.module}/${task.code} (${Math.round(share * 100)}%)`);
    }
  });
  if (mostlyEnglish.length) {
    warn(`! ${mostlyEnglish.length} file(s) are still mostly English: `
      + `${mostlyEnglish.slice(0, 5).join(', ')}${mostlyEnglish.length > 5 ? ', ...' : ''}`);
    warn('  those values count as translations; use --force to translate them again');
  }

  log(`\n${number(tasks.length)} file(s) for ${targets.length} language(s): `
    + `${number(totalCreate)} to create, ${number(totalUpdate)} to update, `
    + `${number(totalUnchanged)} already complete`, { quiet: options.quiet });

  return { totalCreate, totalUpdate, totalUnchanged, totalStrings, totalCharacters, mostlyEnglish };
}

async function main(argv = process.argv.slice(2)) {
  const options = parseArgs(argv);
  if (options.help) {
    console.log(HELP);
    return 0;
  }

  const translationsDir = path.resolve(options.translations);
  const manifestPath = path.resolve(options.manifest);

  let manifest;
  try {
    manifest = await readJson(manifestPath);
  } catch (error) {
    throw new UsageError(`could not read ${manifestPath}: ${error.message}`);
  }

  const { targets, unmapped, unusedFilter } = resolveTargets(
    declaredLanguages(manifest),
    options.langs,
  );
  if (unusedFilter.length) {
    throw new UsageError(
      `--lang ${unusedFilter.join(', ')} is not declared in ${path.basename(manifestPath)}`,
    );
  }
  if (!targets.length) throw new UsageError('no target languages to translate');
  if (unmapped.length) {
    warn(`! no DeepL mapping for ${unmapped.join(', ')}, add them to DEEPL_TARGETS to include them`);
  }

  let modules;
  try {
    modules = await listModules(translationsDir, options.modules);
  } catch (error) {
    throw new UsageError(`could not read ${translationsDir}: ${error.message}`);
  }
  if (!modules.length) throw new UsageError(`no module folders found in ${translationsDir}`);

  // Load every English source once, then build the task list.
  const sources = new Map();
  const missingSources = [];
  await runPool(modules, options.concurrency, async (module) => {
    const file = path.join(translationsDir, module, SOURCE_FILE);
    const { data, error } = await readJsonObject(file);
    if (data) sources.set(module, { data, error: null });
    else {
      sources.set(module, { data: null, error: error ?? 'missing en.json' });
      missingSources.push(module);
    }
  });
  if (missingSources.length) {
    warn(`! no en.json source in ${missingSources.length} folder(s): `
      + `${missingSources.slice(0, 5).join(', ')}${missingSources.length > 5 ? ', ...' : ''}`);
  }

  const usableModules = modules.filter((module) => sources.get(module)?.data);
  if (!usableModules.length) throw new UsageError('no module has an en.json source to translate');

  const tasks = [];
  for (const module of usableModules) {
    for (const target of targets) {
      tasks.push({
        module,
        code: target.code,
        deepl: target.deepl,
        file: path.join(translationsDir, module, `${target.key}.json`),
      });
    }
  }

  log(`Translations: ${translationsDir}`);
  log(`Source:       ${SOURCE_FILE} in ${usableModules.length} module folder(s)`);
  log(`Languages:    ${targets.map((target) => target.code).join(', ')}`);

  const plans = await planTasks({ tasks, sources, options });
  const totals = printPlan({ tasks, plans, targets, options });

  if (options.dryRun) {
    log('\nDry run: nothing was translated and no file was written.', { quiet: options.quiet });
    return 0;
  }

  const authKey = await loadAuthKey();
  if (!authKey) throw new UsageError(`DEEPL_API is missing from ${SECRETS_PATH}`);
  const client = await createDeepLClient(authKey);

  const logs = [];
  const { targets: supportedTargets, skipped } = await filterSupportedTargets(client, targets, logs);
  logs.forEach((line) => warn(`! ${line}`));
  if (skipped.length) {
    warn(`! DeepL does not support these targets, skipping them: ${skipped.join(', ')}`);
  }
  if (!supportedTargets.length) throw new UsageError('none of the target languages is supported by DeepL');

  const supportedKeys = new Set(supportedTargets.map((target) => target.key));
  const queue = tasks
    .map((task, index) => ({ task, plan: plans[index] }))
    .filter((entry) => supportedKeys.has(entry.task.code.toLowerCase()));
  // With a character budget, cheapest files first so the budget covers as many
  // modules as possible instead of being eaten by the few largest files.
  const ordered = options.maxCharacters ? orderByCost(queue) : queue;

  const send = async (texts, deeplCode) => {
    const response = await client.translateText(texts, options.sourceLang, deeplCode);
    const list = Array.isArray(response) ? response : [response];
    return list.map((item) => ({
      text: typeof item?.text === 'string' ? item.text : '',
      billed: Number(item?.billedCharacters) || 0,
    }));
  };

  let before = null;
  try {
    before = formatUsage(await client.getUsage());
  } catch (error) {
    warn(`! could not read DeepL usage (${error.message})`);
  }
  if (before) log(`\nDeepL before: ${before}`);

  const budget = options.maxCharacters;
  let spent = 0;
  let skippedByBudget = 0;
  const results = new Array(ordered.length);
  const progressEvery = Math.max(10, Math.floor(ordered.length / 20));
  let done = 0;
  let stringsDone = 0;

  await runPool(ordered, options.concurrency, async ({ task, plan }, index) => {
    if (!plan || plan.error) {
      results[index] = { skipped: true, error: plan?.error };
      return;
    }
    if (budget && spent + plan.characters > budget) {
      skippedByBudget += 1;
      results[index] = { skipped: true, budget: true };
      return;
    }
    spent += plan.characters;

    const source = sources.get(task.module);
    const target = await readJsonObject(task.file);
    if (options.verbose) {
      log(`  ${task.module}/${task.code}: ${plan.pending} string(s), ${number(plan.characters)} char(s)`,
        { quiet: false });
    }

    const result = await translateTask({ task, source: source.data, target, send, options });
    results[index] = result;
    done += 1;
    stringsDone += result.translated;

    if (done % progressEvery === 0 || done === ordered.length) {
      log(`  ... ${done}/${ordered.length} file(s) | ${number(stringsDone)} string(s) | `
        + `${number(spent)} char(s)`);
    }
  });

  // ---- summary -----------------------------------------------------------
  const summary = { created: 0, updated: 0, unchanged: 0, translated: 0, reused: 0, unresolved: 0, characters: 0 };
  const errors = [];
  for (const result of results) {
    if (!result) continue;
    if (result.error && result.skipped) {
      errors.push(result.error);
      continue;
    }
    if (result.skipped) continue;
    if (result.created) summary.created += 1;
    else if (result.updated) summary.updated += 1;
    else summary.unchanged += 1;
    summary.translated += result.translated;
    summary.reused += result.reused;
    summary.unresolved += result.unresolved ?? 0;
    summary.characters += result.characters;
    errors.push(...result.errors);
  }

  if (errors.length) {
    console.error('\nErrors:');
    for (const error of errors.slice(0, 25)) console.error(`  - ${error}`);
    if (errors.length > 25) console.error(`  ... ${errors.length - 25} more`);
  }

  let after = null;
  try {
    after = formatUsage(await client.getUsage());
  } catch {
    /* ignore */
  }

  console.log('\n' + '-'.repeat(60));
  console.log(`Files:      ${number(summary.created)} created, ${number(summary.updated)} updated, `
    + `${number(summary.unchanged)} unchanged`);
  console.log(`Strings:    ${number(summary.translated)} translated, ${number(summary.reused)} kept from existing files`);
  if (summary.unresolved) {
    console.log(`Unresolved: ${number(summary.unresolved)} string(s) got no answer and stay queued for the next run`);
  }
  console.log(`Characters: ${number(summary.characters)} billed by DeepL`);
  if (skipped.length || skippedByBudget) {
    console.log(`Skipped:    ${number(skippedByBudget)} file(s) past the --max-characters budget, `
      + `${number(skipped.length)} unsupported language(s)`);
  }
  if (after) console.log(`DeepL after: ${after}`);
  console.log(`Planned:    ${number(totals.totalStrings)} string(s) / `
    + `${number(totals.totalCharacters)} character(s) across ${number(tasks.length)} file(s)`);
  console.log(`${number(errors.length)} error(s)`);

  return errors.length ? 1 : 0;
}

const isDirectRun =
  process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href;

if (isDirectRun) {
  main()
    .then((code) => {
      process.exitCode = code;
    })
    .catch((error) => {
      if (error instanceof UsageError) {
        console.error(`error: ${error.message}`);
        process.exitCode = 2;
      } else {
        console.error(error);
        process.exitCode = 1;
      }
    });
}

export { main };
