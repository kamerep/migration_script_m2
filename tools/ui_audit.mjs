// Interface Craft audit (playbook I2). Renders pages in headless Chromium at
// several widths and reports, per page and width:
//   - horizontal overflow
//   - tap targets under 44 px (touch widths)
//   - images without dimensions
//   - console errors and CSP violations
//   - structure (first width only): lang, title, one h1, heading order, main
//     landmark, images without alt, unnamed controls, unlabelled inputs,
//     duplicate ids, zoom disabled
//   - animation still running under prefers-reduced-motion (first width only)
//   - axe-core serious and critical findings, when axe-core is installed
// Saves screenshots. Exits 1 when anything is found.
//
// Requirements: npm i -D playwright-core (or point PW_CORE at an install) and
// a Chromium/Chrome binary (CHROME_PATH). Optional: npm i -D axe-core.
//
// Usage:
//   BASE_URL=http://localhost:4321 PAGES="/,/about/,/contact/" \
//   CHROME_PATH=/path/to/chrome node audit.mjs
//
// Options (environment):
//   WIDTHS="320,360,390,768,1024,1440"   viewport widths
//   CSP_FROM=dist/.htaccess              inject the production CSP header
//   OUT=audit-shots                      screenshot and report folder
//   REPORT=json                          also write OUT/report.json
//   AXE=0                                skip axe-core even if installed
//   COLOR_SCHEME=dark                    emulate a colour scheme
//   FULL_PAGE=1                          full-page screenshots
import { readFileSync, mkdirSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PW_CORE || 'playwright-core');

