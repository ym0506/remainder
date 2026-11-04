"""Drive the running app through every step and save the screenshots used in the README.

Needs the server running (`uvicorn app:app --port 8765`) with real sandbox and Claude keys,
and Playwright with the locally installed Google Chrome (no browser download):

    .venv/bin/python scripts/screenshots.py [--url http://localhost:8765]

Writes docs/img/*.png. Each run creates two sandbox seasons and makes a few Claude calls.
"""

import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parent.parent / "docs" / "img"
LONG = 180_000  # sandbox steps can take a minute or more


def shoot(page, selector, name):
    page.locator(selector).screenshot(path=str(OUT / name))
    print("saved", name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8765")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        page = browser.new_page(viewport={"width": 1180, "height": 900}, device_scale_factor=2)
        page.goto(args.url)
        # The sticky summary bar would otherwise cover the top of whichever step is captured.
        page.add_style_tag(content=".summary { position: static; }")

        page.click("#b-season")
        page.wait_for_selector("#s-notice:not([hidden])", timeout=LONG)

        page.click("#b-read")
        page.wait_for_selector("#reading:not([hidden])", timeout=LONG)
        # Now and then Claude raises a question about the demo notice; clear it as a treasurer would.
        while page.locator("#reading .problems button").count():
            page.locator("#reading .problems button").first.click()
        shoot(page, "#s-notice", "01-notice.png")

        page.click("#b-replies")
        page.wait_for_selector("#replies-source:not([hidden])", timeout=LONG)
        page.click("#write-reply summary")
        page.click(".presets button[data-family=Haddad]")
        page.click("#b-add-reply")
        page.wait_for_function("document.querySelectorAll('#inbox .mail.added .kind').length === 1", timeout=LONG)
        page.locator("#replies-problems button").first.click()
        page.evaluate("document.querySelector('#write-reply').open = false")
        shoot(page, "#s-replies", "02-replies.png")

        page.click("#b-plan")
        page.wait_for_selector("#plan:not([hidden])", timeout=LONG)
        page.check("#show-math")
        shoot(page, "#summary", "03-summary.png")
        shoot(page, "#s-plan", "04-plan.png")

        page.click("#b-send")
        page.wait_for_selector("#crash-banner:not([hidden])", timeout=LONG)
        page.uncheck("#show-math")
        shoot(page, "#s-send", "05-crash.png")
        page.click("#b-resume")
        page.wait_for_selector("#s-check:not([hidden])", timeout=LONG)
        page.locator("#plan tbody tr", has_text="PayPal already had this refund").screenshot(path=str(OUT / "06-not-sent-again.png"))
        print("saved 06-not-sent-again.png")

        page.click("#b-check")
        page.wait_for_selector("#check:not([hidden])", timeout=LONG)
        page.click("#b-compare")
        page.wait_for_selector("#compare:not([hidden])", timeout=LONG * 2)
        shoot(page, "#s-check", "07-check.png")
        shoot(page, "#s-compare", "08-compare.png")
        browser.close()


if __name__ == "__main__":
    main()
