from collections.abc import KeysView
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import os
import asyncio
import re

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
USER_AGENT = "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
USER_AGENT_ENCODED = "Mozilla%2F5.0%20(Linux%3B%20Android%2010%3B%20K)%20AppleWebKit%2F537.36%20(KHTML%2C%20like%20Gecko)%20Chrome%2F120.0.0.0%20Mobile%20Safari%2F537.36"

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
    """
    Normalize a URL without changing its path.

    IMPORTANT:
    This function does NOT convert get.php URLs to iframe/embed URLs.
    """
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


def is_get_php_url(url: str | None) -> bool:
    """
    Validate the expected BUZZEA stream URL format.

    Expected:
        https://streamed.buzz/get.php?106

    Also accepts:
        https://example.com/get.php?106
    """
    if not url:
        return False

    url = normalize_url(url)

    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        return False

    if not parsed.netloc:
        return False

    if not parsed.path.lower().endswith("/get.php"):
        return False

    # Expected query format: ?106
    # parse_qs does not handle this as a normal key, so inspect it
    # directly from the original URL.
    query = parsed.query.strip()

    return bool(re.fullmatch(r"\d+", query))


def extract_channel_id(url: str | None) -> str | None:
    """
    Extract the numeric channel ID from:

        https://streamed.buzz/get.php?106
    """
    if not url:
        return None

    normalized = normalize_url(url)

    parsed = urlparse(normalized)
    query = parsed.query.strip()

    if re.fullmatch(r"\d+", query):
        return query

    # Fallback for unusual but still valid get.php URLs.
    match = re.search(r"/get\.php\?(\d+)", normalized, re.IGNORECASE)

    if match:
        return match.group(1)

    return None


def build_get_php_url(stream_link: str | None) -> str | None:
    """
    Return the correct get.php URL.

    The API-provided URL is authoritative.

    Example:
        https://streamed.buzz/get.php?106

    If only the channel ID is available and BASE_URL is configured,
    construct:

        https://{BASE_URL}/get.php?{ID}
    """
    if not stream_link:
        return None

    normalized = normalize_url(stream_link)

    # Best case: API already provides the correct URL.
    if is_get_php_url(normalized):
        return normalized

    channel_id = extract_channel_id(normalized)

    if channel_id and BASE_URL:
        base = BASE_URL.strip()

        if base.endswith("/"):
            base = base[:-1]

        if not base.startswith(("http://", "https://")):
            base = f"https://{base}"

        return f"{base}/get.php?{channel_id}"

    return None


# ============================================================
# API PARSING
# ============================================================

