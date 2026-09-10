import os
import re
import asyncio
import json
from dataclasses import dataclass
from functools import partial
from typing import Any
from urllib.parse import urljoin, quote, urlencode

import httpx

from playwright.async_api import Browser
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from utils import Cache, Event, Time, get_logger, leagues, network

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


@dataclass(kw_only=True, slots=True)
class STFEvent(Event):
    link: str | None = None
    logo: str | None = None
    category: str
    stream_key: str


async def process_event(
    stream_key: str,
    category: str,
    url_num: int,
) -> str | None:
    """Process a single event to extract the M3U8 URL."""
    
    # Step 1: Check stream availability
    if not (
        quality_data := await network.request(
            urljoin(BASE_URL, f"api/stream-status/{stream_key}"),
            url_num,
            log=log,
        )
    ):
        return

    quality_info: dict[str, str | Any] = quality_data.json()

    if not quality_info.get("available"):
        log.warning(f"URL {url_num}) Stream is unavailable.")
        return

    elif not (sources := quality_info.get("sources")):
        log.warning(f"URL {url_num}) No Sources found.")
        return

    # Step 2: Get available qualities
    quality_sources = {
        f"{quality}{source_num}": value
        for source_num, source_data in sources.items()
        for quality, value in source_data["qualities"].items()
    }

    available_quals: list[tuple[str, str]] = sorted(
        [qual.split("p") for qual, flag in quality_sources.items() if flag],
        key=lambda x: int(x[-1]),
    )

    if not available_quals:
        log.warning(f"URL {url_num}) No available qualities found.")
        return

    qual, num = f"{available_quals[0][0]}p", available_quals[0][-1]
    num = "" if num == "1" else num

    # Step 3: Get server name
    server_name = "cdn"
    if server_info := await network.request(
        urljoin(BASE_URL, f"get-stream-key/{stream_key}"),
        url_num,
        log=log,
    ):
        server_name = server_info.json().get("server_name", "cdn")

    # Step 4: Get stream data
    if not (
        stream_data := await network.request(
            urljoin(BASE_URL, f"embed/{category}/{stream_key}{num}"),
            url_num,
            params={"quality": qual, "category": category},
            timeout=httpx.Timeout(25.0),
            log=log,
        )
    ):
        return

    # Step 5: Extract M3U8 info
    ptrn = re.compile(r"_0x\s+=\s+(.*?);", re.S)

    if not (match := ptrn.search(stream_data.text)):
        log.warning(f"URL {url_num}) Unable to find stream information.")
        return

    m3u_info: dict[str, dict[str, Any]] = json.loads(match[1])[qual]
    query = urlencode(m3u_info)

    log.info(f"URL {url_num}) Captured M3U8")

    return urljoin(
        BASE_URL,
        f"live-{server_name}/{stream_key}{qual}{num}/index.m3u8?{query}",
    )


async def get_events(cached_keys: list[str]) -> list[STFEvent]:
    """Get events from API or cache."""
    now = Time.rn()

    events: list[STFEvent] = []

    # Load from cache
    if not (api_data := API_CACHE.load(per_entry=False)):
        log.info("Refreshing API cache")

        api_data = {"timestamp": now.timestamp()}

        if r := await network.request(
            urljoin(BASE_URL, "api/v1/streams"),
            log=log,
        ):
            api_data = r.json()
            api_data["timestamp"] = now.timestamp()

        API_CACHE.write(api_data)

    # Event window: 3 hours before now
    start_ts = now.delta(hours=-3).timestamp()
    now_ts = now.timestamp()

    for stream_info in api_data.get("streams", []):
        if not all(
            values := [
                stream_info.get(x)
                for x in (
                    "league",
                    "category",
                    "name",
                    "match_timestamp",
                    "stream_key",
                )
            ]
        ):
            continue

        sport, category, name, event_time, stream_key = values

        key = f"[{sport}] {name} ({TAG})"
        
        if key in cached_keys:
            continue

        # Check if event is within time window (3 hours before now)
        if not start_ts <= (event_time + 1800) <= now_ts:
            continue

        events.append(
            STFEvent(
                sport=sport,
                name=name,
                category=category,
                stream_key=quote(stream_key),
                logo=stream_info.get("thumbnail_url"),
                timestamp=now_ts,
            )
        )

    log.info(f"Found {len(events)} eligible event(s)")
    return events


def generate_m3u8_files(events_data: dict[str, dict[str, str | float]]) -> None:
    """Generate VLC and TiviMate M3U8 files from events data."""
    
    # Filter events with source
    valid_events = {k: v for k, v in events_data.items() if v.get("source")}
    
    if not valid_events:
        log.warning("No valid events with sources to generate M3U8 files")
        # Create empty files
        try:
            with open(VLC_OUTPUT_FILE, 'w', encoding='utf-8') as f:
                f.write('#EXTM3U\n')
            log.info(f"Generated empty VLC M3U8 file: {VLC_OUTPUT_FILE}")
        except Exception as e:
            log.error(f"Error writing VLC M3U8 file: {e}")
        
        try:
            with open(TIVIMATE_OUTPUT_FILE, 'w', encoding='utf-8') as f:
                f.write('#EXTM3U\n')
            log.info(f"Generated empty TiviMate M3U8 file: {TIVIMATE_OUTPUT_FILE}")
        except Exception as e:
            log.error(f"Error writing TiviMate M3U8 file: {e}")
        return
    
    vlc_content = ['#EXTM3U']
    tivimate_content = ['#EXTM3U']
    
    channel_counter = 1
    
    for event_name, event_info in valid_events.items():
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


async def scrape() -> None:
    """Main scraping function."""
    cached_urls = CACHE_FILE.load()

    valid_urls = {k: v for k, v in cached_urls.items() if v.get("source")}

    valid_count = cached_count = len(valid_urls)

    urls.update(valid_urls)

    log.info(f"Loaded {cached_count} event(s) from cache")

    log.info(f'Scraping from "{BASE_URL}"')

    if events := await get_events(list(cached_urls.keys())):
        log.info(f"Processing {len(events)} new URL(s)")

        for i, ev in enumerate(events, start=1):
            handler = partial(
                process_event,
                stream_key=ev.stream_key,
                category=ev.category,
                url_num=i,
            )

            source = await network.safe_process(
                handler,
                url_num=i,
                semaphore=network.HTTP_S,
                log=log,
            )

            key = f"[{ev.sport}] {ev.name} ({TAG})"

            tvg_id, logo = leagues.get_tvg_info(ev.sport, ev.name)

            entry = {
                "source": source,
                "logo": ev.logo or logo,
                "refer": f"{BASE_URL}/player/{ev.category}/{ev.stream_key}",
                "timestamp": ev.timestamp,
                "tvg-id": tvg_id or "Live.Event.us",
                "sport": ev.sport,
                "category": ev.category,
                "stream_key": ev.stream_key,
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
        await scrape()
        log.info(f"{TAG} updater completed successfully")
                
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
