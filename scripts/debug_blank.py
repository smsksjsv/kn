import os
import asyncio
from playwright.async_api import async_playwright

URL = os.environ["REACT_APP_BACKEND_URL"]

async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path="/pw-browsers/chromium_headless_shell-1208/chrome-linux/headless_shell")
        page = await b.new_page(viewport={"width": 1600, "height": 800})
        errors = []
        page.on("pageerror", lambda e: errors.append(e.stack or str(e)))
        await page.goto(URL, wait_until="networkidle")
        await page.fill('[data-testid="login-email-input"]', "admin@kainnusantara.id")
        await page.fill('[data-testid="login-password-input"]', "demo12345")
        await page.click('[data-testid="login-submit-button"]')
        await page.wait_for_timeout(3000)
        try:
            await page.click('[data-testid="scope-pick-ent_ksc"]', timeout=3000)
            await page.wait_for_timeout(1000)
        except Exception:
            pass
        await page.click('[data-testid="nav-group-toggle-penjualan"]')
        await page.wait_for_timeout(400)
        await page.click('[data-testid="nav-products-pricing"]')
        await page.wait_for_timeout(3000)
        print("ERRORS:\n", "\n---\n".join(errors[:3])[:3000])
        await b.close()

asyncio.run(main())
