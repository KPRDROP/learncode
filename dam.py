from collections.abc import KeysView
from dataclasses import dataclass
from functools import partial
from urllib.parse import urljoin, quote
from typing import Any
import os
import asyncio

from utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "DAM"

CACHE_FILE = Cache(TAG, exp=10_800)

API_FILE = Cache(f"{TAG}-api", exp=28_800)

# Use environment variable or fallback to default
BASE_URL = os.getenv("DAM_BASE_URL")


@dataclass(kw_only=True, slots=True)
class DAMIEvent(Event):
    stream_id: str
    link: str | None = None
    logo: str | None = None


async def process_event(stream_id: str, url_num: int) -> str | None:
    if not (
        event_data := await network.request(
            urljoin(BASE_URL, f"papi/extract-url/{stream_id}"),
            url_num,
            log=log,
        )
    ):
        return

    elif not (api_data := event_data.json()).get("success"):
        log.warning(f"URL {url_num}) Unsuccessful Request: {api_data.get('error')}")
        return

    if not (m3u8 := api_data.get("hlsUrl", api_data.get("sdUrl"))):
        log.warning(f"URL {url_num}) No source found.")
        return

    log.info(f"URL {url_num}) Captured M3U8")

    return m3u8


async def get_events(cached_urls: dict[str, dict[str, str | float]]) -> list[DAMIEvent]:
    now = Time.rn()

    events: list[DAMIEvent] = []

    # Load API cache
    api_data = API_FILE.load(per_entry=False)
    
    # If cache is empty or not valid, refresh it
    if not api_data or not isinstance(api_data, list):
        log.info("Refreshing API cache")

        if r := await network.request(
            urljoin(BASE_URL, "papi/api/streams"),
            log=log,
        ):
            api_data = r.json()
            
            # Handle different response formats
            if isinstance(api_data, dict):
                # If it has a 'streams' key, use that
                if "streams" in api_data:
                    api_data = api_data.get("streams", [])
                else:
                    # If it's a dict with other data, wrap it
                    api_data = [api_data]
            
            # Ensure it's a list
            if not isinstance(api_data, list):
                log.error(f"Unexpected API response format: {type(api_data)}")
                return events
            
            # Add timestamp to the list
            if api_data:
                # If last item is a dict, add timestamp to it
                if isinstance(api_data[-1], dict):
                    api_data[-1]["timestamp"] = now.timestamp()
                else:
                    api_data.append({"timestamp": now.timestamp()})
            else:
                api_data = [{"timestamp": now.timestamp()}]

        API_FILE.write(api_data)

    # Ensure api_data is a list
    if not isinstance(api_data, list):
        log.error(f"API data is not a list: {type(api_data)}")
        return events

    # Use the original 30-minute window from working code
    start_dt = now.delta(minutes=-30)
    end_dt = now.delta(minutes=30)

    log.info(
        "Event window: %s -> %s (30 minutes before/after)",
        start_dt,
        end_dt,
    )

    # Process events from the API data
    for event in api_data:
        # Skip timestamp entries
        if isinstance(event, dict) and "timestamp" in event and len(event) == 1:
            continue
            
        # Get event data - handle different field names
        name = event.get("name") or event.get("title")
        sport = event.get("league") or event.get("sport")
        start_ts = event.get("starts_at") or event.get("date") or event.get("start")
        stream_id = event.get("id") or event.get("stream_id")
        
        # Skip if missing required fields
        if not all([name, sport, start_ts, stream_id]):
            continue

        stream_id = str(stream_id)

        # Skip unwanted streams
        if stream_id.lower().startswith("dl-"):
            continue

        if stream_id.startswith("247") or (sport and "24/7" in str(sport).lower()):
            continue

        try:
            # Convert timestamp - handle different formats
            if isinstance(start_ts, (int, float)):
                # If timestamp is in milliseconds (13 digits), convert to seconds
                if len(str(int(start_ts))) > 10:
                    start_ts = int(start_ts) / 1000
                event_dt = Time.from_ts(start_ts)
            elif isinstance(start_ts, str):
                # Try to parse as string
                try:
                    # Try as numeric string
                    num_ts = float(start_ts)
                    if len(str(int(num_ts))) > 10:
                        num_ts = num_ts / 1000
                    event_dt = Time.from_ts(num_ts)
                except (ValueError, TypeError):
                    # Try as date string
                    event_dt = Time.from_str(start_ts)
            else:
                log.warning(f"Unsupported timestamp format for {name}: {type(start_ts)}")
                continue
                
        except (TypeError, ValueError, OverflowError) as e:
            log.warning(
                "Invalid timestamp for %s: %r (error: %s)",
                name,
                start_ts,
                str(e),
            )
            continue

        key = f"[{sport}] {name} ({TAG})"

        # Check if already cached with source
        cached_event = cached_urls.get(key)
        if cached_event and cached_event.get("source"):
            continue

        # Check if event is within the 30-minute window
        if not start_dt <= event_dt <= end_dt:
            log.debug(f"Event outside window: {name} at {event_dt}")
            continue

        # Get poster/logo
        logo = event.get("poster") or event.get("logo") or event.get("image")

        events.append(
            DAMIEvent(
                sport=sport,
                name=name,
                logo=logo,
                stream_id=stream_id,
                timestamp=event_dt.timestamp(),
            )
        )

    log.info(
        "Found %d eligible live event(s) within 30-minute window",
        len(events),
    )

    return events


