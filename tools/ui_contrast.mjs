// Contrast of every visible text of the M2 page, in each colour palette and in light and dark (playbook A2.1).
// Text below 24 px (18.66 px bold) needs 4.5:1, larger text 3:1, measured against the colour it really sits on
// (backgrounds are composed up the tree, element opacity included). Exits 1 when anything fails.
//
//   BASE_URL=http://127.0.0.1:8796 RUN=<stamp> PW_CORE=… CHROME_PATH=… node tools/ui_contrast.mjs
//
// bin/m2ctl.sh audit finds PW_CORE and CHROME_PATH and runs this with tools/ui_audit.mjs.
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PW_CORE || 'playwright-core');
const base = (process.env.BASE_URL || 'http://127.0.0.1:8796') + (process.env.RUN ? `/?run=${process.env.RUN}` : '/');
const page_ = readFileSync(new URL('../m2_ui.html', import.meta.url), 'utf8');
const palettes = [...(page_.match(/const PALETTES = \{([^}]*)\}/) || ['', ''])[1].matchAll(/(\w+):/g)].map((m) => m[1]);
const tabs = (process.env.TABS || 'overview,decide,ai,journal,records,files,review,values,finance,settings,docs').split(',');

const check = () => {
  const parse = (c) => {
    const m = c.match(/rgba?\(([^)]+)\)/) || c.match(/color\(srgb ([^)]+)\)/);
    if (!m) return null;
    const p = m[1].split(/[ ,/]+/).filter(Boolean).map(Number);
    const k = c.startsWith('color(') ? 255 : 1;
    return { r: p[0] * k, g: p[1] * k, b: p[2] * k, a: p[3] ?? 1 };
  };
  const lum = ({ r, g, b }) => [r, g, b].map((v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; })
    .reduce((s, v, i) => s + v * [0.2126, 0.7152, 0.0722][i], 0);
  const mix = (t, u) => ({ r: t.r * t.a + u.r * (1 - t.a), g: t.g * t.a + u.g * (1 - t.a), b: t.b * t.a + u.b * (1 - t.a), a: 1 });
  const bgOf = (el) => {
    const stack = [];
    for (let e = el; e; e = e.parentElement) {
      const c = parse(getComputedStyle(e).backgroundColor);
      if (c && c.a > 0) { stack.push(c); if (c.a >= 1) break; }
    }
    return stack.reverse().reduce((bg, c) => mix(c, bg), { r: 255, g: 255, b: 255, a: 1 });
  };
  const out = [];
  for (const el of document.querySelectorAll('body *')) {
    if (![...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim())) continue;
    const r = el.getBoundingClientRect(); const cs = getComputedStyle(el);
    if (!r.width || !r.height || cs.visibility === 'hidden' || el.closest('[aria-hidden="true"], .sk')) continue;
    let fg = parse(cs.color); if (!fg) continue;
    let op = 1; for (let e = el; e; e = e.parentElement) op *= +getComputedStyle(e).opacity;
    if (op === 0) continue;
    const bg = bgOf(el); fg = mix({ ...fg, a: fg.a * op }, bg);
    const [hi, lo] = [lum(fg), lum(bg)].sort((x, y) => y - x);
    const ratio = (hi + 0.05) / (lo + 0.05);
    const size = parseFloat(cs.fontSize); const need = size >= 24 || (size >= 18.66 && +cs.fontWeight >= 700) ? 3 : 4.5;
    if (ratio < need) out.push(`${ratio.toFixed(2)} < ${need}  "${el.textContent.trim().slice(0, 30)}"  ${el.tagName.toLowerCase()}.${[...el.classList].join('.')}`);
  }
  return [...new Set(out)];
};

const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH });
let failed = 0;
for (const theme of ['light', 'dark']) {
  for (const pal of palettes) {
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    await ctx.addInitScript(([p, t]) => { localStorage.setItem('m2palette', p); localStorage.setItem('m2theme', t); }, [pal, theme]);
    const page = await ctx.newPage();
    const found = new Set();
    for (const tab of tabs) {
      await page.goto(`${base}#${tab}`);
      await page.waitForFunction(() => !document.querySelector('#status.loading'), null, { timeout: 20000 }).catch(() => {});
      await page.waitForTimeout(600);
      for (const f of await page.evaluate(check)) found.add(`${tab}: ${f}`);
    }
    failed += found.size;
    console.log(`${found.size ? 'FAIL' : 'ok  '} ${pal} ${theme}${found.size ? `: ${found.size}` : ''}`);
    [...found].slice(0, 12).forEach((f) => console.log('     ' + f));
    await ctx.close();
  }
}
await browser.close();
console.log(failed ? `\n${failed} contrast failure(s).` : '\nAll text meets its contrast minimum.');
process.exit(failed ? 1 : 0);
