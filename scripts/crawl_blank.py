import os
import asyncio, sys
from playwright.async_api import async_playwright

URL = os.environ["REACT_APP_BACKEND_URL"]
EXEC = "/pw-browsers/chromium_headless_shell-1208/chrome-linux/headless_shell"
EMAIL = sys.argv[1] if len(sys.argv) > 1 else "admin@kainnusantara.id"

async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path=EXEC)
        page = await b.new_page(viewport={"width": 1600, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.goto(URL, wait_until="networkidle")
        await page.fill('[data-testid="login-email-input"]', EMAIL)
        await page.fill('[data-testid="login-password-input"]', "demo12345")
        await page.click('[data-testid="login-submit-button"]')
        await page.wait_for_timeout(3000)
        try:
            await page.click('[data-testid="scope-pick-ent_ksc"]', timeout=3000)
            await page.wait_for_timeout(1000)
        except Exception:
            pass
        # expand all groups
        toggles = await page.locator('[data-testid^="nav-group-toggle-"]').evaluate_all("els => els.map(e => e.getAttribute('data-testid'))")
        for t in toggles:
            try:
                await page.click(f'[data-testid="{t}"]', timeout=2000)
                await page.wait_for_timeout(150)
            except Exception:
                pass
        ids = await page.locator('[data-testid^="nav-"]').evaluate_all(
            "els => els.map(e => e.getAttribute('data-testid'))")
        items = [i for i in ids if not i.startswith("nav-group")]
        print("ITEMS:", len(items))
        blanks = []
        for i in items:
            errors.clear()
            try:
                await page.click(f'[data-testid="{i}"]', timeout=4000, force=True)
                await page.wait_for_timeout(2200)
                txt = (await page.locator("body").inner_text()).strip()
                if len(txt) < 30 or errors:
                    blanks.append((i, len(txt), errors[:1]))
                    # recover by reloading
                    await page.goto(URL, wait_until="networkidle")
                    await page.wait_for_timeout(2500)
                    for t in toggles:
                        try:
                            await page.click(f'[data-testid="{t}"]', timeout=1500)
                        except Exception:
                            pass
            except Exception as e:
                blanks.append((i, -1, [str(e)[:100]]))
        print("BLANK/ERROR PAGES:")
        for bl in blanks:
            print(" ", bl)
        if not blanks:
            print("  (none)")
        await b.close()

asyncio.run(main())
