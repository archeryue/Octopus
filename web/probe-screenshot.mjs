import { chromium } from "playwright";

const url = "http://127.0.0.1:8765";
const token = "changeme";

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
await page.goto(url);

await page.locator('input[type="password"]').fill(token);
await page.locator("button.btn-login").click();
await page.waitForSelector(".session-list-header");

// Just take a screenshot of the empty-state app
await page.waitForTimeout(800);
await page.screenshot({ path: "/tmp/octopus-empty.png", fullPage: false });
console.log("empty-state screenshot at /tmp/octopus-empty.png");

const nav = await page.locator("nav").first().evaluate((el) => {
  const cs = getComputedStyle(el);
  return {
    paddingLeft: cs.paddingLeft,
    paddingRight: cs.paddingRight,
  };
});
console.log("NAV padding:", JSON.stringify(nav));

const wordmark = await page.locator("aside > div").first().evaluate((el) => {
  const cs = getComputedStyle(el);
  return {
    paddingLeft: cs.paddingLeft,
    paddingRight: cs.paddingRight,
    className: el.className,
  };
});
console.log("Wordmark:", JSON.stringify(wordmark));

const header = await page.locator(".session-list-header").first().evaluate((el) => {
  const cs = getComputedStyle(el);
  return {
    paddingLeft: cs.paddingLeft,
    paddingRight: cs.paddingRight,
    width: el.getBoundingClientRect().width,
    left: el.getBoundingClientRect().left,
    className: el.className,
  };
});
console.log("Session header:", JSON.stringify(header));

await browser.close();
