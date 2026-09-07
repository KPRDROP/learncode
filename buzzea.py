from collections.abc import KeysView
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from urllib.parse import urlparse
import os
import asyncio
import re

from playwright.async_api import Browser, Error as PlaywrightError
from utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)


# ============================================================
# GLOBALS
# ============================================================

urls: dict[str, dict[str, str | float]] = {}

TAG = "BUZZEA"

CACHE_FILE = Cache(TAG, exp=5_400)

API_CACHE = Cache(f"{TAG}-api", exp=28_800)

# Use environment variable with fallback
BASE_URL = os.getenv("BUZZEA_BASE_URL")
API_URL = os.getenv("BUZZEA_API_URL")

# Constants for output files
REFERER = "https://exposestrat.st/"
ORIGIN = "https://exposestrat.st"

USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 10; K) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Mobile Safari/537.36"
)

USER_AGENT_ENCODED = (
    "Mozilla%2F5.0%20(Linux%3B%20Android%2010%3B%20K)%20"
    "AppleWebKit%2F537.36%20(KHTML%2C%20like%20Gecko)%20"
    "Chrome%2F120.0.0.0%20Mobile%20Safari%2F537.36"
)

# Category mapping for display
CATEGORY_MAP = {
    "football": "Football",
    "american-football": "American Football",
    "baseball": "Baseball",
    "basketball": "Basketball",
    "hockey": "Hockey",
    "motor-sports": "Motor Sports",
    "fight": "Fight",
    "soccer": "Soccer",
    "tennis": "Tennis",
    "cricket": "Cricket",
    "racing": "Racing",
    "combat": "Combat",
}


# ============================================================
# EVENT MODEL
# ============================================================

@dataclass(kw_only=True, slots=True)
class BZEvent(Event):
    event_ts: int | float
    stream_link: str
    status: str
    league: str
    category: str
    link: str | None = None


# ============================================================
# URL HELPERS
# ============================================================

def normalize_url(url: str | None) -> str:
    """Normalize a URL by adding https:// if needed."""

    if not url:
        return ""

    url = str(url).strip()

    if not url:
        return ""

    if url.startswith("//"):
        return f"https:{url}"

    if url.startswith(("http://", "https://")):
        return url

    return f"https://{url}"


def extract_channel_id(url: str | None) -> str | None:
    """Extract numeric channel ID from a get.php URL."""

    if not url:
        return None

    normalized = normalize_url(url)

    parsed = urlparse(normalized)
    query = parsed.query.strip()

    if re.fullmatch(r"\d+", query):
        return query

    match = re.search(
        r"/get\.php\?(\d+)",
        normalized,
        re.IGNORECASE,
    )

    if match:
        return match.group(1)

    return None


# ============================================================
# API PARSING
# ============================================================

def flatten_api_events(data) -> list[dict]:
    """
    Extract event dictionaries from the BUZZEA API response.

    Supports:
        [
            {...},
            {...}
        ]

    and:

        {
            "matches": [...]
        }

    and:

        {
            "days": [
                {
                    "items": [...]
                }
            ]
        }
    """

    if isinstance(data, list):
        return [
            item
            for item in data
            if isinstance(item, dict)
        ]

    if not isinstance(data, dict):
        return []

    # Preferred structure
    matches = data.get("matches")

    if isinstance(matches, list):
        result = [
            item
            for item in matches
            if isinstance(item, dict)
        ]

        if result:
            return result

    # Alternative structure
    days = data.get("days")

    if isinstance(days, list):
        result = []

        for day in days:
            if not isinstance(day, dict):
                continue

            items = day.get("items")

            if not isinstance(items, list):
                continue

            for item in items:
                if isinstance(item, dict):
                    result.append(item)

        if result:
            return result

    # Generic fallback
    for value in data.values():

        if isinstance(value, list):

            candidates = [
                item
                for item in value
                if isinstance(item, dict)
            ]

            if candidates:
                return candidates

    return []


def extract_stream_link(event: dict) -> str | None:
    """
    Extract the stream link from the API event.

    This intentionally preserves the API-provided link instead
    of constructing a different URL.
    """

    streams = event.get("streams")

    if not streams:
        return None

    candidates: list[str] = []

    if isinstance(streams, dict):

        link = streams.get("link")

        if isinstance(link, str):
            candidates.append(link)

        for value in streams.values():

            if isinstance(value, dict):

                nested_link = value.get("link")

                if isinstance(nested_link, str):
                    candidates.append(nested_link)

            elif isinstance(value, str):
                candidates.append(value)

    elif isinstance(streams, list):

        for stream in streams:

            if isinstance(stream, dict):

                link = stream.get("link")

                if isinstance(link, str):
                    candidates.append(link)

            elif isinstance(stream, str):
                candidates.append(stream)

    for candidate in candidates:

        if not candidate:
            continue

        candidate = candidate.strip()

        if "get.php" in candidate.lower():
            return normalize_url(candidate)

    return None


