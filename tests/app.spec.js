// @ts-check
const { test, expect } = require('@playwright/test');
const path = require('path');

const FILE_URL = 'file://' + path.join(__dirname, '..', 'docs', 'index.html');

async function loadApp(page) {
  await page.goto(FILE_URL);
  await expect(page.locator('#results-info')).not.toBeEmpty({ timeout: 5000 });
}

// ── 1. Page load ──────────────────────────────────────────────────────────────
test('page loads and renders offer cards', async ({ page }) => {
  await loadApp(page);

  await expect(page.locator('.product-card')).toHaveCountGreaterThan(0);
  await expect(page.locator('#status-txt')).toContainText('Prospekte');
});

// ── 2. Search ─────────────────────────────────────────────────────────────────
test('search input filters offer cards', async ({ page }) => {
  await loadApp(page);

  const totalText = await page.locator('#results-info').textContent();
  const totalCount = parseInt((totalText.match(/(\d[\d.]*)$/) || ['0','0'])[1].replace('.',''), 10);

  await page.locator('#q').fill('Milch');
  await page.waitForTimeout(200);

  const filteredText = await page.locator('#results-info').textContent();
  const filteredCount = parseInt((filteredText.match(/^(\d[\d.]*)/) || ['0','0'])[1].replace('.',''), 10);

  expect(filteredCount).toBeGreaterThan(0);
  expect(filteredCount).toBeLessThan(totalCount);

  await page.locator('.x').click();
  await expect(page.locator('#results-info')).toContainText(totalText.trim().slice(0, 6));
});

// ── 3. Category tab ───────────────────────────────────────────────────────────
test('category tab filters cards', async ({ page }) => {
  await loadApp(page);

  await expect(page.locator('.tab.active')).toContainText('Alle');
  await page.locator('.tab', { hasText: 'Lebensmittel' }).click();
  await expect(page.locator('.tab.active')).toContainText('Lebensmittel');
  await expect(page.locator('.product-card')).toHaveCountGreaterThan(0);

  await page.locator('.tab', { hasText: 'Alle' }).click();
  await expect(page.locator('.tab.active')).toContainText('Alle');
});

// ── 4. KW filter ──────────────────────────────────────────────────────────────
test('week filter narrows results', async ({ page }) => {
  await loadApp(page);

  await expect(page.locator('.kw-btn.active')).toHaveText('Alle Wochen');

  const dieseWoche = page.locator('.kw-btn', { hasText: 'Diese Woche' });
  await dieseWoche.click();
  await expect(dieseWoche).toHaveClass(/active/);
  await expect(page.locator('.product-card')).toHaveCountGreaterThan(0);

  await page.locator('.kw-btn', { hasText: 'Alle Wochen' }).click();
});

// ── 5. Sort by price ──────────────────────────────────────────────────────────
test('sort by price ascending orders cards correctly', async ({ page }) => {
  await loadApp(page);

  await page.locator('.sort-btn[data-sort="price-asc"]').click();
  await expect(page.locator('.sort-btn[data-sort="price-asc"]')).toHaveClass(/active/);

  const prices = await page.locator('.pc-price').evaluateAll(els =>
    els.slice(0, 5).map(el => parseFloat(el.textContent.replace('€','').replace(',','.').trim()) || 0)
  );

  expect(prices.length).toBeGreaterThan(0);
  for (let i = 1; i < prices.length; i++) {
    expect(prices[i]).toBeGreaterThanOrEqual(prices[i - 1]);
  }
});

// ── 6. Retailer filter ────────────────────────────────────────────────────────
test('retailer filter shows only selected retailer', async ({ page }) => {
  await loadApp(page);

  const select = page.locator('#retailer-select');
  await select.selectOption({ label: /REWE/ });

  const labels = page.locator('.pc-retailer');
  const count = await labels.count();
  expect(count).toBeGreaterThan(0);

  for (let i = 0; i < Math.min(count, 5); i++) {
    await expect(labels.nth(i)).toContainText('REWE');
  }

  await select.selectOption({ value: '' });
});

