// Layout and clarity at the two screen sizes the dashboard must support. Screenshots are always
// attached to the report; with E2E_SCREENSHOTS=1 they are also written to ui/screenshots/.
import path from 'node:path'
import { expect, test } from './fixtures.ts'

const SIZES = [
  { width: 1366, height: 768 },
  { width: 1920, height: 1080 },
]

interface Box {
  name: string
  x: number
  y: number
  w: number
  h: number
}

const overlap = (a: Box, b: Box) => a.x < b.x + b.w - 1 && b.x < a.x + a.w - 1 && a.y < b.y + b.h - 1 && b.y < a.y + a.h - 1

for (const size of SIZES) {
  test.describe(`Control Room at ${size.width}x${size.height}`, () => {
    test.use({ viewport: size })

    test('panels never overlap and nothing spills out of its box', async ({ page }) => {
      await page.goto('/?view=tiles')
      await expect(page.getByTestId('tile-L')).toBeVisible({ timeout: 20_000 })
      await page.waitForTimeout(1500) // a few snapshots, charts drawn

      const boxes: Box[] = await page.locator('[data-panel]').evaluateAll((els) =>
        els.map((el) => {
          const r = el.getBoundingClientRect()
          return { name: el.getAttribute('data-panel')!, x: r.x, y: r.y + window.scrollY, w: r.width, h: r.height }
        }),
      )
      expect(boxes.map((b) => b.name).sort()).toEqual(['cpu', 'launcher', 'noisy', 'p95', 'servers', 'status', 'tiles', 'timeline'])
      for (const b of boxes) expect(b.h, `${b.name} has a real height`).toBeGreaterThan(60)
      for (let i = 0; i < boxes.length; i++)
        for (let j = i + 1; j < boxes.length; j++) expect(overlap(boxes[i], boxes[j]), `${boxes[i].name} overlaps ${boxes[j].name}`).toBe(false)

      // server cells and the status bar keep their text inside
      const spills = await page.locator('[data-testid^="server-w"], [data-panel="status"]').evaluateAll((els) =>
        els.filter((el) => el.scrollHeight > el.clientHeight + 1 || el.scrollWidth > el.clientWidth + 1).map((el) => el.getAttribute('data-testid') ?? el.getAttribute('data-panel')),
      )
      expect(spills, 'elements whose content overflows').toEqual([])
      // the page itself never scrolls sideways
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true)
    })

    test('the status bar keeps its height and the headline is always shown in full', async ({ page }) => {
      await page.goto('/?view=tiles')
      await expect(page.getByTestId('status-bar')).toBeVisible({ timeout: 20_000 })
      const status = page.locator('[data-panel="status"]')
      const heights: number[] = []
      for (let i = 0; i < 6; i++) {
        heights.push(Math.round((await status.boundingBox())!.height))
        await page.waitForTimeout(500)
      }
      expect(new Set(heights).size, `status bar heights ${heights}`).toBe(1)
      const head = await page.getByTestId('status-sentence').evaluate((el) => {
        const cs = getComputedStyle(el)
        return {
          h: el.getBoundingClientRect().height,
          lh: parseFloat(cs.lineHeight),
          clipped: el.scrollWidth > el.clientWidth + 1 || el.scrollHeight > el.clientHeight + 1,
          ellipsis: cs.textOverflow === 'ellipsis',
        }
      })
      expect(head.h, 'headline wraps to at most two lines').toBeLessThanOrEqual(2 * head.lh + 2)
      expect(head.clipped, 'headline text is not clipped').toBe(false)
      expect(head.ellipsis, 'headline never uses an ellipsis').toBe(false)
      await expect(page.getByTestId('scenario-badge')).not.toBeEmpty()
    })

    test('every tile with a level badge shows why, and the screen is captured', async ({ page }, testInfo) => {
      await page.goto('/?view=tiles')
      await expect(page.getByTestId('tile-L')).toBeVisible({ timeout: 20_000 })
      await page.waitForTimeout(6000) // let the charts collect a few seconds of history for the screenshot
      const tiles = await page.locator('[data-testid^="tile-"]').evaluateAll((els) =>
        els.map((el) => ({ id: el.getAttribute('data-testid')!.slice(5), badge: el.querySelector('span.uppercase.tracking-wide')?.textContent?.trim() ?? '' })),
      )
      expect(tiles).toHaveLength(12)
      for (const t of tiles) {
        if (t.badge && t.badge.toLowerCase() !== 'normal') await expect(page.getByTestId(`why-${t.id}`), `tile ${t.id} (${t.badge}) explains why`).toBeVisible()
      }
      const file = `control-room-${size.width}x${size.height}.png`
      // grow the viewport to the page height and let the charts re-measure, instead of fullPage
      // capture (which resizes mid-shot and catches Recharts in a transient narrow layout)
      const full = await page.evaluate(() => document.documentElement.scrollHeight)
      await page.setViewportSize({ width: size.width, height: Math.max(size.height, full) })
      await page.waitForTimeout(1200)
      const shot = await page.screenshot()
      await page.setViewportSize(size)
      await testInfo.attach(file, { body: shot, contentType: 'image/png' })
      if (process.env.E2E_SCREENSHOTS === '1') {
        const { writeFile, mkdir } = await import('node:fs/promises')
        const dir = path.resolve('screenshots')
        await mkdir(dir, { recursive: true })
        await writeFile(path.join(dir, process.env.E2E_SCREENSHOT_PREFIX ? `${process.env.E2E_SCREENSHOT_PREFIX}-${file}` : file), shot)
      }
    })
  })
}
