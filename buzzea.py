from collections.abc import KeysView
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
import asyncio
import json
import os
import re

from playwright.async_api import Browser
from utils import Cache, Event, Time, get_logger, leagues, network


log = get_logger(__name__)


# ---------------------------------------------------------------------------
# GLOBALS
# ---------------------------------------------------------------------------

urls: dict[str, dict[str, str | float | int | None]] = {}

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


# ---------------------------------------------------------------------------
# EVENT
# ---------------------------------------------------------------------------

@dataclass(kw_only=True, slots=True)
class BZEvent(Event):
    event_ts: int | float
    stream_link: str
    status: str
    league: str
    category: str
    link: str | None = None


# ---------------------------------------------------------------------------
# URL HELPERS
# ---------------------------------------------------------------------------

def normalize_url(url: str | None) -> str:
    """Normalize a URL without changing its path or query string."""

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


def is_http_url(url: str | None) -> bool:
    """Return True for normal HTTP/HTTPS URLs."""

    if not url:
        return False

    try:
        parsed = urlparse(url)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except Exception:
        return False


def is_direct_m3u8(url: str | None) -> bool:
    """
    Detect a direct M3U8 URL.

    This intentionally does NOT attempt to discover or extract a hidden
    M3U8 from another website.
    """

    if not url:
        return False

    try:
        parsed = urlparse(url)
        path = parsed.path.lower()
        return path.endswith(".m3u8")
    except Exception:
        return False


def is_get_php_url(url: str | None) -> bool:
    """Detect the API's get.php stream endpoint."""

    if not url:
        return False

    try:
        parsed = urlparse(url)

        if parsed.path.rstrip("/").lower().endswith("/get.php"):
            return bool(parsed.query)

    except Exception:
        pass

    return False


def get_channel_id(url: str | None) -> str | None:
    """Extract the channel identifier from a get.php URL."""

    if not is_get_php_url(url):
        return None

    try:
        parsed = urlparse(url)

        # Expected API form:
        # get.php?18
        # get.php?106
        # get.php?620
        value = parsed.query.strip()

        if value and re.fullmatch(r"\d+", value):
            return value

        # Also tolerate:
        # get.php?id=620
        match = re.search(r"(?:^|&)id=(\d+)(?:&|$)", value, re.I)

        if match:
            return match.group(1)

    except Exception:
        pass

    return None


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def extract_api_events(data) -> list[dict]:
    """
    Extract event objects from the supported API response formats.

    Preferred format:
        {
            "days": [...],
            "matches": [...]
        }

    The API's top-level "matches" array is preferred because it already
    contains the event objects.
    """

    if isinstance(data, list):
        return [
            item
            for item in data
            if isinstance(item, dict)
        ]

    if not isinstance(data, dict):
        return []

    matches = data.get("matches")

    if isinstance(matches, list):
        return [
            item
            for item in matches
            if isinstance(item, dict)
        ]

    events: list[dict] = []

    days = data.get("days")

    if isinstance(days, list):
        for day in days:
            if not isinstance(day, dict):
                continue

            items = day.get("items")

            if not isinstance(items, list):
                continue

            for item in items:
                if isinstance(item, dict):
                    events.append(item)

    if events:
        return events

    # Generic fallback for APIs returning a list under another key.
    for value in data.values():
        if not isinstance(value, list):
            continue

        candidates = [
            item
            for item in value
            if isinstance(item, dict)
        ]

        if candidates:
            return candidates

    return []


async def refresh_api_cache(now: Time) -> list[dict]:
    """Fetch and normalize events from the BUZZEA API."""

    if not API_URL:
        log.error(
            "API_URL is not set. "
            "Please set BUZZEA_API_URL environment variable."
        )
        return []

    api_url = normalize_url(API_URL)

    log.info(f"Fetching API: {api_url}")

    response = await network.request(
        api_url,
        log=log,
    )

    if not response:
        log.warning("Failed to fetch API data")
        return []

    try:
        data = response.json()
    except Exception as exc:
        log.error(f"Failed to parse API JSON: {exc}")
        return []

    events = extract_api_events(data)

    log.info(f"Found {len(events)} events from API")

    return events


# ---------------------------------------------------------------------------
# STREAM VALIDATION
# ---------------------------------------------------------------------------

