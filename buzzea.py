from collections.abc import KeysView
from dataclasses import dataclass
from functools import partial
from urllib.parse import urljoin
from pathlib import Path
import os
import asyncio
import re

from playwright.async_api import Browser
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "BUZZEA"

CACHE_FILE = Cache(TAG, exp=5_400)

API_CACHE = Cache(f"{TAG}-api", exp=28_800)

# Use environment variable with fallback
BASE_URL = os.getenv("BUZZEA_BASE_URL", "https://streamed.buzz/")
API_URL = os.getenv("BUZZEA_API_URL", "https://streamed.buzz/api.php")

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


@dataclass(kw_only=True, slots=True)
class BZEvent(Event):
    event_ts: int | float
    stream_link: str
    status: str
    league: str
    category: str
    link: str | None = None


def normalize_url(url: str) -> str:
    """Ensure URL has proper protocol."""
    if not url:
        return url
    
    # If URL starts with //, add https:
    if url.startswith('//'):
        return f'https:{url}'
    
    # If URL doesn't have protocol, add https://
    if not url.startswith(('http://', 'https://')):
        return f'https://{url}'
    
    return url


async def refresh_api_cache(now: Time) -> list[dict]:
    """Fetch events from the API endpoint."""
    events = []

    if not API_URL:
        log.error("API_URL is not set. Please set BUZZEA_API_URL environment variable.")
        return events

    if not (response := await network.request(API_URL, log=log)):
        log.warning("Failed to fetch API data")
        return events

    try:
        data = response.json()
    except Exception as e:
        log.error(f"Failed to parse API response: {e}")
        return events

    # Handle different response formats
    if isinstance(data, dict):
        # Check for 'matches' key (from the JSON example)
        if "matches" in data:
            events = data["matches"]
        elif "days" in data:
            # Flatten days into items
            for day in data.get("days", []):
                events.extend(day.get("items", []))
        else:
            # Try to find any list in the response
            for key, value in data.items():
                if isinstance(value, list) and value and isinstance(value[0], dict):
                    events = value
                    break
    
    elif isinstance(data, list):
        events = data

    log.info(f"Found {len(events)} events from API")
    return events


async def process_event(stream_link: str, url_num: int) -> str | None:
    """Process a single event to extract the M3U8 URL."""
    try:
        if not stream_link:
            log.warning(f"URL {url_num}) No stream link provided")
            return None

        # Normalize the URL
        normalized_link = normalize_url(stream_link)
        log.info(f"URL {url_num}) Fetching: {normalized_link}")

        # Add headers to mimic a browser request
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }

        if not (response := await network.request(normalized_link, url_num, headers=headers, log=log)):
            return None

        # Look for M3U8 URL in the response
        content = response.text
        
        # Try to find M3U8 URL in the page
        m3u8_pattern = r'https?://[^\s"\']+\.m3u8[^\s"\']*'
        match = re.search(m3u8_pattern, content)
        
        if match:
            m3u8_url = match.group(0)
            log.info(f"URL {url_num}) Captured M3U8")
            return m3u8_url
        
        # Try to find in script tags or iframe
        iframe_pattern = r'<iframe[^>]+src=["\']([^"\']+)["\']'
        iframe_match = re.search(iframe_pattern, content)
        if iframe_match:
            iframe_url = normalize_url(iframe_match.group(1))
            log.info(f"URL {url_num}) Following iframe: {iframe_url}")
            # Follow iframe
            if iframe_response := await network.request(iframe_url, url_num, headers=headers, log=log):
                iframe_content = iframe_response.text
                m3u8_match = re.search(m3u8_pattern, iframe_content)
                if m3u8_match:
                    log.info(f"URL {url_num}) Captured M3U8 from iframe")
                    return m3u8_match.group(0)

        log.warning(f"URL {url_num}) No M3U8 found")
        return None

    except Exception as e:
        log.warning(f"URL {url_num}) Error processing: {e}")
        return None


async def get_events(cached_keys: KeysView[str]) -> list[BZEvent]:
    """Get events from API or cache."""
    now = Time.rn()

    # Load from cache
    if not (events_data := API_CACHE.load(per_entry=False, ts_index=-1)):
        log.info("Refreshing API cache")
        
        events_data = await refresh_api_cache(now)
        
        if events_data:
            # Add timestamp to cache
            if isinstance(events_data, list):
                events_data.append({"timestamp": now.timestamp()})
            API_CACHE.write(events_data)
        else:
            return []

    # Process events
    event_list = []
    
    # Get timestamp from cache if available
    if isinstance(events_data, list) and events_data:
        # Remove timestamp entry if present
        if events_data and isinstance(events_data[-1], dict) and "timestamp" in events_data[-1]:
            events_data.pop()

    # Time window: 6 hours before to 2 hours after
    start_ts = now.delta(hours=-6).timestamp()
    end_ts = now.delta(hours=2).timestamp()

    for event in events_data:
        if not isinstance(event, dict):
            continue

        # Get event fields
        category = event.get("category", "")
        league = event.get("league", "")
        title = event.get("title", "")
        event_time = event.get("ts_et", 0)
        status = event.get("status", "UPCOMING")
        streams = event.get("streams", [])

        # Skip if missing required fields
        if not all([category, title, event_time]):
            continue

        # Get first stream link
        if not streams or not streams[0].get("link"):
            continue
        
        stream_link = streams[0]["link"]

        # Format sport name
        sport = CATEGORY_MAP.get(category, category.title())

        key = f"[{sport}] {title} ({TAG})"

        # Skip if already cached
        if key in cached_keys:
            continue

        # Check if event is within time window
        if not start_ts <= event_time <= end_ts:
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
                event_ts=event_time,
                timestamp=now.timestamp(),
            )
        )

    log.info(f"Found {len(event_list)} eligible event(s)")
    return event_list