def flatten_api_events(data) -> list[dict]:
    """
    Extract event dictionaries from the BUZZEA API response.

    Supported formats:

    {
        "days": [
            {
                "items": [...]
            }
        ],
        "matches": [...]
    }

    or simply:

    [...]
    """
    if isinstance(data, list):
        return [
            item
            for item in data
            if isinstance(item, dict)
        ]

    if not isinstance(data, dict):
        return []

    # Prefer the top-level "matches" array when available.
    # It represents the complete event collection and avoids
    # processing the same events from both "days" and "matches".
    matches = data.get("matches")

    if isinstance(matches, list):
        result = [
            item
            for item in matches
            if isinstance(item, dict)
        ]

        if result:
            return result

    # Fallback: flatten days[].items.
    days = data.get("days")

    if isinstance(days, list):
        result: list[dict] = []

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

    # Last-resort generic list detection.
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
    Extract the get.php URL from an event's streams field.

    Handles both possible structures:

        "streams": {
            "hd": ...,
            "link": "https://streamed.buzz/get.php?106"
        }

    and:

        "streams": [
            {
                "hd": ...,
                "link": "https://streamed.buzz/get.php?106"
            }
        ]
    """
    streams = event.get("streams")

    if not streams:
        return None

    candidates: list[str] = []

    if isinstance(streams, dict):
        link = streams.get("link")

        if isinstance(link, str):
            candidates.append(link)

        # Some APIs may have quality-specific dictionaries.
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

    # Return the first valid get.php URL.
    for candidate in candidates:
        result = build_get_php_url(candidate)

        if result:
            return result

    return None


# ============================================================
# API CACHE
# ============================================================

async def refresh_api_cache(now: Time) -> list[dict]:
    """
    Fetch all events from the BUZZEA API endpoint.
    """
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
        log.error(f"Failed to parse API response: {exc}")
        return []

    events = flatten_api_events(data)

    log.info(f"Found {len(events)} events from API")

    return events


# ============================================================
# EVENT CACHE / EVENT DISCOVERY
# ============================================================

def get_cached_timestamp(events_data) -> float | None:
    """
    Get timestamp metadata from the API cache without modifying
    the original list.
    """
    if not isinstance(events_data, list):
        return None

    for item in reversed(events_data):
        if isinstance(item, dict) and "timestamp" in item:
            try:
                return float(item["timestamp"])
            except (TypeError, ValueError):
                return None

    return None


def remove_cache_metadata(events_data) -> list[dict]:
    """
    Return API events without cache metadata records.
    """
    if not isinstance(events_data, list):
        return []

    return [
        item
        for item in events_data
        if isinstance(item, dict) and "timestamp" not in item
    ]


async def get_events(cached_keys: KeysView[str]) -> list[BZEvent]:
    """
    Get ALL events from the API.

    Unlike the previous version, this function does not restrict
    events to a +/- time window.

    That means live, upcoming, and other events returned by the API
    can all be processed.
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
            cache_data = list(events_data)
            cache_data.append(
                {
                    "timestamp": now.timestamp()
                }
            )

            API_CACHE.write(cache_data)

        else:
            log.warning("API returned no events")
            return []

    events_data = remove_cache_metadata(events_data)

    event_list: list[BZEvent] = []

    seen_keys: set[str] = set()

    for event in events_data:
        if not isinstance(event, dict):
            continue

        event_id = event.get("id")

        category = str(
            event.get("category") or ""
        ).strip()

        league = str(
            event.get("league") or ""
        ).strip()

        title = str(
            event.get("title") or ""
        ).strip()

        event_time = event.get("ts_et", 0)

        status = str(
            event.get("status") or "UPCOMING"
        ).strip()

        if not category or not title:
            continue

        try:
            event_ts = float(event_time)
        except (TypeError, ValueError):
            event_ts = 0

        if not event_ts:
            log.debug(
                f"Skipping event without timestamp: {title}"
            )
            continue

        # Extract the actual get.php stream URL.
        stream_link = extract_stream_link(event)

        if not stream_link:
            log.warning(
                f"No valid get.php stream link: {title}"
            )
            continue

        # Make sure we never accidentally process an embed URL.
        if "/embed" in stream_link.lower():
            log.warning(
                f"Skipping invalid embed URL for {title}: "
                f"{stream_link}"
            )
            continue

        sport = CATEGORY_MAP.get(
            category.lower(),
            category.title(),
        )

        key = f"[{sport}] {title} ({TAG})"

        # Prevent duplicate events.
        if key in seen_keys:
            continue

        seen_keys.add(key)

        # Existing cache entry.
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

        log.debug(
            f"API event {event_id or '-'}: "
            f"{title} -> {stream_link}"
        )

    log.info(
        f"Found {len(event_list)} new eligible event(s)"
    )

    return event_list


# ============================================================
# STREAM PROCESSING
# ============================================================