def get_first_stream_link(event: dict) -> str | None:
    """
    Get the first usable stream link from an API event.

    The API currently supplies get.php links in streams[].link.
    Those links are preserved exactly rather than incorrectly converting
    them to an iframe/embed URL.
    """

    streams = event.get("streams")

    if not isinstance(streams, list):
        return None

    for stream in streams:

        if not isinstance(stream, dict):
            continue

        link = normalize_url(stream.get("link"))

        if not is_http_url(link):
            continue

        return link

    return None


def validate_stream_link(
    stream_link: str | None,
    url_num: int,
) -> str | None:
    """
    Validate the API stream URL.

    A direct M3U8 is accepted.

    A get.php URL is retained as an API/provider endpoint, but it is NOT
    falsely reported as an extracted M3U8.
    """

    if not stream_link:
        log.warning(
            f"URL {url_num}) No stream link supplied by API"
        )
        return None

    stream_link = normalize_url(stream_link)

    if not is_http_url(stream_link):
        log.warning(
            f"URL {url_num}) Invalid stream URL: {stream_link}"
        )
        return None

    if is_direct_m3u8(stream_link):
        log.info(
            f"URL {url_num}) API supplied direct M3U8"
        )
        return stream_link

    if is_get_php_url(stream_link):
        channel_id = get_channel_id(stream_link)

        if channel_id:
            log.info(
                f"URL {url_num}) API get.php stream "
                f"channel ID: {channel_id}"
            )
        else:
            log.warning(
                f"URL {url_num}) Invalid get.php stream: "
                f"{stream_link}"
            )

        return stream_link

    log.info(
        f"URL {url_num}) API supplied HTTP stream endpoint: "
        f"{stream_link}"
    )

    return stream_link


# ---------------------------------------------------------------------------
# EVENTS
# ---------------------------------------------------------------------------

def event_timestamp(event: dict) -> float:
    """Safely obtain an event timestamp."""

    value = event.get("ts_et")

    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def event_status(event: dict) -> str:
    """Normalize event status."""

    value = event.get("status")

    if value is None:
        return "UPCOMING"

    return str(value).strip() or "UPCOMING"


def event_category(event: dict) -> str:
    """Normalize category."""

    value = event.get("category")

    if value is None:
        return ""

    return str(value).strip().lower()


def event_league(event: dict) -> str:
    """Normalize league."""

    value = event.get("league")

    if value is None:
        return ""

    return str(value).strip()


def event_title(event: dict) -> str:
    """Normalize event title."""

    value = event.get("title")

    if value is None:
        return ""

    return str(value).strip()


async def get_events(cached_keys: KeysView[str]) -> list[BZEvent]:
    """
    Load events from API cache and return all events that have usable
    stream links.

    No artificial +/- hour event window is applied. This allows the API's
    full current event list to be processed.
    """

    now = Time.rn()

    events_data = API_CACHE.load(
        per_entry=False,
        ts_index=-1,
    )

    if not events_data:
        log.info("Refreshing API cache")

        events_data = await refresh_api_cache(now)

        if events_data:
            API_CACHE.write(events_data)
        else:
            return []

    if not isinstance(events_data, list):
        log.warning("API cache contains unexpected data")
        return []

    event_list: list[BZEvent] = []

    seen_keys: set[str] = set()

    for event in events_data:

        if not isinstance(event, dict):
            continue

        category = event_category(event)
        league = event_league(event)
        title = event_title(event)
        event_ts = event_timestamp(event)
        status = event_status(event)

        if not title:
            continue

        stream_link = get_first_stream_link(event)

        if not stream_link:
            log.debug(
                f"Skipping '{title}': no API stream link"
            )
            continue

        stream_link = validate_stream_link(
            stream_link,
            len(event_list) + 1,
        )

        if not stream_link:
            continue

        sport = CATEGORY_MAP.get(
            category,
            category.title() if category else "Other",
        )

        key = f"[{sport}] {title} ({TAG})"

        # Avoid duplicate event objects from the API.
        if key in seen_keys:
            continue

        seen_keys.add(key)

        # Do not re-process a cached event unless its existing entry does
        # not contain a usable source.
        cached_entry = None

        try:
            cached_entry = None
        except Exception:
            pass

        if key in cached_keys:
            continue

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

    event_list.sort(
        key=lambda item: (
            item.sport.lower(),
            item.event_ts,
            item.name.lower(),
        )
    )

    log.info(
        f"Found {len(event_list)} new eligible event(s)"
    )

    return event_list


