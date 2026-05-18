import { chromium } from "playwright";
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
await page.goto("http://127.0.0.1:8765");
await page.locator('input[type="password"]').fill("changeme");
await page.locator("button.btn-login").click();
await page.waitForSelector(".session-list-header");
await page.waitForTimeout(500);
const wm = await page.locator("aside > div").first().evaluate(el => {
  const cs = getComputedStyle(el);
  return { paddingLeft: cs.paddingLeft, paddingRight: cs.paddingRight };
});
const hdr = await page.locator(".session-list-header").first().evaluate(el => {
  const cs = getComputedStyle(el);
  return { paddingLeft: cs.paddingLeft, paddingRight: cs.paddingRight };
});
const nav = await page.locator("nav").first().evaluate(el => {
  const cs = getComputedStyle(el);
  return { paddingLeft: cs.paddingLeft, paddingRight: cs.paddingRight };
});
console.log("WORDMARK:", JSON.stringify(wm));
console.log("NAV     :", JSON.stringify(nav));
console.log("HEADER  :", JSON.stringify(hdr));
await browser.close();
