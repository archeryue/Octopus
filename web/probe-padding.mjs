import { chromium } from "playwright";

const url = process.env.URL || "http://127.0.0.1:8765";
const token = process.env.TOKEN || "changeme";

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
await page.goto(url);

// Log in
await page.locator('input[type="password"]').fill(token);
await page.locator("button.btn-login").click();
await page.waitForSelector(".session-list-header");

// Create a session via API
const fp = await page.request.post(`${url}/api/sessions`, {
  headers: { Authorization: `Bearer ${token}` },
  data: { name: "Padding Probe", working_dir: "/tmp" },
});
const session = await fp.json();

// Seed a message via import so we can inspect a bubble
await page.request.delete(`${url}/api/sessions/${session.id}`, {
  headers: { Authorization: `Bearer ${token}` },
});
const imp = await page.request.post(`${url}/api/sessions/import`, {
  headers: { Authorization: `Bearer ${token}` },
  data: {
    name: "Padding Probe",
    working_dir: "/tmp",
    messages: [{ role: "user", type: "text", content: "Hello padding world" }],
  },
});
const imported = await imp.json();

await page.locator(".session-item .session-name", { hasText: "Padding Probe" }).click();
await page.waitForSelector(".msg-user .msg-content");

const computed = await page.locator(".msg-user .msg-content").first().evaluate((el) => {
  const cs = getComputedStyle(el);
  return {
    className: el.className,
    paddingLeft: cs.paddingLeft,
    paddingRight: cs.paddingRight,
    paddingTop: cs.paddingTop,
    paddingBottom: cs.paddingBottom,
    paddingInline: cs.paddingInline,
    width: cs.width,
  };
});
console.log("MESSAGE BUBBLE:", JSON.stringify(computed, null, 2));

const sideItem = await page.locator(".session-item").first().evaluate((el) => {
  const cs = getComputedStyle(el);
  return {
    className: el.className,
    paddingLeft: cs.paddingLeft,
    paddingRight: cs.paddingRight,
    paddingInline: cs.paddingInline,
  };
});
console.log("SIDEBAR ITEM:", JSON.stringify(sideItem, null, 2));

const cssHref = await page.evaluate(() => {
  const link = document.querySelector('link[rel="stylesheet"][href*="/assets/"]');
  return link?.getAttribute("href") || null;
});
console.log("LOADED CSS:", cssHref);

// Cleanup
await page.request.delete(`${url}/api/sessions/${imported.id}`, {
  headers: { Authorization: `Bearer ${token}` },
});
await browser.close();
