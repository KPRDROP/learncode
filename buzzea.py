from collections.abc import KeysView
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urljoin
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
BASE_URL = os.getenv("BUZZEA_BASE_UR")
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
    """Extract the numeric channel ID from get.php URL."""
    if not url:
        return None

    normalized = normalize_url(url)
    parsed = urlparse(normalized)
    query = parsed.query.strip()

    if re.fullmatch(r"\d+", query):
        return query

    match = re.search(r"/get\.php\?(\d+)", normalized, re.IGNORECASE)
    if match:
        return match.group(1)

    return None


# ============================================================
# API PARSING
# ============================================================

def flatten_api_events(data) -> list[dict]:
    """Extract event dictionaries from the BUZZEA API response."""
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]

    if not isinstance(data, dict):
        return []

    matches = data.get("matches")
    if isinstance(matches, list):
        result = [item for item in matches if isinstance(item, dict)]
        if result:
            return result

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

    for value in data.values():
        if isinstance(value, list):
            candidates = [item for item in value if isinstance(item, dict)]
            if candidates:
                return candidates

    return []


def extract_stream_link(event: dict) -> str | None:
    """Extract the get.php URL from an event's streams field."""
    streams = event.get("streams")
    if not streams:
        return None

    candidates = []

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
        if candidate and "get.php" in candidate:
            return normalize_url(candidate)

    return None


# ============================================================
# API CACHE
# ============================================================

async def refresh_api_cache(now: Time) -> list[dict]:
    """Fetch all events from the BUZZEA API endpoint."""
    if not API_URL:
        log.error("API_URL is not set. Please set BUZZEA_API_URL environment variable.")
        return []

    log.info(f"Fetching API: {API_URL}")

    response = await network.request(API_URL, log=log)
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


def remove_cache_metadata(events_data) -> list[dict]:
    """Return API events without cache metadata records."""
    if not isinstance(events_data, list):
        return []
    return [item for item in events_data if isinstance(item, dict) and "timestamp" not in item]


# ============================================================
# EVENT CACHE / EVENT DISCOVERY
# ============================================================

async def get_events(cached_keys: KeysView[str]) -> list[BZEvent]:
    """Get ALL events from the API."""
    now = Time.rn()

    events_data = API_CACHE.load(per_entry=False, ts_index=-1)

    if not events_data:
        log.info("Refreshing API cache")
        events_data = await refresh_api_cache(now)

        if events_data:
            cache_data = list(events_data)
            cache_data.append({"timestamp": now.timestamp()})
            API_CACHE.write(cache_data)
        else:
            log.warning("API returned no events")
            return []

    events_data = remove_cache_metadata(events_data)

    event_list = []
    seen_keys = set()

    for event in events_data:
        if not isinstance(event, dict):
            continue

        category = str(event.get("category") or "").strip()
        league = str(event.get("league") or "").strip()
        title = str(event.get("title") or "").strip()
        event_time = event.get("ts_et", 0)
        status = str(event.get("status") or "UPCOMING").strip()

        if not category or not title:
            continue

        try:
            event_ts = float(event_time)
        except (TypeError, ValueError):
            event_ts = 0

        if not event_ts:
            continue

        stream_link = extract_stream_link(event)
        if not stream_link:
            continue

        sport = CATEGORY_MAP.get(category.lower(), category.title())
        key = f"[{sport}] {title} ({TAG})"

        if key in seen_keys or key in cached_keys:
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

    log.info(f"Found {len(event_list)} new eligible event(s)")
    return event_list


# ============================================================
# STREAM PROCESSING - FETCH get.php AND EXTRACT M3U8
# ============================================================

async def process_event(stream_link: str, url_num: int) -> str | None:
    """
    Process the get.php URL to extract the M3U8 URL.
    
    The get.php URL returns a page that either:
    1. Contains the M3U8 URL directly in the HTML
    2. Redirects to the M3U8 URL
    3. Contains the M3U8 URL in a script tag or iframe
    """
    try:
        if not stream_link:
            log.warning(f"URL {url_num}) No stream link provided")
            return None

        # Normalize the URL
        normalized_link = normalize_url(stream_link)
        log.info(f"URL {url_num}) Fetching: {normalized_link}")

        # Make HTTP request with proper headers
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Referer": "https://streamed.buzz/",
        }

        response = await network.request(normalized_link, url_num, headers=headers, log=log)
        if not response:
            log.warning(f"URL {url_num}) Failed to fetch page")
            return None

        content = response.text

        # Look for M3U8 URL in the page - this is the primary method
        m3u8_pattern = r'https?://[^\s"\']+\.m3u8[^\s"\']*'
        match = re.search(m3u8_pattern, content)

        if match:
            m3u8_url = match.group(0)
            log.info(f"URL {url_num}) Captured M3U8: {m3u8_url[:50]}...")
            return m3u8_url

        # Try to find in script tags
        script_pattern = r'<script[^>]*>.*?(https?://[^\s"\']+\.m3u8[^\s"\']*).*?</script>'
        script_match = re.search(script_pattern, content, re.DOTALL | re.IGNORECASE)
        if script_match:
            m3u8_url = script_match.group(1)
            log.info(f"URL {url_num}) Captured M3U8 from script")
            return m3u8_url

        # Try to find in iframes - but be careful not to follow to embed3
        iframe_pattern = r'<iframe[^>]+src=["\']([^"\']+)["\']'
        iframe_match = re.search(iframe_pattern, content)
        if iframe_match:
            iframe_url = normalize_url(iframe_match.group(1))
            # Only follow iframe if it's not the problematic embed3
            if "/embed3/" not in iframe_url.lower():
                log.info(f"URL {url_num}) Following iframe: {iframe_url}")
                iframe_response = await network.request(iframe_url, url_num, headers=headers, log=log)
                if iframe_response:
                    iframe_content = iframe_response.text
                    m3u8_match = re.search(m3u8_pattern, iframe_content)
                    if m3u8_match:
                        log.info(f"URL {url_num}) Captured M3U8 from iframe")
                        return m3u8_match.group(0)
            else:
                log.info(f"URL {url_num}) Skipping embed3 iframe")

        log.warning(f"URL {url_num}) No M3U8 found")
        return None

    except Exception as e:
        log.warning(f"URL {url_num}) Error processing: {e}")
        return None


