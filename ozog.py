```python
import asyncio
import os
import re
from collections.abc import KeysView
from functools import partial
from urllib.parse import quote, urljoin

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TAG = "OVOGOZO"

CACHE_FILE = Cache(TAG, exp=28_800)

BASE_URL = "https://ovostream.net/"

REFERER = "https://gozowatch.top/"
ORIGIN = "https://gozowatch.top"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/134.0.0.0 Safari/537.36 Edg/134.0.0.0"
)

DEFAULT_LOGO = (
    "https://i.gyazo.com/"
    "4a5e9fa2525808ee4b65002b56d3450e.png"
)


# ---------------------------------------------------------------------------
# Browser state
# ---------------------------------------------------------------------------

_playwright = None
_browser: Browser | None = None
_context: BrowserContext | None = None


# ---------------------------------------------------------------------------
# Event-name helpers
# ---------------------------------------------------------------------------

def fix_event_name(name: str) -> str:
    """Normalize event-name capitalization."""

    name = re.sub(r"\s+VS\s+", " vs ", name, flags=re.IGNORECASE)

    words = []

    for word in name.split():
        if word.lower() == "vs":
            words.append("vs")
        elif len(word) <= 4 and word.isupper():
            words.append(word.upper())
        else:
            words.append(word.capitalize())

    return " ".join(words)


def normalize_source(source: str | None) -> str | None:
    """Clean and validate a stream URL."""

    if not source:
        return None

    source = source.strip().strip("\"'")
    source = source.rstrip("/")

    if not source:
        return None

    if not re.match(r"^https?://", source, re.IGNORECASE):
        return None

    return source


# ---------------------------------------------------------------------------
# Browser initialization
# ---------------------------------------------------------------------------

async def start_browser() -> None:
    """
    Start a normal Chromium browser.

    This deliberately does not attempt to bypass Cloudflare challenges or
    manipulate Cloudflare clearance/fingerprinting mechanisms.
    """

    global _playwright, _browser, _context

    if _browser is not None:
        return

    log.info("Starting Chromium browser")

    _playwright = await async_playwright().start()

    _browser = await _playwright.chromium.launch(
        headless=True,
        args=[
            "--disable-dev-shm-usage",
            "--no-sandbox",
        ],
    )

    _context = await _browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1920, "height": 1080},
        locale="en-US",
        timezone_id="America/New_York",
        color_scheme="dark",
        java_script_enabled=True,
    )

    await _context.set_extra_http_headers(
        {
            "Accept-Language": "en-US,en;q=0.9",
        }
    )


async def stop_browser() -> None:
    """Close browser resources."""

    global _playwright, _browser, _context

    try:
        if _context is not None:
            await _context.close()
    except Exception:
        pass

    try:
        if _browser is not None:
            await _browser.close()
    except Exception:
        pass

    try:
        if _playwright is not None:
            await _playwright.stop()
    except Exception:
        pass

    _context = None
    _browser = None
    _playwright = None


# ---------------------------------------------------------------------------
# Cloudflare detection
# ---------------------------------------------------------------------------

def looks_like_cloudflare_challenge(page_content: str, title: str = "") -> bool:
    """
    Detect a Cloudflare challenge/block page.

    This is detection only; it does not attempt to bypass the challenge.
    """

    text = f"{title}\n{page_content}".lower()

    indicators = (
        "just a moment",
        "checking your browser",
        "verify you are human",
        "cf-chl-",
        "challenge-platform",
        "attention required",
        "cloudflare",
    )

    return any(item in text for item in indicators)


# ---------------------------------------------------------------------------
# Browser page fetch
# ---------------------------------------------------------------------------

async def fetch_page(
    url: str,
    *,
    wait_until: str = "domcontentloaded",
    wait_ms: int = 1500,
) -> tuple[str | None, Page | None]:
    """
    Navigate to a page using a normal Chromium browser.

    Returns:
        (HTML, Page)

    A Cloudflare challenge is detected and reported rather than bypassed.
    """

    if _context is None:
        await start_browser()

    assert _context is not None

    page = await _context.new_page()

    try:
        log.info(f'Browser navigation: "{url}"')

        response = await page.goto(
            url,
            wait_until=wait_until,
            timeout=45_000,
        )

        status = response.status if response else 0

        log.info(f"Browser response status: {status}")

        # Give normal client-side JavaScript a chance to render.
        if wait_ms:
            await page.wait_for_timeout(wait_ms)

        title = await page.title()

        html = await page.content()

        if status == 403:
            log.error(
                f'Browser received HTTP 403 from "{url}".'
            )

        if looks_like_cloudflare_challenge(html, title):
            log.error(
                f'Cloudflare challenge/block detected for "{url}". '
                "The scraper will not attempt to bypass it."
            )
            await page.close()
            return None, None

        if status < 200 or status >= 400:
            log.error(
                f'Failed to fetch "{url}" - Status: {status}'
            )
            await page.close()
            return None, None

        return html, page

    except PlaywrightTimeoutError:
        log.error(f'Timeout while loading "{url}"')
        await page.close()
        return None, None

    except Exception as e:
        log.error(f'Browser error while loading "{url}": {e}')
        await page.close()
        return None, None


# ---------------------------------------------------------------------------
# Event discovery
# ---------------------------------------------------------------------------

async def get_events(cached_keys: KeysView[str]) -> list[Event]:
    events: list[Event] = []

    log.info(f'Fetching events from "{BASE_URL}"')

    page_content, page = await fetch_page(BASE_URL)

    if not page_content:
        log.error(f'Failed to fetch "{BASE_URL}"')
        return events

    try:
        soup = HTMLParser(page_content)

        cards = soup.css(".card")

        log.info(f"Found {len(cards)} card(s) on homepage")

        for card in cards:

            data_search = card.attributes.get("data-search")

            if not data_search:
                continue

            sport_elem = card.css_first(".sport-tag")

            if not sport_elem:
                continue

            sport = sport_elem.text(strip=True).capitalize()

            if sport == "Sports":
                sport = "Live Event"

            event_name = fix_event_name(data_search)

            watch_btn = card.css_first("a.watch-btn")

            if not watch_btn:
                continue

            href = watch_btn.attributes.get("href")

            if not href:
                continue

            # The supplied HTML shows that these are ?game=... links.
            event_url = urljoin(BASE_URL, href)

            key = f"[{sport}] {event_name} ({TAG})"

            if key in cached_keys:
                continue

            events.append(
                Event(
                    sport=sport,
                    name=event_name,
                    link=event_url,
                )
            )

            log.debug(
                f"Discovered event: {event_name} -> {event_url}"
            )

    finally:
        if page is not None:
            await page.close()

    log.info(f"Found {len(events)} new event(s)")

    return events


# ---------------------------------------------------------------------------
# Extract stream URL from browser page
# ---------------------------------------------------------------------------

async def extract_stream_url(page: Page) -> str | None:
    """
    Inspect the rendered watch page for an exposed media URL.

    This supports ordinary page content/network-visible URLs. It does not
    attempt to solve Cloudflare challenges or obtain protected clearance
    tokens.
    """

    # ---------------------------------------------------------------
    # 1. Look at common media elements.
    # ---------------------------------------------------------------

    selectors = (
        "video",
        "video source",
        "audio",
        "audio source",
        "iframe",
    )

    for selector in selectors:

        try:
            elements = await page.locator(selector).all()

            for element in elements:

                for attribute in ("src", "data-src", "data-url"):

                    value = await element.get_attribute(attribute)

                    value = normalize_source(value)

                    if value:
                        log.info(
                            f"Found media URL from {selector}: {value}"
                        )
                        return value

        except Exception:
            continue

    # ---------------------------------------------------------------
    # 2. Inspect rendered HTML for ordinary media URLs.
    # ---------------------------------------------------------------

    try:
        html = await page.content()

        patterns = (
            r'https?://[^"\'<>\s]+\.m3u8(?:\?[^"\'<>\s]*)?',
            r'https?://[^"\'<>\s]+\.mpd(?:\?[^"\'<>\s]*)?',
        )

        for pattern in patterns:

            match = re.search(
                pattern,
                html,
                flags=re.IGNORECASE,
            )

            if match:

                value = normalize_source(match.group(0))

                if value:
                    log.info(
                        f"Found media URL in rendered page: {value}"
                    )
                    return value

    except Exception:
        pass

    return None


# ---------------------------------------------------------------------------
# Process event
# ---------------------------------------------------------------------------

async def process_event(
    url: str,
    url_num: int,
) -> tuple[str | None, str | None]:

    nones = None, None

    log.info(
        f"URL {url_num}) Processing event page: {url}"
    )

    page_content, page = await fetch_page(
        url,
        wait_until="domcontentloaded",
        wait_ms=2500,
    )

    if not page_content or page is None:
        log.error(
            f"URL {url_num}) Failed to load event page"
        )
        return nones

    try:

        # -----------------------------------------------------------
        # Capture network media requests generated by the page.
        # -----------------------------------------------------------

        captured_urls: list[str] = []

        def on_request(request) -> None:
            request_url = request.url

            lower = request_url.lower()

            if (
                ".m3u8" in lower
                or ".mpd" in lower
            ):
                if request_url not in captured_urls:
                    captured_urls.append(request_url)

        page.on("request", on_request)

        # Allow the watch page to finish its normal initialization.
        await page.wait_for_timeout(3000)

        # -----------------------------------------------------------
        # First inspect rendered media elements.
        # -----------------------------------------------------------

        stream_url = await extract_stream_url(page)

        if stream_url:
            log.info(
                f"URL {url_num}) Captured stream source: "
                f"{stream_url}"
            )
            return stream_url, url

        # -----------------------------------------------------------
        # Then inspect URLs generated by normal browser requests.
        # -----------------------------------------------------------

        for request_url in captured_urls:

            stream_url = normalize_source(request_url)

            if stream_url:
                log.info(
                    f"URL {url_num}) Captured network media URL: "
                    f"{stream_url}"
                )
                return stream_url, url

        log.warning(
            f"URL {url_num}) No exposed media URL found"
        )

        return nones

    finally:
        await page.close()


# ---------------------------------------------------------------------------
# Scrape
# ---------------------------------------------------------------------------

async def scrape() -> None:

    cached_urls = CACHE_FILE.load()

    valid_urls = {
        k: v
        for k, v in cached_urls.items()
        if v.get("source")
    }

    valid_count = len(valid_urls)
    cached_count = len(valid_urls)

    urls.clear()
    urls.update(valid_urls)

    log.info(
        f"Loaded {cached_count} event(s) from cache"
    )

    log.info(
        f'Scraping from "{BASE_URL}"'
    )

    events = await get_events(
        cached_urls.keys()
    )

    if not events:

        log.info("No new events found")

        CACHE_FILE.write(cached_urls)

        return

    log.info(
        f"Processing {len(events)} new URL(s)"
    )

    now = Time.rn()

    for i, ev in enumerate(events, start=1):

        handler = partial(
            process_event,
            url=ev.link,
            url_num=i,
        )

        source, referer = await network.safe_process(
            handler,
            url_num=i,
            timeout_return=(None, None),
            semaphore=network.HTTP_S,
            log=log,
        )

        key = (
            f"[{ev.sport}] "
            f"{ev.name} "
            f"({TAG})"
        )

        tvg_id, logo = leagues.get_tvg_info(
            ev.sport,
            ev.name,
        )

        entry = {
            "source": source,
            "logo": logo or DEFAULT_LOGO,
            "refer": referer or ev.link or REFERER,
            "origin": ORIGIN,
            "timestamp": now.timestamp(),
            "tvg-id": tvg_id or "Live.Event.us",
            "link": ev.link,
        }

        cached_urls[key] = entry

        if source:

            valid_count += 1

            urls[key] = entry

            log.info(
                f"URL {i}) Saved event: {key}"
            )

        else:

            log.warning(
                f"No stream source for: {key}"
            )

    log.info(
        f"Collected and cached "
        f"{valid_count - cached_count} new event(s)"
    )

    CACHE_FILE.write(cached_urls)


# ---------------------------------------------------------------------------
# VLC playlist
# ---------------------------------------------------------------------------

def generate_vlc_m3u8() -> str:

    content = "#EXTM3U\n"

    playlist_index = 0

    for title, data in urls.items():

        source = data.get("source")

        if not source:
            continue

        playlist_index += 1

        tvg_id = data.get(
            "tvg-id",
            "Live.Event.us",
        )

        logo = data.get(
            "logo",
            DEFAULT_LOGO,
        )

        referer = data.get(
            "refer",
            REFERER,
        )

        origin = data.get(
            "origin",
            ORIGIN,
        )

        content += (
            f'#EXTINF:-1 '
            f'tvg-chno="{playlist_index}" '
            f'tvg-id="{tvg_id}" '
            f'tvg-name="{title}" '
            f'tvg-logo="{logo}" '
            f'group-title="Live Events",'
            f'{title}\n'
        )

        content += (
            f"#EXTVLCOPT:http-referrer={referer}\n"
        )

        content += (
            f"#EXTVLCOPT:http-origin={origin}\n"
        )

        content += (
            f"#EXTVLCOPT:http-user-agent={USER_AGENT}\n"
        )

        content += f"{source}\n"

    return content


# ---------------------------------------------------------------------------
# TiviMate playlist
# ---------------------------------------------------------------------------

def generate_tivimate_m3u8() -> str:

    content = "#EXTM3U\n"

    encoded_user_agent = quote(
        USER_AGENT,
        safe="",
    )

    playlist_index = 0

    for title, data in urls.items():

        source = data.get("source")

        if not source:
            continue

        playlist_index += 1

        tvg_id = data.get(
            "tvg-id",
            "Live.Event.us",
        )

        logo = data.get(
            "logo",
            DEFAULT_LOGO,
        )

        referer = data.get(
            "refer",
            REFERER,
        )

        origin = data.get(
            "origin",
            ORIGIN,
        )

        content += (
            f'#EXTINF:-1 '
            f'tvg-chno="{playlist_index}" '
            f'tvg-id="{tvg_id}" '
            f'tvg-name="{title}" '
            f'tvg-logo="{logo}" '
            f'group-title="Live Events",'
            f'{title}\n'
        )

        content += (
            f"{source}"
            f"|referer={referer}"
            f"|origin={origin}"
            f"|user-agent={encoded_user_agent}\n"
        )

    return content


# ---------------------------------------------------------------------------
# Write output files
# ---------------------------------------------------------------------------

def write_output_files() -> None:

    output_dir = os.getenv(
        "OUTPUT_DIR",
        ".",
    )

    os.makedirs(
        output_dir,
        exist_ok=True,
    )

    vlc_file = os.path.join(
        output_dir,
        "ozog_vlc.m3u8",
    )

    with open(
        vlc_file,
        "w",
        encoding="utf-8",
        newline="\n",
    ) as f:

        f.write(
            generate_vlc_m3u8()
        )

    tivimate_file = os.path.join(
        output_dir,
        "ozog_tivimate.m3u8",
    )

    with open(
        tivimate_file,
        "w",
        encoding="utf-8",
        newline="\n",
    ) as f:

        f.write(
            generate_tivimate_m3u8()
        )

    log.info(
        f"Generated VLC playlist: {vlc_file}"
    )

    log.info(
        f"Generated TiviMate playlist: {tivimate_file}"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:

    log.info(
        "Starting OVOGOZO updater"
    )

    try:

        await start_browser()

        await scrape()

        if urls:

            write_output_files()

            log.info(
                f"Successfully processed "
                f"{len(urls)} event(s)"
            )

        else:

            log.warning(
                "No events found to write to output files"
            )

    finally:

        await stop_browser()


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    asyncio.run(main())
```
