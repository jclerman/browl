"""Take the manual's screenshots (docs/manual/screenshots/) from a running browl with sign-in off.

Uploads the demo ontology, `docs/manual/screenshots/pets.ttl`, with ELK, then captures the pages the manual shows.
Run it against a throwaway stack, never one serving a real ontology, since it replaces the ontology:

    BROWL_PORT=127.0.0.1:8765 docker compose -p browl-manual -f docker-compose.yml -f override.yml up -d --build
    poetry run python scripts/manual_screenshots.py http://localhost:8765

where `override.yml` sets the app's `BASE_URL: http://localhost:8765/` and `AUTH_DISABLED: "true"`. Remove the stack
afterwards with `docker compose -p browl-manual down -v`.
"""

import argparse
import logging
import time
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import Page, ViewportSize, expect, sync_playwright

logger = logging.getLogger(__name__)

SCREENSHOTS = Path(__file__).resolve().parent.parent / "docs" / "manual" / "screenshots"
ONTOLOGY = SCREENSHOTS / "pets.ttl"
NAMESPACE = "https://example.org/pets/"
VIEWPORT: ViewportSize = {"width": 1280, "height": 800}
LOAD_TIMEOUT_MS = 180_000
HOME = "/example.org/pets"
TREE_OPEN = ("LivingThing", "Animal", "Pet", "Vertebrate", "Mammal")
"""The classes open in the tree's screenshot: pet shows the class the reasoner placed under it."""


def shoot(page: Page, name: str, full_page: bool = False) -> None:
    """Save a screenshot of the page as `name`.png."""
    path = SCREENSHOTS / f"{name}.png"
    page.screenshot(path=path, full_page=full_page)
    logger.info("Saved %s", path)


def wait_until_served(page: Page, url: str) -> None:
    """Wait for the load just started to take the site down (503), then for `url` to be served again."""
    deadline = time.monotonic() + LOAD_TIMEOUT_MS / 1000
    went_down = False
    while time.monotonic() < deadline:
        status = page.request.get(url).status
        went_down = went_down or status == 503
        if went_down and status == 200:
            return
        page.wait_for_timeout(100)
    raise TimeoutError(f"{url} wasn't served again within {LOAD_TIMEOUT_MS} ms of the upload")


def entity_path(local_name: str) -> str:
    """Return the proxied URL path of a demo ontology entity."""
    return "/example.org/pets/" + local_name


def main() -> None:
    """Upload the demo ontology and take the screenshots."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("base_url", help="the throwaway browl's address, e.g. http://localhost:8765")
    base = parser.parse_args().base_url.rstrip("/")
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport=VIEWPORT, color_scheme="light")

        page.goto(f"{base}/_browl/admin/upload")
        page.get_by_label("ELK").check()
        page.set_input_files("input[type=file]", ONTOLOGY)
        shoot(page, "admin-upload")
        page.get_by_role("button", name="Upload").click()
        wait_until_served(page, base + HOME)
        page.goto(f"{base}/_browl/admin/status")
        expect(page.get_by_text("Published").first).to_be_visible()
        shoot(page, "admin-status")

        page.goto(base + HOME)
        shoot(page, "home")

        page.goto(base + entity_path("Dog"))
        shoot(page, "entity-overview")

        page.goto(base + entity_path("Person") + "?tab=usage")
        page.wait_for_load_state("networkidle")
        shoot(page, "entity-usage")

        page.goto(base + entity_path("rex"))
        shoot(page, "individual")

        opened = "&".join("open=" + quote(NAMESPACE + name, safe="") for name in TREE_OPEN)
        page.goto(f"{base}/_browl/tree/classes?{opened}")
        shoot(page, "tree")

        page.goto(base + entity_path("Cat"))
        box = page.locator("#site-search-box")
        box.click()
        box.press_sequentially("do", delay=100)
        expect(page.get_by_role("listbox", name="Suggestions")).to_be_visible()
        shoot(page, "search-suggestions")

        page.goto(f"{base}/_browl/search?q=dog")
        shoot(page, "search-results")

        browser.close()


if __name__ == "__main__":
    main()
