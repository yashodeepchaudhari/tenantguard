// End-to-end check of the operator actions against whatever backend the UI points at
// (mock-ws-server or the real services). Drives the installed Edge with puppeteer-core.
//
//   node scripts/e2e.mjs [uiUrl] [gatewayUrl] [labUrl]
//   default: http://127.0.0.1:5174  http://127.0.0.1:18000  http://127.0.0.1:18300
//
// Verifies through the backend's own API (not the UI) that each click had its effect:
//   1. live snapshot renders 12 tiles     2. mode toggle -> GET /admin/mode
//   3. attack launcher -> GET /lab/status  4. Stop -> attack state != running
//   5. inline quota edit -> GET /admin/tenants
import { existsSync } from 'node:fs'
import puppeteer from 'puppeteer-core'

const UI = process.argv[2] || 'http://127.0.0.1:5174'
const GW = process.argv[3] || 'http://127.0.0.1:18000'
const LAB = process.argv[4] || 'http://127.0.0.1:18300'
const edge = [
  process.env.EDGE_PATH,
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
].find((p) => p && existsSync(p))

const getJson = async (url) => (await fetch(url)).json()
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
async function until(what, fn, timeoutMs = 8000) {
  const t0 = Date.now()
  for (;;) {
    const v = await fn()
    if (v) return v
    if (Date.now() - t0 > timeoutMs) throw new Error(`timeout waiting for: ${what}`)
    await sleep(200)
  }
}

let failures = 0
const check = (name, ok, extra = '') => {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${extra ? `  (${extra})` : ''}`)
  if (!ok) failures++
}

const browser = await puppeteer.launch({ executablePath: edge, headless: true, args: ['--window-size=1920,1080'] })
try {
  const page = await browser.newPage()
  await page.setViewport({ width: 1920, height: 1080 })
  const errors = []
  page.on('pageerror', (e) => errors.push(String(e)))

  // 1. live state
  await page.goto(UI + '/', { waitUntil: 'networkidle2' })
  await page.waitForSelector('[data-testid="tile-L"]', { timeout: 10000 })
  const tiles = await page.$$eval('[data-testid^="tile-"]', (els) => els.length)
  const sentence = await page.$eval('[data-testid="status-sentence"]', (el) => el.textContent)
  check('12 tenant tiles from the brain stream', tiles === 12, `tiles=${tiles}`)
  check('status sentence rendered', !!sentence && sentence.length > 5, sentence)

  // 2. mode toggle
  const before = (await getJson(`${GW}/admin/mode`)).mode
  const target = before === 'industry' ? 'off' : 'industry'
  const label = { off: 'OFF', industry: 'Industry', tenantguard: 'TenantGuard' }
  await page.locator(`::-p-xpath(//button[@role="radio" and normalize-space()="${label[target]}"])`).click()
  const after = await until('mode change', async () => {
    const m = (await getJson(`${GW}/admin/mode`)).mode
    return m === target && m
  })
  check(`mode toggle ${before} -> ${target}`, after === target, `GET /admin/mode = ${after}`)
  await page.locator(`::-p-xpath(//button[@role="radio" and normalize-space()="TenantGuard"])`).click()
  await until('mode back to tenantguard', async () => (await getJson(`${GW}/admin/mode`)).mode === 'tenantguard')
  check('mode toggle back to tenantguard', true)

  // 3. attack launcher: flood on B, intensity default, duration 30 s
  const runningBefore = new Set((await getJson(`${LAB}/lab/status`)).attacks.filter((a) => a.state === 'running').map((a) => a.attack_id))
  await page.select('select[title]', 'flood')
  const selects = await page.$$('select')
  await selects[1].select('B')
  await page.locator('::-p-xpath(//button[starts-with(normalize-space(), "Launch on B")])').click()
  const started = await until('attack visible in lab', async () => {
    const st = await getJson(`${LAB}/lab/status`)
    return st.attacks.find((a) => !runningBefore.has(a.attack_id) && a.attack === 'flood' && a.tenant === 'B' && a.state === 'running')
  })
  check('launcher starts flood on B', !!started, `${started.attack_id} intensity=${started.intensity}`)

  // 4. stop it from the running list
  await until('running row shown', () => page.$('::-p-xpath(//button[normalize-space()="Stop"])'))
  const rows = await page.$$('::-p-xpath(//div[contains(., "flood") and contains(., "→ B")]/button[normalize-space()="Stop"])')
  await rows[rows.length - 1].click()
  const stopped = await until('attack stopped', async () => {
    const st = await getJson(`${LAB}/lab/status`)
    const a = st.attacks.find((x) => x.attack_id === started.attack_id)
    return a && a.state !== 'running' && a
  })
  check('Stop button stops the attack', !!stopped, `state=${stopped.state}`)

  // 5. inline quota edit on tenant E (values differ from the current ones, so a PATCH must happen)
  const cur = (await getJson(`${GW}/admin/tenants`)).find((x) => x.id === 'E').quota
  const newRate = cur.rate_rps === 33 ? 34 : 33
  const newConc = cur.concurrency === 5 ? 6 : 5
  await page.goto(UI + '/tenants', { waitUntil: 'networkidle2' })
  await page.waitForSelector('[data-testid="row-E"]')
  await page.locator('[data-testid="row-E"] ::-p-text(Edit quota)').click()
  const rate = await page.waitForSelector('input[aria-label="E Rate"]')
  await rate.click({ clickCount: 3 })
  await rate.type(String(newRate))
  const conc = await page.$('input[aria-label="E Concurrency"]')
  await conc.click({ clickCount: 3 })
  await conc.type(String(newConc))
  await page.locator('[data-testid="row-E"] ::-p-text(Save)').click()
  const e = await until('quota patched', async () => {
    const list = await getJson(`${GW}/admin/tenants`)
    const t = list.find((x) => x.id === 'E')
    return t.quota.rate_rps === newRate && t.quota.concurrency === newConc && t
  })
  check('inline quota edit PATCHes the gateway', !!e, `E quota=${JSON.stringify(e.quota)}`)
  const savedMsg = await page.waitForSelector('::-p-text(Quota for E saved)', { timeout: 3000 }).then(() => true, () => false)
  check('UI confirms the save', savedMsg)

  // invalid edit is refused client-side
  await page.locator('[data-testid="row-E"] ::-p-text(Edit quota)').click()
  const rate2 = await page.waitForSelector('input[aria-label="E Rate"]')
  await rate2.click({ clickCount: 3 })
  await rate2.type('-4')
  await page.locator('[data-testid="row-E"] ::-p-text(Save)').click()
  const alert = await page.waitForSelector('[data-testid="row-E"] [role="alert"]', { timeout: 3000 }).then((h) => h.evaluate((el) => el.textContent))
  check('invalid quota rejected before sending', /> 0/.test(alert ?? ''), alert)

  check('no uncaught page errors', errors.length === 0, errors.join(' | '))
} finally {
  await browser.close()
}
console.log(failures ? `\n${failures} check(s) FAILED` : '\nall e2e checks passed')
process.exit(failures ? 1 : 0)
