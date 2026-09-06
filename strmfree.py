import os
import re
import asyncio
import json
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, quote

from playwright.async_api import Browser
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from utils import Cache, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "STFREE"

CACHE_FILE = Cache(TAG, exp=10_800)

API_CACHE = Cache(f"{TAG}-api", exp=19_800)

# Get API_URL from environment variable (secret) with validation
API_URL = os.environ.get("STRM_FREE_API_URL")
# Ensure URL has protocol
if API_URL and not API_URL.startswith(('http://', 'https://')):
    API_URL = f"https://{API_URL}"

# Constants
BASE_URL = "https://streamfree.top"
VLC_OUTPUT_FILE = "strmfree_vlc.m3u8"
TIVIMATE_OUTPUT_FILE = "strmfree_tivimate.m3u8"

# User Agent
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
USER_AGENT_ENCODED = quote(USER_AGENT, safe='')

# Category mapping for referer URLs
CATEGORY_IDS = {
    "Basketball": "basketball",
    "Soccer": "soccer", 
    "Football": "football",
    "Hockey": "hockey",
    "Baseball": "baseball",
    "Combat": "combat",
    "Racing": "racing",
    "Tennis": "tennis",
    "Cricket": "cricket"
}

# Sport name to ID mapping (for reverse lookup)
SPORT_TO_CATEGORY = {v: k for k, v in CATEGORY_IDS.items()}


async def get_event_data() -> dict[str, dict[str, Any]]:
    """Fetch event data from the API endpoint."""
    if not API_URL:
        log.error("STRM_FREE_API_URL environment variable not set")
        return {}

    try:
        response = await network.request(API_URL, log=log)
        if not response:
            return {}
        
        data = response.json()
        
        # Handle different response formats
        if isinstance(data, list):
            # If it's a list of events
            events = {}
            for event in data:
                event_name = event.get("name", "")
                sport = event.get("sport", "")
                category = CATEGORY_IDS.get(sport, sport.lower())
                
                # Create stream key from event name
                stream_key = re.sub(r'[^a-z0-9]+', '-', event_name.lower()).strip('-')
                
                key = f"[{sport}] {event_name} ({TAG})"
                events[key] = {
                    "sport": sport,
                    "name": event_name,
                    "category": category,
                    "stream_key": stream_key,
                    "thumbnail": event.get("thumbnail", ""),
                    "league": event.get("league", ""),
                    "timestamp": event.get("timestamp", Time.rn().timestamp()),
                }
            return events
        elif isinstance(data, dict):
            # If it's a dict with events
            events = {}
            for sport, sport_events in data.items():
                category = CATEGORY_IDS.get(sport, sport.lower())
                for event in sport_events:
                    event_name = event.get("name", "")
                    stream_key = re.sub(r'[^a-z0-9]+', '-', event_name.lower()).strip('-')
                    
                    key = f"[{sport}] {event_name} ({TAG})"
                    events[key] = {
                        "sport": sport,
                        "name": event_name,
                        "category": category,
                        "stream_key": stream_key,
                        "thumbnail": event.get("thumbnail", ""),
                        "league": event.get("league", ""),
                        "timestamp": event.get("timestamp", Time.rn().timestamp()),
                    }
            return events
        
        return {}
        
    except Exception as e:
        log.error(f"Error fetching event data: {e}")
        return {}


async def process_event(event_url: str, url_num: int, page) -> str | None:
    """Process a single event page to extract the M3U8 URL."""
    try:
        await page.goto(event_url, wait_until="networkidle", timeout=30000)
        
        # Wait for video element or source
        await page.wait_for_selector("video", timeout=10000)
        
        # Get the M3U8 URL from the video source
        m3u8_url = await page.evaluate("""
            () => {
                const video = document.querySelector('video');
                if (video) {
                    const src = video.src || video.currentSrc;
                    if (src && src.includes('.m3u8')) {
                        return src;
                    }
                }
                // Try to find in other elements
                const source = document.querySelector('source[src*=".m3u8"]');
                if (source) {
                    return source.src;
                }
                return null;
            }
        """)
        
        if m3u8_url:
            log.info(f"URL {url_num}) Captured M3U8")
            return m3u8_url
        
        log.warning(f"URL {url_num}) No M3U8 found")
        return None
        
    except Exception as e:
        log.warning(f"URL {url_num}) Error processing: {e}")
        return None


async def get_events(cached_keys: list[str]) -> list[dict[str, Any]]:
    """Get events from API or cache."""
    # Fixed: Changed from Time.clean(Time.now()) to Time.rn()
    now = Time.rn()

    # Fixed: Changed from API_CACHE.load(per_entry=False, index=-1) to ts_index=-1
    if not (events := API_CACHE.load(per_entry=False, ts_index=-1)):
        log.info("Refreshing event cache")
        
        events = await get_event_data()
        
        if events:
            # Add timestamp to events
            if isinstance(events, dict):
                # Create a timestamp entry
                events["_timestamp"] = now.timestamp()
            API_CACHE.write(events)
        else:
            return []

    # Process events
    event_list = []
    
    # Handle different event formats
    if isinstance(events, dict):
        # Remove timestamp entry
        events.pop("_timestamp", None)
        
        for key, event_data in events.items():
            if key in cached_keys:
                continue
                
            # Skip if missing required data
            if not event_data.get("stream_key"):
                continue
                
            # Check if event is within time window (30 minutes before/after)
            event_ts = event_data.get("timestamp", now.timestamp())
            if not (now.delta(minutes=-30).timestamp() <= event_ts <= now.delta(minutes=30).timestamp()):
                continue
            
            event_list.append({
                "key": key,
                **event_data
            })
    
    log.info(f"Found {len(event_list)} eligible event(s)")
    return event_list


