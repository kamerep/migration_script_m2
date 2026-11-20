// Print an HTML guide to PDF with a footer "title · page X of Y" (headless Chrome / Brave, DevTools protocol).
//   node tools/print_pdf.mjs <page.html> <out.pdf> ["Footer title"]
// tools/make_docs_pdf.sh prints without page numbers; this is for the user guide, which is read on paper.
import { spawn } from 'node:child_process';
import { mkdtempSync, writeFileSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

const [page, out, title = 'M2 Migration – User Guide'] = process.argv.slice(2);
const chrome = ['/usr/bin/google-chrome', '/usr/bin/chromium', '/usr/bin/chromium-browser', '/usr/bin/brave-browser']
  .find(p => existsSync(p));
if (!page || !out || !chrome) { console.error('usage: node tools/print_pdf.mjs <page.html> <out.pdf> [title] (needs Chrome)'); process.exit(2); }
const port = 9334;
const proc = spawn(chrome, ['--headless=new', '--no-sandbox', '--disable-gpu', `--remote-debugging-port=${port}`,
  `--user-data-dir=${mkdtempSync(join(tmpdir(), 'print-'))}`, 'about:blank'], { stdio: 'ignore' });
const sleep = ms => new Promise(r => setTimeout(r, ms));
let url;
for (let i = 0; i < 40 && !url; i++) {
  try { url = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find(t => t.type === 'page')?.webSocketDebuggerUrl; }
  catch (e) { /* starting */ }
  if (!url) await sleep(250);
}
const ws = new WebSocket(url);
await new Promise(r => ws.addEventListener('open', r, { once: true }));
let seq = 0; const waiting = new Map();
ws.addEventListener('message', ev => { const m = JSON.parse(ev.data); if (m.id && waiting.has(m.id)) { waiting.get(m.id)(m); waiting.delete(m.id); } });
const send = (method, params = {}) => new Promise(r => { const id = ++seq; waiting.set(id, r); ws.send(JSON.stringify({ id, method, params })); });
await send('Page.enable');
await send('Page.navigate', { url: 'file://' + resolve(page) });
await sleep(4000);
const esc = s => s.replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' })[c]);
const pdf = await send('Page.printToPDF', {
  printBackground: true, preferCSSPageSize: true, displayHeaderFooter: true,
  headerTemplate: '<span></span>',
  footerTemplate: `<div style="font-size:8px;color:#6b7280;width:100%;padding:0 14mm;display:flex;justify-content:space-between;font-family:system-ui,sans-serif">
    <span>${esc(title)}</span><span>Page <span class="pageNumber"></span> of <span class="totalPages"></span></span></div>`,
});
writeFileSync(out, Buffer.from(pdf.result.data, 'base64'));
console.log('Written:', out);
ws.close(); proc.kill(); process.exit(0);