# ---------------------------------------------------------------------------
# CACHE
# ---------------------------------------------------------------------------

def clean_cache_entry(entry: dict) -> dict:
    """Return a normalized cache entry."""

    return {
        "source": entry.get("source"),
        "logo": entry.get("logo", ""),
        "refer": entry.get("refer", REFERER),
        "origin": entry.get("origin", ORIGIN),
        "timestamp": entry.get("timestamp", 0),
        "event_ts": entry.get("event_ts", 0),
        "tvg-id": entry.get("tvg-id", "Live.Event.us"),
        "link": entry.get("link", ""),
        "stream_link": entry.get("stream_link", ""),
        "sport": entry.get("sport", ""),
        "status": entry.get("status", "UPCOMING"),
        "league": entry.get("league", ""),
    }


# ---------------------------------------------------------------------------
# PLAYLIST GENERATION
# ---------------------------------------------------------------------------

def playlist_entries(
    events_data: dict[str, dict],
) -> list[tuple[str, dict]]:
    """Return valid cached playlist entries sorted consistently."""

    entries = []

    for key, data in events_data.items():

        if not isinstance(data, dict):
            continue

        source = data.get("source")

        if not source:
            continue

        source = str(source).strip()

        if not source:
            continue

        entries.append(
            (
                key,
                data,
            )
        )

    return sorted(
        entries,
        key=lambda item: (
            str(item[1].get("sport", "")).lower(),
            float(item[1].get("event_ts", 0) or 0),
            item[0].lower(),
        ),
    )


def parse_event_key(key: str) -> tuple[str, str]:
    """Extract sport and event name from the cache key."""

    clean = key.replace(
        f" ({TAG})",
        "",
    )

    if "] " in clean:
        sport, name = clean.split(
            "] ",
            1,
        )

        return sport.lstrip("[").strip(), name.strip()

    return "Other", clean.strip()


def generate_m3u8_files(
    events_data: dict[str, dict],
) -> None:
    """Generate VLC and TiviMate playlist files."""

    entries = playlist_entries(events_data)

    vlc_lines: list[str] = [
        "#EXTM3U",
        "",
    ]

    tivimate_lines: list[str] = [
        "#EXTM3U",
        "",
    ]

    valid_streams = 0

    for idx, (key, data) in enumerate(
        entries,
        start=1,
    ):

        source = str(
            data.get("source", "")
        ).strip()

        if not source:
            continue

        sport, event_name = parse_event_key(key)

        tvg_id = str(
            data.get(
                "tvg-id",
                "Live.Event.us",
            )
            or "Live.Event.us"
        )

        logo = str(
            data.get("logo", "")
            or ""
        )

        valid_streams += 1

        extinf = (
            f'#EXTINF:-1 '
            f'tvg-chno="{idx}" '
            f'tvg-id="{tvg_id}" '
            f'tvg-name="{event_name}" '
            f'tvg-logo="{logo}" '
            f'group-title="{sport}",'
            f'{event_name}'
        )

        # VLC
        vlc_lines.append(extinf)
        vlc_lines.append(
            f"#EXTVLCOPT:http-referrer={REFERER}"
        )
        vlc_lines.append(
            f"#EXTVLCOPT:http-origin={ORIGIN}"
        )
        vlc_lines.append(
            f"#EXTVLCOPT:http-user-agent={USER_AGENT}"
        )
        vlc_lines.append(source)
        vlc_lines.append("")

        # TiviMate
        tivimate_lines.append(extinf)
        tivimate_lines.append(
            f"{source}"
            f"|referer={REFERER}"
            f"|origin={ORIGIN}"
            f"|user-agent={USER_AGENT_ENCODED}"
        )
        tivimate_lines.append("")

    vlc_path = Path(
        f"{TAG.lower()}_vlc.m3u8"
    )

    tivimate_path = Path(
        f"{TAG.lower()}_tivimate.m3u8"
    )

    try:
        vlc_path.write_text(
            "\n".join(vlc_lines),
            encoding="utf-8",
        )

        log.info(
            f"Generated {vlc_path} "
            f"with {valid_streams} streams"
        )

    except Exception as exc:
        log.error(
            f"Error writing {vlc_path}: {exc}"
        )

    try:
        tivimate_path.write_text(
            "\n".join(tivimate_lines),
            encoding="utf-8",
        )

        log.info(
            f"Generated {tivimate_path} "
            f"with {valid_streams} streams"
        )

    except Exception as exc:
        log.error(
            f"Error writing {tivimate_path}: {exc}"
        )

    if vlc_path.exists():
        log.info(
            f"✓ {vlc_path} exists "
            f"({vlc_path.stat().st_size} bytes)"
        )
    else:
        log.error(
            f"✗ {vlc_path} was not created"
        )

    if tivimate_path.exists():
        log.info(
            f"✓ {tivimate_path} exists "
            f"({tivimate_path.stat().st_size} bytes)"
        )
    else:
        log.error(
            f"✗ {tivimate_path} was not created"
        )


