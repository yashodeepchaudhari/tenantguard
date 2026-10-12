// Headless Edge screenshot of a running UI (puppeteer-core, real-time wait).
//   node scripts/screenshot.mjs [url] [out.png] [waitMs] [clickSelector]
// Default: http://127.0.0.1:5173/?mock=1 -> screenshots/control-room-mock.png at 1920x1080.
// Real time (not --virtual-time-budget): that mode stalls on pages that keep polling a remote host.
import { existsSync, mkdirSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import puppeteer from 'puppeteer-core'

const url = process.argv[2] || 'http://127.0.0.1:5173/?mock=1'
const out = resolve(process.argv[3] || 'screenshots/control-room-mock.png')
const wait = Number(process.argv[4] || '20000')
const click = process.argv[5]
const edge = [
  process.env.EDGE_PATH,
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
].find((p) => p && existsSync(p))
if (!edge) throw new Error('msedge.exe not found; set EDGE_PATH')
mkdirSync(dirname(out), { recursive: true })

const browser = await puppeteer.launch({ executablePath: edge, headless: true, args: ['--window-size=1920,1080', '--hide-scrollbars'] })
try {
  const page = await browser.newPage()
  await page.setViewport({ width: 1920, height: 1080, deviceScaleFactor: 1 })
  await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 })
  if (click) {
    await page.waitForSelector(click, { timeout: 30000 })
    await new Promise((r) => setTimeout(r, Math.min(wait, 3000)))
    await page.click(click)
  }
  await new Promise((r) => setTimeout(r, wait))
  await page.screenshot({ path: out })
  console.log(`saved ${out}`)
} finally {
  await browser.close()
}