# ============================================================
# PLAYLIST GENERATION
# ============================================================

def generate_m3u8_files(events_data: dict[str, dict]) -> None:
    """Generate VLC and TiviMate M3U8 files."""

    sorted_events = sorted(
        [(k, v) for k, v in events_data.items() if v.get("source")],
        key=lambda x: (x[1].get("sport", ""), x[1].get("event_ts", 0))
    )

    vlc_lines = []
    tivimate_lines = []
    valid_streams = 0

    for idx, (key, data) in enumerate(sorted_events, start=1):
        source = data.get("source")
        if not source:
            continue

        valid_streams += 1

        # Extract event info
        key_clean = key.replace(f" ({TAG})", "")
        sport_part = key_clean.split("] ", 1)
        sport = sport_part[0].strip("[")
        event_name = sport_part[1] if len(sport_part) > 1 else key_clean

        tvg_id = data.get("tvg-id", "Live.Event.us")
        logo = data.get("logo", "")
        stream_url = str(source)

        # VLC format
        vlc_lines.append(f'#EXTINF:-1 tvg-chno="{idx}" tvg-id="{tvg_id}" tvg-name="{event_name}" tvg-logo="{logo}" group-title="{sport}",{event_name}')
        vlc_lines.append(f'#EXTVLCOPT:http-referrer={REFERER}')
        vlc_lines.append(f'#EXTVLCOPT:http-origin={ORIGIN}')
        vlc_lines.append(f'#EXTVLCOPT:http-user-agent={USER_AGENT}')
        vlc_lines.append(stream_url)
        vlc_lines.append("")

        # TiviMate format
        tivimate_lines.append(f'#EXTINF:-1 tvg-chno="{idx}" tvg-id="{tvg_id}" tvg-name="{event_name}" tvg-logo="{logo}" group-title="{sport}",{event_name}')
        tivimate_lines.append(f'{stream_url}|referer={REFERER}|origin={ORIGIN}|user-agent={USER_AGENT_ENCODED}')
        tivimate_lines.append("")

    # Write VLC file
    vlc_output_path = Path(f"{TAG.lower()}_vlc.m3u8")
    try:
        with vlc_output_path.open("w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
            if vlc_lines:
                f.write("\n".join(vlc_lines))
        log.info(f"Generated {vlc_output_path} with {valid_streams} streams")
    except Exception as e:
        log.error(f"Error writing VLC M3U8 file: {e}")

    # Write TiviMate file
    tivimate_output_path = Path(f"{TAG.lower()}_tivimate.m3u8")
    try:
        with tivimate_output_path.open("w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
            if tivimate_lines:
                f.write("\n".join(tivimate_lines))
        log.info(f"Generated {tivimate_output_path} with {valid_streams} streams")
    except Exception as e:
        log.error(f"Error writing TiviMate M3U8 file: {e}")

    # Verify files
    if vlc_output_path.exists():
        log.info(f"✓ {vlc_output_path} exists ({vlc_output_path.stat().st_size} bytes)")
    if tivimate_output_path.exists():
        log.info(f"✓ {tivimate_output_path} exists ({tivimate_output_path.stat().st_size} bytes)")


# ============================================================
# MAIN SCRAPER
# ============================================================

async def scrape() -> None:
    """Main scraping function."""
    cached_urls = CACHE_FILE.load()

    valid_urls = {k: v for k, v in cached_urls.items() if v.get("source")}

    valid_count = cached_count = len(valid_urls)

    urls.update(valid_urls)

    log.info(f"Loaded {cached_count} event(s) from cache")

    log.info(f'Scraping from "{BASE_URL}"')

    if events := await get_events(cached_urls.keys()):
        log.info(f"Processing {len(events)} new URL(s)")

        for i, ev in enumerate(events, start=1):
            log.info(f"URL {i}) {ev.name}")

            # Process the stream link to get M3U8
            source = await process_event(ev.stream_link, i)

            tvg_id, logo = leagues.get_tvg_info(ev.sport, ev.name)

            key = f"[{ev.sport}] {ev.name} ({TAG})"

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

            if source:
                valid_count += 1
                urls[key] = entry
                log.info(f"Added event: {key}")

        log.info(f"Collected and cached {valid_count - cached_count} new event(s)")

    else:
        log.info("No new events found")

    CACHE_FILE.write(cached_urls)

    # Generate M3U8 files after updating cache
    generate_m3u8_files(cached_urls)


# ============================================================
# MAIN
# ============================================================

async def main() -> None:
    """Main entry point."""
    try:
        log.info(f"Starting {TAG} updater...")
        log.info(f"Using BASE_URL: {BASE_URL}")
        log.info(f"Using API_URL: {API_URL}")

        await scrape()
        log.info(f"{TAG} updater completed successfully")

    except Exception as e:
        log.error(f"{TAG} updater failed: {e}")
        import traceback
        traceback.print_exc()
        raise


if __name__ == "__main__":
    asyncio.run(main())