def generate_m3u8_files(events_data: dict[str, dict]) -> None:
    """Generate VLC and TiviMate M3U8 files from event data"""
    
    # Sort events by sport and time for better organization
    sorted_events = sorted(
        [(k, v) for k, v in events_data.items() if v.get("source")],
        key=lambda x: (x[1].get("sport", ""), x[1].get("event_ts", 0))
    )
    
    vlc_lines = []
    tivimate_lines = []
    valid_streams = 0
    
    for idx, (key, data) in enumerate(sorted_events, start=1):
        if not data.get("source"):
            continue
            
        valid_streams += 1
        
        # Extract event info from key
        key_clean = key.replace(f" ({TAG})", "")
        sport_part = key_clean.split("] ", 1)
        sport = sport_part[0].strip("[")
        event_name = sport_part[1] if len(sport_part) > 1 else key_clean
        
        tvg_id = data.get("tvg-id", "Live.Event.us")
        logo = data.get("logo", "")
        stream_url = data["source"]
        
        # VLC format
        vlc_lines.append(f'#EXTINF:-1 tvg-chno="{idx}" tvg-id="{tvg_id}" tvg-name="{event_name}" tvg-logo="{logo}" group-title="{sport}",{event_name}')
        vlc_lines.append(f'#EXTVLCOPT:http-referrer={REFERER}')
        vlc_lines.append(f'#EXTVLCOPT:http-origin={ORIGIN}')
        vlc_lines.append(f'#EXTVLCOPT:http-user-agent={USER_AGENT}')
        vlc_lines.append(stream_url)
        vlc_lines.append("")  # Empty line for separation
        
        # TiviMate format (pipe-separated with encoded user agent)
        tivimate_lines.append(f'#EXTINF:-1 tvg-chno="{idx}" tvg-id="{tvg_id}" tvg-name="{event_name}" tvg-logo="{logo}" group-title="{sport}",{event_name}')
        tivimate_line = f"{stream_url}|referer={REFERER}|origin={ORIGIN}|user-agent={USER_AGENT_ENCODED}"
        tivimate_lines.append(tivimate_line)
        tivimate_lines.append("")  # Empty line for separation
    
    # Write VLC file
    try:
        vlc_output_path = Path(f"{TAG.lower()}_vlc.m3u8")
        with open(vlc_output_path, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
            f.write("\n".join(vlc_lines))
        log.info(f"Generated {vlc_output_path} with {valid_streams} streams")
    except Exception as e:
        log.error(f"Error writing VLC M3U8 file: {e}")
    
    # Write TiviMate file
    try:
        tivimate_output_path = Path(f"{TAG.lower()}_tivimate.m3u8")
        with open(tivimate_output_path, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
            f.write("\n".join(tivimate_lines))
        log.info(f"Generated {tivimate_output_path} with {valid_streams} streams")
    except Exception as e:
        log.error(f"Error writing TiviMate M3U8 file: {e}")
    
    # Verify files were created
    if vlc_output_path.exists():
        log.info(f"✓ {vlc_output_path} exists ({vlc_output_path.stat().st_size} bytes)")
    else:
        log.error(f"✗ {vlc_output_path} was not created!")
        
    if tivimate_output_path.exists():
        log.info(f"✓ {tivimate_output_path} exists ({tivimate_output_path.stat().st_size} bytes)")
    else:
        log.error(f"✗ {tivimate_output_path} was not created!")


async def scrape(browser: Browser) -> None:
    """Main scraping function."""
    cached_urls = CACHE_FILE.load()

    valid_urls = {k: v for k, v in cached_urls.items() if v.get("source")}

    valid_count = cached_count = len(valid_urls)

    urls.update(valid_urls)

    log.info(f"Loaded {cached_count} event(s) from cache")

    log.info(f'Scraping from "{BASE_URL}"')

    if events := await get_events(cached_urls.keys()):
        log.info(f"Processing {len(events)} new URL(s)")

        async with network.event_context(browser) as context:
            for i, ev in enumerate(events, start=1):
                async with network.event_page(context) as page:
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


async def main() -> None:
    """Main function to run the updater"""
    try:
        log.info(f"Starting {TAG} updater...")
        log.info(f"Using BASE_URL: {BASE_URL}")
        log.info(f"Using API_URL: {API_URL}")
        
        # Initialize playwright and run scraper
        from playwright.async_api import async_playwright
        
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-setuid-sandbox']
            )
            try:
                await scrape(browser)
                log.info(f"{TAG} updater completed successfully")
            finally:
                await browser.close()
                
    except Exception as e:
        log.error(f"{TAG} updater failed: {e}")
        import traceback
        traceback.print_exc()
        raise


if __name__ == "__main__":
    asyncio.run(main())
