// End-to-end check of the operator actions against whatever backend the UI points at
// (mock-ws-server or the real services). Drives the installed Edge with puppeteer-core.
//
//   node scripts/e2e.mjs [uiUrl] [gatewayUrl] [labUrl] [brainUrl]
//   default: http://127.0.0.1:5174  http://127.0.0.1:18000  http://127.0.0.1:18300  http://127.0.0.1:18200
// Gateway calls send X-TenantGuard-Control-Token (env TENANTGUARD_CONTROL_TOKEN, default dev).
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
const BRAIN = process.argv[5] || 'http://127.0.0.1:18200'
const edge = [
  process.env.EDGE_PATH,
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
].find((p) => p && existsSync(p))

const TOKEN = process.env.TENANTGUARD_CONTROL_TOKEN || 'dev'
const getJson = async (url) => (await fetch(url, { headers: { 'X-TenantGuard-Control-Token': TOKEN } })).json()
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

  // 1b. server grid: picking a tenant tile outlines its 3 barcode servers
  await page.click('[data-testid="tile-E"]')
  const lit = await page.$$eval('[data-testid^="server-w"][data-lit="1"]', (els) => els.map((e) => e.dataset.testid.slice(7)).sort())
  const barcodeE = (await getJson(`${GW}/admin/tenants`)).find((x) => x.id === 'E').servers.slice().sort()
  check('3x3 grid shows 9 servers', (await page.$$('[data-testid^="server-w"]')).length === 9)
  check("tile click highlights the tenant's barcode", JSON.stringify(lit) === JSON.stringify(barcodeE), `lit=${lit} barcode=${barcodeE}`)
  await page.click('[data-testid="tile-E"]') // unpick

  // 1c. case file: open the newest incident from the timeline, compare with the brain's REST view
  const haveIncident = await until('an incident exists', async () => (await getJson(`${BRAIN}/api/incidents?limit=5`)).length > 0, 40000).then(
    () => true,
    () => false,
  )
  if (!haveIncident) console.log('SKIP  case file (no incident on the brain yet)')
  else {
    await page.waitForSelector('[data-testid="timeline-row"]', { timeout: 10000 })
    const href = await page.$eval('[data-testid="timeline-row"]', (a) => a.getAttribute('href'))
    await page.click('[data-testid="timeline-row"]')
    await page.waitForSelector('[data-testid="case-steps"]', { timeout: 10000 })
    const id = decodeURIComponent(href.split('/case/')[1])
    check('timeline row opens /case/:incident_id', page.url().endsWith(href), page.url())
    const rest = await getJson(`${BRAIN}/api/incidents?incident_id=${encodeURIComponent(id)}`)
    const reached = await page.$$eval('[data-testid="case-steps"] li[data-reached="1"]', (els) => els.length)
    check('case steps reached match the brain', reached > 0 && reached <= new Set(rest.map((e) => e.stage)).size, `reached=${reached}`)
    const logged = await until('case log filled', async () => {
      const n = await page.$$eval('[data-testid="case-log"] li', (els) => els.length)
      return n >= rest.length && n
    }).catch(() => 0)
    check('case log shows every event of the incident', logged >= rest.length, `ui=${logged} brain=${rest.length}`)
    const hasReport = !!(await page.$('[data-testid="report"], [data-testid="report-placeholder"]'))
    check('report or template placeholder rendered', hasReport)
    await page.goto(UI + '/', { waitUntil: 'networkidle2' })
    await page.waitForSelector('[data-testid="tile-L"]')
  }

  // 1d. scorecard: one chart per scenario from lab /lab/results
  const results = await fetch(`${LAB}/lab/results`).then((r) => (r.ok ? r.json() : null), () => null)
  await page.goto(UI + '/scorecard', { waitUntil: 'networkidle2' })
  if (!results) {
    const alert = await page.waitForSelector('[role="alert"]', { timeout: 8000 }).then(() => true, () => false)
    check('scorecard shows an error when lab results are unavailable', alert)
  } else {
    const list = Array.isArray(results) ? results : results.results || []
    const scenarios = new Set(list.map((r) => r.scenario)).size
    if (scenarios === 0) console.log('SKIP  scorecard charts (no runs yet)')
    else {
      await page.waitForSelector('[data-testid="score-table"]', { timeout: 8000 })
      const charts = await page.$$eval('[data-testid^="score-"]:not([data-testid="score-table"])', (els) => els.length)
      const rows = await page.$$eval('[data-testid="score-table"] tbody tr', (els) => els.length)
      check('scorecard: one chart per scenario', charts === scenarios, `charts=${charts} scenarios=${scenarios}`)
      check('scorecard: scenario x 3 modes table', rows === scenarios * 3, `rows=${rows}`)
    }
  }
  await page.goto(UI + '/', { waitUntil: 'networkidle2' })
  await page.waitForSelector('[data-testid="tile-L"]')

  // Shared stacks: never disturb a measured lab run (mode changes and attacks would skew it).
  const lab = await fetch(`${LAB}/lab/status`).then((r) => (r.ok ? r.json() : null), () => null)
  const labBusy = !!lab?.run
  if (labBusy) console.log(`SKIP  mode toggle + attacks (lab run ${lab.run} in progress)`)

  // 2. mode toggle
  if (!labBusy) {
    const before = (await getJson(`${GW}/admin/mode`)).mode
    const target = before === 'industry' ? 'off' : 'industry'
    const label = { off: 'OFF', industry: 'Industry', tenantguard: 'TenantGuard' }
    await page.locator(`::-p-xpath(//button[@role="radio" and normalize-space()="${label[target]}"])`).click()
    const after = await until('mode change', async () => {
      const m = (await getJson(`${GW}/admin/mode`)).mode
      return m === target && m
    })
    check(`mode toggle ${before} -> ${target}`, after === target, `GET /admin/mode = ${after}`)
    await page.locator(`::-p-xpath(//button[@role="radio" and normalize-space()="${label[before]}"])`).click()
    await until(`mode back to ${before}`, async () => (await getJson(`${GW}/admin/mode`)).mode === before)
    check(`mode toggle back to ${before}`, true)
  }

  // 3+4. attack launcher
  if (!lab) console.log(`SKIP  attack launch / stop (lab ${LAB} unreachable)`)
  else if (!labBusy) {
    const runningBefore = new Set(lab.attacks.filter((a) => a.state === 'running').map((a) => a.attack_id))
    await page.select('select[title]', 'flood')
    const selects = await page.$$('select')
    await selects[1].select('B')
    await page.locator('::-p-xpath(//button[starts-with(normalize-space(), "Launch on B")])').click()
    const started = await until('attack visible in lab', async () => {
      const st = await getJson(`${LAB}/lab/status`)
      return st.attacks.find((a) => !runningBefore.has(a.attack_id) && a.attack === 'flood' && a.tenant === 'B' && a.state === 'running')
    })
    check('launcher starts flood on B', !!started, `${started.attack_id} intensity=${started.intensity}`)

    // 4. stop it from the running list (wait for this attack's own row; the list polls every 2 s)
    const stopBtn = await until('flood row shown', () =>
      page.$('::-p-xpath(//div[.//b[normalize-space()="flood"] and contains(., "→ B")]/button[normalize-space()="Stop"])'),
    )
    await stopBtn.click()
    const stopped = await until('attack stopped', async () => {
      const st = await getJson(`${LAB}/lab/status`)
      const a = st.attacks.find((x) => x.attack_id === started.attack_id)
      return a && a.state !== 'running' && a
    })
    check('Stop button stops the attack', !!stopped, `state=${stopped.state}`)
  }

  if (labBusy) console.log('SKIP  quota edit (lab run in progress)')
  else {
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

    // leave the shared gateway as we found it
    const restored = await fetch(`${GW}/admin/tenants/E/quota`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json', 'X-TenantGuard-Control-Token': TOKEN },
      body: JSON.stringify(cur),
    }).then((r) => r.ok)
    check('original quota of E restored', restored, JSON.stringify(cur))
  }

  check('no uncaught page errors', errors.length === 0, errors.join(' | '))

} finally {
  await browser.close()
}
console.log(failures ? `\n${failures} check(s) FAILED` : '\nall e2e checks passed')
process.exit(failures ? 1 : 0)
