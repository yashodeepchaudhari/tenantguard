// Headless Edge screenshot of a running UI.
//   node scripts/screenshot.mjs [url] [out.png] [waitMs]
// Default: http://127.0.0.1:5173/?mock=1 -> screenshots/control-room-mock.png at 1920x1080.
import { execFileSync } from 'node:child_process'
import { existsSync, mkdirSync } from 'node:fs'
import { dirname, resolve } from 'node:path'

const url = process.argv[2] || 'http://127.0.0.1:5173/?mock=1'
const out = resolve(process.argv[3] || 'screenshots/control-room-mock.png')
const wait = process.argv[4] || '20000'
const candidates = [
  process.env.EDGE_PATH,
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
].filter(Boolean)
const edge = candidates.find((p) => existsSync(p))
if (!edge) throw new Error('msedge.exe not found; set EDGE_PATH')
mkdirSync(dirname(out), { recursive: true })
execFileSync(
  edge,
  [
    '--headless=new',
    '--disable-gpu',
    '--hide-scrollbars',
    '--force-device-scale-factor=1',
    '--window-size=1920,1080',
    `--virtual-time-budget=${wait}`,
    `--screenshot=${out}`,
    url,
  ],
  { stdio: 'inherit', timeout: 120000 },
)
console.log(`saved ${out}`)