# ============================================================
# API CACHE
# ============================================================

async def refresh_api_cache(now: Time) -> list[dict]:
    """Fetch all events from the BUZZEA API endpoint."""

    if not API_URL:
        log.error(
            "API_URL is not set. "
            "Please set BUZZEA_API_URL environment variable."
        )
        return []

    log.info(f"Fetching API: {API_URL}")

    response = await network.request(
        API_URL,
        log=log,
    )

    if not response:
        log.warning("Failed to fetch API data")
        return []

    try:
        data = response.json()

    except Exception as exc:
        log.error(
            f"Failed to parse API response: {exc}"
        )
        return []

    events = flatten_api_events(data)

    log.info(
        f"Found {len(events)} events from API"
    )

    return events


def remove_cache_metadata(events_data) -> list[dict]:
    """Remove cache metadata records."""

    if not isinstance(events_data, list):
        return []

    return [
        item
        for item in events_data
        if isinstance(item, dict)
        and "timestamp" not in item
    ]


# ============================================================
# EVENT CACHE / EVENT DISCOVERY
# ============================================================

async def get_events(
    cached_keys: KeysView[str],
) -> list[BZEvent]:

    """Get all events from the API."""

    now = Time.rn()

    events_data = API_CACHE.load(
        per_entry=False,
        ts_index=-1,
    )

    if not events_data:

        log.info("Refreshing API cache")

        events_data = await refresh_api_cache(now)

        if events_data:

            cache_data = list(events_data)

            cache_data.append(
                {
                    "timestamp": now.timestamp()
                }
            )

            API_CACHE.write(cache_data)

        else:

            log.warning(
                "API returned no events"
            )

            return []

    events_data = remove_cache_metadata(
        events_data
    )

    event_list: list[BZEvent] = []

    seen_keys: set[str] = set()

    for event in events_data:

        if not isinstance(event, dict):
            continue

        category = str(
            event.get("category") or ""
        ).strip()

        league = str(
            event.get("league") or ""
        ).strip()

        title = str(
            event.get("title") or ""
        ).strip()

        event_time = event.get(
            "ts_et",
            0,
        )

        status = str(
            event.get("status") or "UPCOMING"
        ).strip()

        if not category or not title:
            continue

        try:
            event_ts = float(event_time)

        except (
            TypeError,
            ValueError,
        ):
            event_ts = 0

        if not event_ts:
            continue

        stream_link = extract_stream_link(
            event
        )

        if not stream_link:
            continue

        sport = CATEGORY_MAP.get(
            category.lower(),
            category.title(),
        )

        key = (
            f"[{sport}] "
            f"{title} "
            f"({TAG})"
        )

        if key in seen_keys:
            continue

        if key in cached_keys:
            continue

        seen_keys.add(key)

        event_list.append(
            BZEvent(
                sport=sport,
                name=title,
                link=stream_link,
                league=league,
                category=category,
                status=status,
                stream_link=stream_link,
                event_ts=event_ts,
                timestamp=now.timestamp(),
            )
        )

    log.info(
        f"Found {len(event_list)} "
        f"new eligible event(s)"
    )

    return event_list


# ============================================================
# SAFE PAGE CLEANUP
# ============================================================

async def safe_close_page(page, url_num: int) -> None:
    """
    Close a Playwright page without allowing a cleanup error
    to terminate the entire scraper.

    Playwright can report an exception while a route handler is
    still processing a Service Worker request. In that situation
    page.close() may raise even though the event itself was
    already processed successfully.
    """

    if page is None:
        return

    try:

        if page.is_closed():
            return

    except Exception:
        return

    try:

        await page.close()

    except PlaywrightError as exc:

        message = str(exc)

        if (
            "Service Worker requests do not have "
            "an associated frame"
            in message
        ):

            log.warning(
                f"URL {url_num}) Ignoring Playwright "
                f"Service Worker cleanup error"
            )

            return

        log.warning(
            f"URL {url_num}) Page cleanup warning: "
            f"{exc}"
        )

    except Exception as exc:

        log.warning(
            f"URL {url_num}) Unexpected page cleanup "
            f"warning: {exc}"
        )