def generate_m3u8_files(events_data: dict[str, dict[str, str | float]]) -> None:
    """Generate VLC and TiviMate M3U8 files from events data."""
    
    vlc_filename = f"{TAG.lower()}_vlc.m3u8"
    tivimate_filename = f"{TAG.lower()}_tivimate.m3u8"
    
    vlc_content = ['#EXTM3U']
    tivimate_content = ['#EXTM3U']
    
    channel_counter = 1
    
    for event_name, event_info in events_data.items():
        source_url = event_info.get("source")
        
        # Skip if no source URL
        if not source_url:
            continue
        
        # Get sport from event name or use default
        sport = "Live Events"
        for s in ["MLB", "NBA", "NHL", "NFL", "WNBA", "Football", "Soccer", "Basketball", "Leagues Cup"]:
            if s in event_name:
                sport = s
                break
        
        tvg_id = event_info.get("tvg-id", "Live.Event.us")
        logo = event_info.get("logo", "")
        referer = event_info.get("refer", BASE_URL)
        
        # VLC format
        vlc_entry = f'#EXTINF:-1 tvg-chno="{channel_counter}" tvg-id="{tvg_id}" tvg-name="{event_name}" tvg-logo="{logo}" group-title="{sport}",{event_name}'
        vlc_content.append(vlc_entry)
        vlc_content.append(f'#EXTVLCOPT:http-referrer={referer}')
        vlc_content.append(f'#EXTVLCOPT:http-origin={referer}')
        vlc_content.append('#EXTVLCOPT:http-user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36 Edg/134.0.0.0')
        vlc_content.append(source_url)
        
        # TiviMate format (pipe-separated with encoded user agent)
        user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36 Edg/134.0.0.0"
        encoded_user_agent = quote(user_agent, safe='')
        
        tivimate_entry = f'#EXTINF:-1 tvg-chno="{channel_counter}" tvg-id="{tvg_id}" tvg-name="{event_name}" tvg-logo="{logo}" group-title="{sport}",{event_name}'
        tivimate_content.append(tivimate_entry)
        tivimate_content.append(f'{source_url}|referer={referer}|origin={referer}|user-agent={encoded_user_agent}')
        
        channel_counter += 1
    
    # Write VLC file
    try:
        with open(vlc_filename, 'w', encoding='utf-8') as f:
            f.write('\n'.join(vlc_content))
        log.info(f"Generated VLC M3U8 file: {vlc_filename}")
    except Exception as e:
        log.error(f"Error writing VLC M3U8 file: {e}")
    
    # Write TiviMate file
    try:
        with open(tivimate_filename, 'w', encoding='utf-8') as f:
            f.write('\n'.join(tivimate_content))
        log.info(f"Generated TiviMate M3U8 file: {tivimate_filename}")
    except Exception as e:
        log.error(f"Error writing TiviMate M3U8 file: {e}")


async def scrape() -> None:
    cached_urls = CACHE_FILE.load()

    valid_urls = {k: v for k, v in cached_urls.items() if v.get("source")}

    valid_count = cached_count = len(valid_urls)

    urls.update(valid_urls)

    log.info(f"Loaded {cached_count} event(s) from cache")

    log.info(f'Scraping from "{BASE_URL}"')

    if events := await get_events(cached_urls):
        log.info(f"Processing {len(events)} new URL(s)")

        for i, ev in enumerate(events, start=1):
            handler = partial(
                process_event,
                stream_id=ev.stream_id,
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
                "refer": urljoin(BASE_URL, f"embed/?id={ev.stream_id}"),
                "timestamp": ev.timestamp,
                "tvg-id": tvg_id or "Live.Event.us",
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
    
    # Generate M3U8 files after scraping
    generate_m3u8_files(urls)


async def main() -> None:
    """Main entry point for the script."""
    try:
        log.info(f"Starting {TAG} updater...")
        log.info(f"Using BASE_URL: {BASE_URL}")
        await scrape()
        log.info(f"{TAG} updater completed successfully")
    except Exception as e:
        log.error(f"{TAG} updater failed: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())