const base = process.env.BASE_URL || 'http://localhost:4321';
const pages = (process.env.PAGES || '/').split(',').map((p) => p.trim()).filter(Boolean);
const widths = (process.env.WIDTHS || '320,360,390,768,1024,1440').split(',').map(Number);
const out = process.env.OUT || 'audit-shots';
const csp = process.env.CSP_FROM
  ? (readFileSync(process.env.CSP_FROM, 'utf8').match(/Content-Security-Policy"?\s*"([^"]+)"/) || [])[1]
  : '';
const colorScheme = process.env.COLOR_SCHEME || undefined;
mkdirSync(out, { recursive: true });

let axeSource = '';
if (process.env.AXE !== '0') {
  try { axeSource = readFileSync(require.resolve(process.env.AXE_PATH || 'axe-core/axe.min.js'), 'utf8'); } catch { /* optional */ }
}

async function withCsp(ctx) {
  if (!csp) return;
  await ctx.route('**/*', async (route) => {
    const res = await route.fetch();
    const headers = { ...res.headers() };
    if ((headers['content-type'] || '').includes('text/html')) headers['content-security-policy'] = csp;
    await route.fulfill({ response: res, headers });
  });
}

// Runs in the page. Layout checks for every width.
function layoutChecks(isTouch) {
  const docW = document.documentElement.scrollWidth;
  const small = [];
  if (isTouch) {
    for (const el of document.querySelectorAll('a[href], button, [role=button], [role=tab], summary, input:not([type=hidden]), select, textarea')) {
      if (el.closest('[aria-hidden="true"], .sr-only, .visually-hidden')) continue;
      // Layout size (offsetWidth/Height) ignores transforms, so elements
      // still mid-animation (scaled reveals) are measured at full size.
      const b = { width: el.offsetWidth || el.getBoundingClientRect().width, height: el.offsetHeight || el.getBoundingClientRect().height };
      const s = getComputedStyle(el);
      if (!b.width || s.visibility === 'hidden' || s.display === 'none') continue;
      const inlineInText = s.display === 'inline' && el.closest('p, li, dd, td, figcaption, label');
      // A checkbox or radio shares its hit target with its label.
      if (el.matches('input[type=checkbox], input[type=radio]') && [...(el.labels || [])].some((l) => l.offsetHeight >= 43.5)) continue;
      const stretched = getComputedStyle(el, '::after').position === 'absolute' && getComputedStyle(el, '::after').content !== 'none';
      if (b.height < 43.5 && !inlineInText && !stretched) small.push(`${el.tagName.toLowerCase()} "${(el.textContent || el.getAttribute('aria-label') || '').trim().slice(0, 30)}" ${Math.round(b.width)}x${Math.round(b.height)}`);
    }
  }
  const noDims = [...document.images].filter((i) => !i.getAttribute('width') || !i.getAttribute('height')).map((i) => i.currentSrc.split('/').pop()).slice(0, 5);
  return { overflow: docW > innerWidth ? docW - innerWidth : 0, small, noDims };
}

// Runs in the page. Structure checks, once per page. A coarse screen:
// it does not replace axe-core or the manual script (playbook I7).
function structureChecks() {
  const issues = [];
  // An element inside a display:none ancestor (a hidden tab or SPA page) keeps its own display value,
  // so ask the browser whether it renders at all.
  const shown = (el) => { const s = getComputedStyle(el); return (el.checkVisibility ? el.checkVisibility() : el.getClientRects().length > 0)
    && s.visibility !== 'hidden' && !el.closest('[hidden], [aria-hidden="true"]'); };
  const ref = (el, attr) => (el.getAttribute(attr) || '').split(/\s+/).map((id) => document.getElementById(id)?.textContent || '').join(' ').trim();
  const name = (el) => (el.getAttribute('aria-label') || ref(el, 'aria-labelledby') || el.textContent || el.getAttribute('title')
    || [...el.querySelectorAll('img[alt], svg title')].map((n) => n.getAttribute('alt') || n.textContent).join(' ') || el.value || '').trim();
  const tag = (el) => `${el.tagName.toLowerCase()}${el.id ? '#' + el.id : ''}${el.classList[0] ? '.' + el.classList[0] : ''}`;

  if (!document.documentElement.getAttribute('lang')) issues.push('no lang on <html>');
  if (!document.title.trim()) issues.push('empty <title>');
  const vp = document.querySelector('meta[name=viewport]')?.content || '';
  if (/user-scalable\s*=\s*(no|0)/i.test(vp) || /maximum-scale\s*=\s*1(\.0)?(\D|$)/i.test(vp)) issues.push('zoom disabled in viewport meta');
  const hs = [...document.querySelectorAll('h1, h2, h3, h4, h5, h6')].filter(shown);
  const h1 = hs.filter((h) => h.tagName === 'H1').length;
  if (h1 !== 1) issues.push(`${h1} visible <h1> (expected 1)`);
  let prev = 0;
  for (const h of hs) { const l = +h.tagName[1]; if (prev && l > prev + 1) { issues.push(`heading skips h${prev} to h${l}: "${h.textContent.trim().slice(0, 30)}"`); break; } prev = l; }
  if (!document.querySelector('main, [role=main]')) issues.push('no <main> landmark');
  const noAlt = [...document.images].filter((i) => !i.hasAttribute('alt') && shown(i));
  if (noAlt.length) issues.push(`${noAlt.length} image(s) without alt: ${noAlt.slice(0, 3).map((i) => i.currentSrc.split('/').pop()).join(', ')}`);
  const unnamed = [...document.querySelectorAll('a[href], button, [role=button], [role=tab], [role=link], summary')].filter((el) => shown(el) && !name(el));
  if (unnamed.length) issues.push(`${unnamed.length} control(s) without a name: ${unnamed.slice(0, 3).map(tag).join(', ')}`);
  const unlabelled = [...document.querySelectorAll('input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=image]), select, textarea')]
    .filter((el) => shown(el) && !(el.labels && el.labels.length) && !el.getAttribute('aria-label') && !ref(el, 'aria-labelledby') && !el.getAttribute('title'));
  if (unlabelled.length) issues.push(`${unlabelled.length} field(s) without a label: ${unlabelled.slice(0, 3).map(tag).join(', ')}`);
  const ids = {}; for (const el of document.querySelectorAll('[id]')) ids[el.id] = (ids[el.id] || 0) + 1;
  const dup = Object.keys(ids).filter((k) => ids[k] > 1);
  if (dup.length) issues.push(`duplicate id(s): ${dup.slice(0, 4).join(', ')}`);
  const noType = [...document.querySelectorAll('form button:not([type])')].length;
  if (noType) issues.push(`${noType} button(s) in forms without type`);
  return issues;
}

const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH });
const report = [];
let problems = 0;
const record = (width, path, check, issues) => {
  problems += issues.length;
  report.push({ width, path, check, issues });
  console.log(`${String(width).padStart(5)} ${path} ${check.padEnd(9)} ${issues.length ? 'FAIL  ' + issues.join('  ||  ') : 'ok'}`);
};