def generate_m3u8_files(events_data: dict[str, dict[str, str | float]]) -> None:
    """Generate VLC and TiviMate M3U8 files from events data."""
    
    vlc_content = ['#EXTM3U']
    tivimate_content = ['#EXTM3U']
    
    channel_counter = 1
    
    for event_name, event_info in events_data.items():
        source_url = event_info.get("source")
        
        # Skip if no source URL
        if not source_url:
            continue
        
        # Get event details
        sport = event_info.get("sport", "Live Events")
        category = event_info.get("category", "basketball")
        stream_key = event_info.get("stream_key", "")
        tvg_id = event_info.get("tvg-id", "Live.Event.us")
        logo = event_info.get("logo", "")
        
        # Build referer URL: https://streamfree.top/player/{category}/{stream_key}
        referer_url = f"{BASE_URL}/player/{category}/{stream_key}"
        
        # VLC format
        vlc_entry = f'#EXTINF:-1 tvg-chno="{channel_counter}" tvg-id="{tvg_id}" tvg-name="{event_name}" tvg-logo="{logo}" group-title="{sport}",{event_name}'
        vlc_content.append(vlc_entry)
        vlc_content.append(f'#EXTVLCOPT:http-referrer={referer_url}')
        vlc_content.append(f'#EXTVLCOPT:http-origin={referer_url}')
        vlc_content.append(f'#EXTVLCOPT:http-user-agent={USER_AGENT}')
        vlc_content.append(source_url)
        
        # TiviMate format (pipe-separated with encoded user agent)
        tivimate_entry = f'#EXTINF:-1 tvg-chno="{channel_counter}" tvg-id="{tvg_id}" tvg-name="{event_name}" tvg-logo="{logo}" group-title="{sport}",{event_name}'
        tivimate_content.append(tivimate_entry)
        tivimate_content.append(f'{source_url}|referer={referer_url}|origin={referer_url}|user-agent={USER_AGENT_ENCODED}')
        
        channel_counter += 1
    
    # Write VLC file
    try:
        with open(VLC_OUTPUT_FILE, 'w', encoding='utf-8') as f:
            f.write('\n'.join(vlc_content))
        log.info(f"Generated VLC M3U8 file: {VLC_OUTPUT_FILE}")
    except Exception as e:
        log.error(f"Error writing VLC M3U8 file: {e}")
    
    # Write TiviMate file
    try:
        with open(TIVIMATE_OUTPUT_FILE, 'w', encoding='utf-8') as f:
            f.write('\n'.join(tivimate_content))
        log.info(f"Generated TiviMate M3U8 file: {TIVIMATE_OUTPUT_FILE}")
    except Exception as e:
        log.error(f"Error writing TiviMate M3U8 file: {e}")


async def scrape(browser: Browser) -> None:
    """Main scraping function."""
    cached_urls = CACHE_FILE.load()
    
    valid_urls = {k: v for k, v in cached_urls.items() if v.get("source")}
    
    valid_count = cached_count = len(valid_urls)
    
    urls.update(valid_urls)
    
    log.info(f"Loaded {cached_count} event(s) from cache")
    log.info(f'Scraping from "{API_URL}"')
    
    if events := await get_events(list(cached_urls.keys())):
        log.info(f"Processing {len(events)} new URL(s)")
        
        async with network.event_context(browser) as context:
            for i, ev in enumerate(events, start=1):
                # Build event URL
                event_url = f"{BASE_URL}/player/{ev['category']}/{ev['stream_key']}"
                
                async with network.event_page(context) as page:
                    source = await process_event(event_url, i, page)
                    
                    tvg_id, logo = leagues.get_tvg_info(ev['sport'], ev['name'])
                    
                    key = ev['key']
                    
                    entry = {
                        "source": source,
                        "logo": logo,
                        "refer": event_url,
                        "timestamp": ev.get("timestamp", Time.rn().timestamp()),
                        "tvg-id": tvg_id or "Live.Event.us",
                        "sport": ev['sport'],
                        "category": ev['category'],
                        "stream_key": ev['stream_key'],
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
    generate_m3u8_files(urls)


async def main() -> None:
    """Main entry point for the script."""
    try:
        log.info(f"Starting {TAG} updater")
        log.info(f"Using API URL: {API_URL}")
        
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


def run():
    """Run the async main function."""
    asyncio.run(main())


if __name__ == "__main__":
    run()
