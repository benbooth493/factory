#!/usr/bin/env node
// shoot.mjs — full-page screenshots of one route at a few viewports.
//
//   node shoot.mjs --base http://localhost:3000 --route /settings --out <dir>
//                  [--label settings] [--viewports qa|refine|1280x800,...]
//                  [--themes light|dark|light,dark] [--select '<css>' ...]
//                  [--timeout 30000] [--settle 300]
//
// Viewports: qa = desktop 1920x1080, tablet 1024x768, mobile 375x667 (default);
//            refine = desktop 1440x900, mobile 390x844; or a comma list of WxH.
// Themes:    sets prefers-color-scheme, and for class-based dark modes also
//            localStorage.theme and a `dark` class / data-theme on <html>.
// --select:  crop that element too (repeatable); first match per selector.
//
// Files: <out>/<label>-<viewport>-<theme>.png, crops
//        <out>/<label>-<viewport>-<theme>--<selector-slug>.png.
// Prints a JSON manifest on stdout: {url, files: [{path, kind, viewport,
// width, height, theme, selector?}], warnings: [...]}.
//
// Playwright is resolved from the project (cwd), never bundled. Exit codes:
// 0 ok · 2 usage · 3 Playwright or its browser missing · 4 page failed to load.

import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import fs from 'node:fs';
import path from 'node:path';

const INSTALL_HINT =
  'Playwright is not installed in this project. From the project root run:\n' +
  '  npm i -D playwright && npx playwright install chromium';

export const PRESETS = {
  qa: [
    { name: 'desktop', width: 1920, height: 1080 },
    { name: 'tablet', width: 1024, height: 768 },
    { name: 'mobile', width: 375, height: 667 },
  ],
  refine: [
    { name: 'desktop', width: 1440, height: 900 },
    { name: 'mobile', width: 390, height: 844 },
  ],
};

export class UsageError extends Error {}

export function parseArgs(argv) {
  const opts = { select: [] };
  for (let i = 0; i < argv.length; i++) {
    const flag = argv[i];
    if (!flag.startsWith('--')) throw new UsageError(`unexpected argument: ${flag}`);
    const key = flag.slice(2);
    const val = argv[i + 1];
    if (val === undefined || val.startsWith('--')) throw new UsageError(`${flag} needs a value`);
    i++;
    if (key === 'select') opts.select.push(val);
    else opts[key] = val;
  }
  for (const k of ['base', 'route', 'out']) {
    if (!opts[k]) throw new UsageError(`--${k} is required`);
  }
  if (!opts.route.startsWith('/')) opts.route = `/${opts.route}`;
  opts.label = opts.label || routeSlug(opts.route);
  opts.viewports = parseViewports(opts.viewports || 'qa');
  opts.themes = parseThemes(opts.themes || 'light');
  opts.timeout = Number(opts.timeout || 30000);
  opts.settle = Number(opts.settle || 300);
  return opts;
}

export function parseViewports(spec) {
  if (PRESETS[spec]) return PRESETS[spec];
  return spec.split(',').map((s) => {
    const m = s.trim().match(/^(\d+)x(\d+)$/);
    if (!m) throw new UsageError(`bad viewport "${s}": use qa, refine or WxH`);
    return { name: `${m[1]}x${m[2]}`, width: Number(m[1]), height: Number(m[2]) };
  });
}

export function parseThemes(spec) {
  const themes = spec.split(',').map((s) => s.trim()).filter(Boolean);
  for (const t of themes) {
    if (t !== 'light' && t !== 'dark') throw new UsageError(`bad theme "${t}": light or dark`);
  }
  return themes;
}

export function slug(text) {
  return text.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 40);
}

export function routeSlug(route) {
  return slug(route.split('?')[0]) || 'home';
}

export function joinUrl(base, route) {
  return base.replace(/\/+$/, '') + route;
}