for (const [wi, width] of widths.entries()) {
  const touch = width < 1024;
  const ctx = await browser.newContext({ viewport: { width, height: touch ? 844 : 900 }, isMobile: touch, hasTouch: touch, colorScheme });
  await withCsp(ctx);
  for (const path of pages) {
    const page = await ctx.newPage();
    const errors = [];
    page.on('console', (m) => (m.type() === 'error' || /Content Security Policy/i.test(m.text())) && errors.push(m.text().slice(0, 160)));
    page.on('pageerror', (e) => errors.push(e.message.slice(0, 160)));
    await page.goto(base + path, { waitUntil: 'load' });
    // Scroll through so lazy content and reveals run.
    await page.evaluate(async () => {
      for (let y = 0; y < document.body.scrollHeight; y += 600) { window.scrollTo(0, y); await new Promise((r) => setTimeout(r, 50)); }
      window.scrollTo(0, 0);
    });
    await page.waitForTimeout(500);
    const r = await page.evaluate(layoutChecks, touch);
    const name = `${width}-${path.replace(/\W+/g, '_') || 'home'}`;
    await page.screenshot({ path: `${out}/${name}.png`, fullPage: process.env.FULL_PAGE === '1' });
    const issues = [];
    if (r.overflow) issues.push(`horizontal overflow ${r.overflow}px`);
    if (r.small.length) issues.push(`${r.small.length} small targets: ${r.small.slice(0, 4).join(' | ')}`);
    if (r.noDims.length) issues.push(`images without width/height: ${r.noDims.join(', ')}`);
    if (errors.length) issues.push(`console/CSP: ${errors.slice(0, 3).join(' ; ')}`);
    record(width, path, 'layout', issues);

    if (wi === 0) {
      record(width, path, 'structure', await page.evaluate(structureChecks));
      if (axeSource) {
        // Evaluated through the DevTools protocol, so the page CSP does not block it.
        await page.evaluate(axeSource);
        const v = await page.evaluate(async () => (await window.axe.run(document, { resultTypes: ['violations'] })).violations
          .filter((x) => x.impact === 'serious' || x.impact === 'critical')
          .map((x) => `${x.id} (${x.impact}, ${x.nodes.length}): ${x.nodes[0].target.join(' ')}`));
        record(width, path, 'axe', v);
      }
    }
    await page.close();
  }
  await ctx.close();
}

// Reduced motion: nothing time-based should still be running after a second.
{
  const width = widths[0];
  const ctx = await browser.newContext({ viewport: { width, height: 844 }, reducedMotion: 'reduce', colorScheme });
  await withCsp(ctx);
  for (const path of pages) {
    const page = await ctx.newPage();
    await page.goto(base + path, { waitUntil: 'load' });
    await page.waitForTimeout(1200);
    const running = await page.evaluate(() => document.getAnimations()
      .filter((a) => a.playState === 'running' && a.timeline instanceof DocumentTimeline)
      .map((a) => { const t = a.effect?.target; return `${a.animationName || a.transitionProperty || 'animation'} on ${t ? t.tagName.toLowerCase() + (t.classList[0] ? '.' + t.classList[0] : '') : '?'}`; })
      .slice(0, 5));
    const videos = await page.evaluate(() => [...document.querySelectorAll('video')].filter((v) => !v.paused).length);
    const issues = [];
    if (running.length) issues.push(`still animating: ${running.join(', ')}`);
    if (videos) issues.push(`${videos} video(s) playing`);
    record(width, path, 'motion', issues);
    await page.close();
  }
  await ctx.close();
}

await browser.close();
if (process.env.REPORT === 'json') writeFileSync(`${out}/report.json`, JSON.stringify({ base, widths, pages, axe: !!axeSource, problems, results: report }, null, 2));
if (!axeSource && process.env.AXE !== '0') console.log('\nNote: axe-core not installed; ran the built-in structure checks only (npm i -D axe-core).');
console.log(problems ? `\n${problems} issue group(s). Screenshots in ${out}/` : `\nAll clear. Screenshots in ${out}/`);
process.exit(problems ? 1 : 0);