// ── 7. Detail modal ───────────────────────────────────────────────────────────
test('clicking a product card opens the detail panel', async ({ page }) => {
  await loadApp(page);

  const overlay = page.locator('#detail-overlay');
  await expect(overlay).not.toHaveClass(/open/);

  await page.locator('.product-card').first().click();
  await expect(overlay).toHaveClass(/open/);
  await expect(page.locator('#detail-content .dp-title')).not.toBeEmpty();

  await page.locator('#detail-content .dp-close').click();
  await expect(overlay).not.toHaveClass(/open/);
});

// ── 8. Like → shopping list ───────────────────────────────────────────────────
test('liking a product adds it to the shopping list', async ({ page }) => {
  await loadApp(page);
  await page.evaluate(() => localStorage.removeItem('liked_offers'));

  const firstLikeBtn = page.locator('.like-btn').first();
  await firstLikeBtn.click();
  await expect(firstLikeBtn).toHaveClass(/liked/);
  await expect(page.locator('#like-count-badge')).toHaveText('1');

  await page.locator('.view-btn[data-view="einkaufsliste"]').click();
  await expect(page.locator('.el-row')).toHaveCountGreaterThan(0);

  await page.evaluate(() => localStorage.removeItem('liked_offers'));
});

// ── 9. Prospekte view ─────────────────────────────────────────────────────────
test('prospekte view shows leaflet cards', async ({ page }) => {
  await loadApp(page);

  await page.locator('.view-btn[data-view="prospekte"]').click();
  await expect(page.locator('.view-btn[data-view="prospekte"]')).toHaveClass(/active/);
  await expect(page.locator('#toolbar')).toBeHidden();
  await expect(page.locator('.leaflet-card')).toHaveCountGreaterThan(0);
  await expect(page.locator('.leaflet-card').first().locator('.lc-retailer')).not.toBeEmpty();
});

// ── 10. Settings panel ────────────────────────────────────────────────────────
test('settings panel opens and closes', async ({ page }) => {
  await loadApp(page);

  const overlay = page.locator('#settings-overlay');
  await expect(overlay).not.toHaveClass(/open/);

  await page.locator('#settings-btn').click();
  await expect(overlay).toHaveClass(/open/);
  await expect(page.locator('#sp-list .sp-row').first()).toBeVisible();

  await page.locator('.sp-close').click();
  await expect(overlay).not.toHaveClass(/open/);
});

// ── 11. Data integrity: no Globus ─────────────────────────────────────────────
test('no Globus or Globus Baumarkt in dataset', async ({ page }) => {
  await loadApp(page);

  const globusOffers = await page.evaluate(() => {
    const bad = ['globus', 'globus baumarkt'];
    return (window.__DATA__.offers || [])
      .filter(o => bad.some(b => o.retailer.toLowerCase().includes(b)))
      .map(o => ({ retailer: o.retailer, title: o.title }));
  });

  expect(globusOffers).toHaveLength(0);
});

// ── 12. Status chip ───────────────────────────────────────────────────────────
test('status chip shows offer and leaflet counts', async ({ page }, testInfo) => {
  if (testInfo.project.name === 'mobile-safari') {
    testInfo.skip(true, 'status-chip is hidden on mobile viewport');
  }

  await loadApp(page);

  const text = await page.locator('#status-txt').textContent();
  expect(text).toMatch(/\d+ Prospekte/);
  expect(text).toMatch(/[\d.]+ Angebote/);
});

// ── 13. Mobile smoke test ─────────────────────────────────────────────────────
test('mobile: search and prospekte view work at 393x852', async ({ page }) => {
  await page.setViewportSize({ width: 393, height: 852 });
  await loadApp(page);

  await expect(page.locator('.product-card')).toHaveCountGreaterThan(0);

  await page.locator('#q').fill('Wasser');
  await page.waitForTimeout(200);
  await expect(page.locator('#results-info')).toContainText('von');
  await page.locator('.x').click();

  await page.locator('.view-btn[data-view="prospekte"]').click();
  await expect(page.locator('.leaflet-card')).toHaveCountGreaterThan(0);
});