async def process_event(
    stream_link: str,
    url_num: int,
) -> str | None:
    """
    Process the API stream link.

    IMPORTANT:
    BUZZEA's API provides get.php URLs. We keep that URL intact.

    We do NOT:
      - follow iframe URLs
      - convert get.php to embed3
      - scrape player HTML
      - attempt to bypass HTTP 403 protection
      - extract protected CDN URLs

    The returned URL is therefore the authorized API-provided
    get.php stream endpoint.
    """
    if not stream_link:
        log.warning(
            f"URL {url_num}) No stream link provided"
        )
        return None

    try:
        source = build_get_php_url(stream_link)

        if not source:
            log.warning(
                f"URL {url_num}) Invalid stream URL: "
                f"{stream_link}"
            )
            return None

        if not is_get_php_url(source):
            log.warning(
                f"URL {url_num}) URL is not a valid get.php "
                f"endpoint: {source}"
            )
            return None

        channel_id = extract_channel_id(source)

        log.info(
            f"URL {url_num}) Using API stream: {source}"
        )

        if channel_id:
            log.info(
                f"URL {url_num}) Channel ID: {channel_id}"
            )

        # Explicit protection against the old bug.
        if "/embed3/" in source.lower():
            log.error(
                f"URL {url_num}) Refusing invalid embed3 URL: "
                f"{source}"
            )
            return None

        return source

    except Exception as exc:
        log.warning(
            f"URL {url_num}) Error processing stream link: "
            f"{exc}"
        )
        return None


# ============================================================
# PLAYLIST GENERATION
# ============================================================

def generate_m3u8_files(
    events_data: dict[str, dict]
) -> None:
    """
    Generate:

        buzzea_vlc.m3u8
        buzzea_tivimate.m3u8
    """

    sorted_events = sorted(
        [
            (key, value)
            for key, value in events_data.items()
            if value.get("source")
        ],
        key=lambda item: (
            str(item[1].get("sport", "")),
            float(item[1].get("event_ts", 0) or 0),
            item[0],
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

        # Only put valid get.php sources into the playlists.
        if not is_get_php_url(str(source)):
            log.warning(
                f"Skipping invalid playlist source: {source}"
            )
            continue

        valid_streams += 1

        # ----------------------------------------------------
        # Event metadata
        # ----------------------------------------------------

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

        tvg_id = (
            data.get("tvg-id")
            or "Live.Event.us"
        )

        logo = data.get("logo") or ""

        stream_url = str(source)

        # ----------------------------------------------------
        # VLC
        # ----------------------------------------------------

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
            f"#EXTVLCOPT:http-referrer={REFERER}"
        )

        vlc_lines.append(
            f"#EXTVLCOPT:http-origin={ORIGIN}"
        )

        vlc_lines.append(
            f"#EXTVLCOPT:http-user-agent={USER_AGENT}"
        )

        vlc_lines.append(stream_url)
        vlc_lines.append("")

        # ----------------------------------------------------
        # TiviMate
        # ----------------------------------------------------

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
            f"{stream_url}"
            f"|referer={REFERER}"
            f"|origin={ORIGIN}"
            f"|user-agent={USER_AGENT_ENCODED}"
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
            newline="\n",
        ) as file:
            file.write("#EXTM3U\n")

            if vlc_lines:
                file.write(
                    "\n".join(vlc_lines)
                )

                if not vlc_lines[-1].endswith("\n"):
                    file.write("\n")

        log.info(
            f"Generated {vlc_output_path} "
            f"with {valid_streams} streams"
        )

    except Exception as exc:
        log.error(
            f"Error writing VLC M3U8 file: {exc}"
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
            newline="\n",
        ) as file:
            file.write("#EXTM3U\n")

            if tivimate_lines:
                file.write(
                    "\n".join(tivimate_lines)
                )

                if not tivimate_lines[-1].endswith("\n"):
                    file.write("\n")

        log.info(
            f"Generated {tivimate_output_path} "
            f"with {valid_streams} streams"
        )

    except Exception as exc:
        log.error(
            f"Error writing TiviMate M3U8 file: {exc}"
        )

    # ========================================================
    # VERIFY FILES
    # ========================================================

    if vlc_output_path.exists():
        log.info(
            f"✓ {vlc_output_path} exists "
            f"({vlc_output_path.stat().st_size} bytes)"
        )
    else:
        log.error(
            f"✗ {vlc_output_path} was not created!"
        )

    if tivimate_output_path.exists():
        log.info(
            f"✓ {tivimate_output_path} exists "
            f"({tivimate_output_path.stat().st_size} bytes)"
        )
    else:
        log.error(
            f"✗ {tivimate_output_path} was not created!"
        )