# ============================================================
# STREAM PROCESSING
# ============================================================

async def process_event_with_network(
    browser: Browser,
    events: list[BZEvent],
    cached_urls: dict,
) -> tuple[dict, int]:

    """
    Process events using network.process_event.

    Important:
    We intentionally manage the page lifecycle ourselves
    instead of using:

        async with network.event_page(context) as page:

    because event_page() closes the page automatically and
    that cleanup can propagate a Service Worker route error
    after process_event() has already succeeded.
    """

    valid_count = 0

    async with network.event_context(
        browser
    ) as context:

        for i, ev in enumerate(
            events,
            start=1,
        ):

            log.info(
                f"URL {i}) {ev.name}"
            )

            page = None
            source = None

            try:

                # Create a normal page directly.
                page = await context.new_page()

                handler = partial(
                    network.process_event,
                    url=ev.link,
                    url_num=i,
                    page=page,
                    log=log,
                )

                source = await network.safe_process(
                    handler,
                    url_num=i,
                    semaphore=network.PW_S,
                    log=log,
                )

            except Exception as exc:

                log.error(
                    f"URL {i}) Processing failed: "
                    f"{exc}"
                )

            finally:

                # Never allow page cleanup to destroy a
                # successful scrape.
                await safe_close_page(
                    page,
                    i,
                )

            # ------------------------------------------------
            # Event metadata
            # ------------------------------------------------

            tvg_id, logo = leagues.get_tvg_info(
                ev.sport,
                ev.name,
            )

            key = (
                f"[{ev.sport}] "
                f"{ev.name} "
                f"({TAG})"
            )

            # Only save successfully processed events.
            if not source:

                log.warning(
                    f"URL {i}) No source returned; "
                    f"event not added"
                )

                continue

            entry = {
                "source": source,
                "logo": logo,
                "refer": REFERER,
                "event_ts": ev.event_ts,
                "timestamp": ev.timestamp,
                "tvg-id": tvg_id or "Live.Event.us",
                "stream_link": ev.stream_link,
                "sport": ev.sport,
                "status": ev.status,
            }

            cached_urls[key] = entry

            urls[key] = entry

            valid_count += 1

            log.info(
                f"Added event: {key}"
            )

    return cached_urls, valid_count


# ============================================================
# PLAYLIST GENERATION
# ============================================================