// Resolve playwright (or @playwright/test) the way the project's own code would.
export function loadPlaywright(fromDir) {
  const req = createRequire(path.join(fromDir, 'package.json'));
  for (const name of ['playwright', '@playwright/test']) {
    try {
      return req(name);
    } catch (err) {
      if (err.code !== 'MODULE_NOT_FOUND') throw err;
    }
  }
  return null;
}

async function waitIdle(page, timeout, warnings) {
  try {
    await page.waitForLoadState('networkidle', { timeout });
  } catch {
    warnings.push(`network never went idle within ${timeout}ms; shot taken anyway`);
  }
}

async function shoot(browser, opts, url, vp, theme, manifest) {
  const ctx = await browser.newContext({
    viewport: { width: vp.width, height: vp.height },
    colorScheme: theme,
    deviceScaleFactor: 1,
  });
  try {
    await ctx.addInitScript((t) => {
      try { localStorage.setItem('theme', t); } catch { /* storage blocked */ }
    }, theme);
    const page = await ctx.newPage();
    const res = await page.goto(url, { waitUntil: 'load', timeout: opts.timeout });
    if (res && res.status() >= 400) manifest.warnings.push(`${url} answered ${res.status()} at ${vp.name}/${theme}`);
    await waitIdle(page, opts.timeout, manifest.warnings);
    await page.evaluate((t) => {
      const h = document.documentElement;
      h.classList.toggle('dark', t === 'dark');
      h.dataset.theme = t;
    }, theme);
    // Dev-server error overlays are not the page.
    await page.addStyleTag({ content: 'nextjs-portal,vite-error-overlay{display:none!important}' });
    await page.waitForTimeout(opts.settle);

    const stem = path.join(opts.out, `${opts.label}-${vp.name}-${theme}`);
    await page.screenshot({ path: `${stem}.png`, fullPage: true });
    manifest.files.push({ path: `${stem}.png`, kind: 'page', viewport: vp.name, width: vp.width, height: vp.height, theme });

    for (const sel of opts.select) {
      const el = page.locator(sel).first();
      const file = `${stem}--${slug(sel) || 'element'}.png`;
      try {
        await el.screenshot({ path: file, timeout: 5000 });
        manifest.files.push({ path: file, kind: 'crop', viewport: vp.name, width: vp.width, height: vp.height, theme, selector: sel });
      } catch {
        manifest.warnings.push(`no visible element for "${sel}" at ${vp.name}/${theme}`);
      }
    }
  } finally {
    await ctx.close();
  }
}

export async function run(argv, cwd = process.cwd()) {
  const opts = parseArgs(argv);
  opts.out = path.resolve(cwd, opts.out);
  const pw = loadPlaywright(cwd);
  if (!pw) {
    const err = new Error(INSTALL_HINT);
    err.exitCode = 3;
    throw err;
  }
  let browser;
  try {
    browser = await pw.chromium.launch();
  } catch (e) {
    const err = new Error(`Chromium for Playwright is not installed (${e.message.split('\n')[0]}).\nRun: npx playwright install chromium`);
    err.exitCode = 3;
    throw err;
  }
  fs.mkdirSync(opts.out, { recursive: true });
  const url = joinUrl(opts.base, opts.route);
  const manifest = { url, files: [], warnings: [] };
  try {
    for (const vp of opts.viewports) {
      for (const theme of opts.themes) {
        try {
          await shoot(browser, opts, url, vp, theme, manifest);
        } catch (e) {
          const err = new Error(`could not load ${url} (${vp.name}/${theme}): ${e.message.split('\n')[0]}`);
          err.exitCode = 4;
          throw err;
        }
      }
    }
  } finally {
    await browser.close();
  }
  return manifest;
}

const isMain = process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href;
if (isMain) {
  run(process.argv.slice(2)).then(
    (manifest) => console.log(JSON.stringify(manifest, null, 2)),
    (err) => {
      console.error(`shoot: ${err.message}`);
      process.exitCode = err instanceof UsageError ? 2 : err.exitCode || 1;
    },
  );
}