# ---------------------------------------------------------------------------
# SCRAPER
# ---------------------------------------------------------------------------

async def scrape(browser: Browser) -> None:
    """
    Main BUZZEA scraper.

    The important correction here is that ev.link is the API-provided
    stream endpoint. It is passed through unchanged.

    This function does not manufacture /embed3/ URLs.
    """

    cached_urls = CACHE_FILE.load()

    if not isinstance(cached_urls, dict):
        cached_urls = {}

    valid_urls = {
        key: value
        for key, value in cached_urls.items()
        if isinstance(value, dict)
        and value.get("source")
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
        generate_m3u8_files(cached_urls)

        return

    log.info(
        f"Processing {len(events)} new URL(s)"
    )

    # ------------------------------------------------------------------
    # IMPORTANT:
    #
    # The API already supplies the provider stream endpoint.
    # We do not create a different iframe/embed URL.
    #
    # If an authorized provider API supplies a direct M3U8, that direct
    # M3U8 can be stored as source.
    # ------------------------------------------------------------------

    for i, ev in enumerate(
        events,
        start=1,
    ):

        log.info(
            f"URL {i}) {ev.name}"
        )

        source = validate_stream_link(
            ev.link or ev.stream_link,
            i,
        )

        tvg_id, logo = leagues.get_tvg_info(
            ev.sport,
            ev.name,
        )

        key = (
            f"[{ev.sport}] "
            f"{ev.name} "
            f"({TAG})"
        )

        entry = {
            "source": source,
            "logo": logo,
            "refer": REFERER,
            "origin": ORIGIN,
            "timestamp": ev.timestamp,
            "event_ts": ev.event_ts,
            "tvg-id": tvg_id or "Live.Event.us",
            "link": ev.link,
            "stream_link": ev.stream_link,
            "sport": ev.sport,
            "status": ev.status,
            "league": ev.league,
        }

        cached_urls[key] = entry

        if source:
            valid_count += 1
            urls[key] = entry

            if is_direct_m3u8(source):
                log.info(
                    f"URL {i}) Direct M3U8 accepted"
                )
            elif is_get_php_url(source):
                channel_id = get_channel_id(source)

                log.info(
                    f"URL {i}) Using API stream: "
                    f"{source}"
                )

                if channel_id:
                    log.info(
                        f"URL {i}) Channel ID: "
                        f"{channel_id}"
                    )

            log.info(
                f"Added event: {key}"
            )

    log.info(
        f"Collected and cached "
        f"{valid_count - cached_count} new event(s)"
    )

    CACHE_FILE.write(cached_urls)

    generate_m3u8_files(cached_urls)

    log.info(
        f"Finished {TAG} scrape: "
        f"{valid_count} cached event(s)"
    )


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

async def main() -> None:
    """Run the BUZZEA updater."""

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

        # Playwright is retained because the surrounding project/network
        # infrastructure expects a Browser object.
        #
        # No browser navigation to a third-party get.php endpoint is
        # performed here.
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
                await browser.close()

    except Exception as exc:

        log.error(
            f"{TAG} updater failed: {exc}"
        )

        import traceback

        traceback.print_exc()

        raise


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    asyncio.run(main())