def generate_m3u8_files(
    events_data: dict[str, dict],
) -> None:

    """Generate VLC and TiviMate M3U8 files."""

    sorted_events = sorted(
        [
            (k, v)
            for k, v in events_data.items()
            if v.get("source")
        ],
        key=lambda x: (
            x[1].get("sport", ""),
            x[1].get("event_ts", 0),
        ),
    )

    vlc_lines: list[str] = []
    tivimate_lines: list[str] = []

    valid_streams = 0

    for idx, (key, data) in enumerate(
        sorted_events,
        start=1,
    ):

        source = data.get("source")

        if not source:
            continue

        valid_streams += 1

        # --------------------------------------------
        # Event information
        # --------------------------------------------

        key_clean = key.replace(
            f" ({TAG})",
            "",
        )

        sport_part = key_clean.split(
            "] ",
            1,
        )

        sport = sport_part[0].strip("[")

        event_name = (
            sport_part[1]
            if len(sport_part) > 1
            else key_clean
        )

        tvg_id = data.get(
            "tvg-id",
            "Live.Event.us",
        )

        logo = data.get(
            "logo",
            "",
        )

        stream_url = str(source)

        # --------------------------------------------
        # VLC
        # --------------------------------------------

        vlc_lines.append(
            f'#EXTINF:-1 '
            f'tvg-chno="{idx}" '
            f'tvg-id="{tvg_id}" '
            f'tvg-name="{event_name}" '
            f'tvg-logo="{logo}" '
            f'group-title="{sport}",'
            f'{event_name}'
        )

        vlc_lines.append(
            f'#EXTVLCOPT:http-referrer={REFERER}'
        )

        vlc_lines.append(
            f'#EXTVLCOPT:http-origin={ORIGIN}'
        )

        vlc_lines.append(
            f'#EXTVLCOPT:http-user-agent={USER_AGENT}'
        )

        vlc_lines.append(
            stream_url
        )

        vlc_lines.append("")

        # --------------------------------------------
        # TiviMate
        # --------------------------------------------

        tivimate_lines.append(
            f'#EXTINF:-1 '
            f'tvg-chno="{idx}" '
            f'tvg-id="{tvg_id}" '
            f'tvg-name="{event_name}" '
            f'tvg-logo="{logo}" '
            f'group-title="{sport}",'
            f'{event_name}'
        )

        tivimate_lines.append(
            f'{stream_url}'
            f'|referer={REFERER}'
            f'|origin={ORIGIN}'
            f'|user-agent={USER_AGENT_ENCODED}'
        )

        tivimate_lines.append("")

    # ========================================================
    # VLC OUTPUT
    # ========================================================

    vlc_output_path = Path(
        f"{TAG.lower()}_vlc.m3u8"
    )

    try:

        with vlc_output_path.open(
            "w",
            encoding="utf-8",
        ) as f:

            f.write("#EXTM3U\n")

            if vlc_lines:
                f.write(
                    "\n".join(vlc_lines)
                )

        log.info(
            f"Generated {vlc_output_path} "
            f"with {valid_streams} streams"
        )

    except Exception as exc:

        log.error(
            f"Error writing VLC M3U8 file: "
            f"{exc}"
        )

    # ========================================================
    # TIVIMATE OUTPUT
    # ========================================================

    tivimate_output_path = Path(
        f"{TAG.lower()}_tivimate.m3u8"
    )

    try:

        with tivimate_output_path.open(
            "w",
            encoding="utf-8",
        ) as f:

            f.write("#EXTM3U\n")

            if tivimate_lines:
                f.write(
                    "\n".join(tivimate_lines)
                )

        log.info(
            f"Generated {tivimate_output_path} "
            f"with {valid_streams} streams"
        )

    except Exception as exc:

        log.error(
            f"Error writing TiviMate M3U8 file: "
            f"{exc}"
        )

    # ========================================================
    # VERIFY OUTPUT
    # ========================================================

    if vlc_output_path.exists():

        log.info(
            f"✓ {vlc_output_path} exists "
            f"({vlc_output_path.stat().st_size} bytes)"
        )

    if tivimate_output_path.exists():

        log.info(
            f"✓ {tivimate_output_path} exists "
            f"({tivimate_output_path.stat().st_size} bytes)"
        )


# ============================================================
# MAIN SCRAPER
# ============================================================

async def scrape(browser: Browser) -> None:
    """Main scraping function."""

    cached_urls = CACHE_FILE.load()

    if not isinstance(
        cached_urls,
        dict,
    ):
        cached_urls = {}

    valid_urls = {
        k: v
        for k, v in cached_urls.items()
        if isinstance(v, dict)
        and v.get("source")
    }

    cached_count = len(valid_urls)

    urls.update(valid_urls)

    log.info(
        f"Loaded {cached_count} "
        f"event(s) from cache"
    )

    log.info(
        f'Scraping from "{BASE_URL}"'
    )

    events = await get_events(
        cached_urls.keys()
    )

    if events:

        log.info(
            f"Processing {len(events)} "
            f"new URL(s)"
        )

        cached_urls, new_count = (
            await process_event_with_network(
                browser,
                events,
                cached_urls,
            )
        )

        log.info(
            f"Collected and cached "
            f"{new_count} new event(s)"
        )

    else:

        log.info(
            "No new events found"
        )

    # --------------------------------------------------------
    # Always write cache and playlists, even when some events
    # fail.
    # --------------------------------------------------------

    CACHE_FILE.write(cached_urls)

    generate_m3u8_files(
        cached_urls
    )


# ============================================================
# MAIN
# ============================================================

async def main() -> None:
    """Main entry point."""

    try:

        log.info(
            f"Starting {TAG} updater..."
        )

        log.info(
            f"Using BASE_URL: {BASE_URL}"
        )

        log.info(
            f"Using API_URL: {API_URL}"
        )

        from playwright.async_api import async_playwright

        async with async_playwright() as p:

            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                ],
            )

            try:

                await scrape(browser)

                log.info(
                    f"{TAG} updater completed successfully"
                )

            finally:

                try:

                    await browser.close()

                except Exception as exc:

                    log.warning(
                        f"Browser cleanup warning: "
                        f"{exc}"
                    )

    except Exception as exc:

        log.error(
            f"{TAG} updater failed: {exc}"
        )

        import traceback

        traceback.print_exc()

        raise


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    asyncio.run(main())