# ============================================================
# MAIN SCRAPER
# ============================================================

async def scrape() -> None:
    """
    Main BUZZEA updater.

    API -> get.php URL -> cache -> playlists
    """

    global urls

    cached_urls = CACHE_FILE.load()

    if not isinstance(cached_urls, dict):
        cached_urls = {}

    valid_urls = {
        key: value
        for key, value in cached_urls.items()
        if isinstance(value, dict)
        and value.get("source")
        and is_get_php_url(
            str(value.get("source"))
        )
    }

    urls.update(valid_urls)

    log.info(
        f"Loaded {len(valid_urls)} event(s) from cache"
    )

    if BASE_URL:
        log.info(
            f'Scraping from "{BASE_URL}"'
        )
    else:
        log.info(
            "BASE_URL is not configured; "
            "using API-provided stream hosts"
        )

    events = await get_events(
        cached_urls.keys()
    )

    if not events:
        log.info(
            "No new events found"
        )

        # Still regenerate playlists from the existing cache.
        generate_m3u8_files(cached_urls)
        CACHE_FILE.write(cached_urls)
        return

    log.info(
        f"Processing {len(events)} new URL(s)"
    )

    added_count = 0

    for index, event in enumerate(
        events,
        start=1,
    ):
        key = (
            f"[{event.sport}] "
            f"{event.name} "
            f"({TAG})"
        )

        log.info(
            f"URL {index}) "
            f"{event.name}"
        )

        # ----------------------------------------------------
        # Process the API get.php URL.
        # ----------------------------------------------------

        source = await process_event(
            event.stream_link,
            index,
        )

        # ----------------------------------------------------
        # TVG metadata
        # ----------------------------------------------------

        try:
            tvg_id, logo = leagues.get_tvg_info(
                event.sport,
                event.name,
            )
        except Exception as exc:
            log.warning(
                f"Could not get TVG info for "
                f"{event.name}: {exc}"
            )

            tvg_id = None
            logo = ""

        # ----------------------------------------------------
        # Cache entry
        # ----------------------------------------------------

        entry = {
            "source": source,
            "logo": logo or "",
            "refer": REFERER,
            "event_ts": event.event_ts,
            "timestamp": event.timestamp,
            "tvg-id": tvg_id or "Live.Event.us",
            "stream_link": event.stream_link,
            "sport": event.sport,
            "status": event.status,
            "league": event.league,
            "category": event.category,
        }

        cached_urls[key] = entry

        if source:
            urls[key] = entry
            added_count += 1

            log.info(
                f"Added event: {key}"
            )

    # --------------------------------------------------------
    # Save cache
    # --------------------------------------------------------

    CACHE_FILE.write(cached_urls)

    log.info(
        f"Collected and cached "
        f"{added_count} new event(s)"
    )

    # --------------------------------------------------------
    # Generate playlists
    # --------------------------------------------------------

    generate_m3u8_files(cached_urls)

    log.info(
        f"Finished {TAG} scrape: "
        f"{len(cached_urls)} cached event(s)"
    )


# ============================================================
# MAIN
# ============================================================

async def main() -> None:
    """
    Main entry point.
    """
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

        await scrape()

        log.info(
            f"{TAG} updater completed successfully"
        )

    except Exception as exc:
        log.error(
            f"{TAG} updater failed: {exc}"
        )

        import traceback
        traceback.print_exc()

        raise


if __name__ == "__main__":
    asyncio.run(main())
