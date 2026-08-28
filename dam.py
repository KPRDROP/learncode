from collections.abc import KeysView
from dataclasses import dataclass
from functools import partial
from urllib.parse import urljoin, quote
import os
import asyncio

from utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "DAM"

CACHE_FILE = Cache(TAG, exp=10_800)

API_FILE = Cache(f"{TAG}-api", exp=3_600)

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
    # Use Time.rn() instead of Time.clean(Time.now())
    now = Time.rn()

    events: list[DAMIEvent] = []

    if not (api_data := API_FILE.load(per_entry=False, ts_index=-1)):
        log.info("Refreshing API cache")

        api_data = [{"timestamp": now.timestamp()}]

        if r := await network.request(
            urljoin(BASE_URL, "papi/api/streams"),
            log=log,
        ):
            api_data = r.json()
            api_data[-1]["timestamp"] = now.timestamp()

        API_FILE.write(api_data)

    # ---------------------------------------------------------------
    # Capture both live and upcoming events.
    #
    # Default:
    #   24 hours before now
    #   14 days after now
    #
    # Override with:
    #   DAM_EVENT_PAST_HOURS
    #   DAM_EVENT_FUTURE_DAYS
    # ---------------------------------------------------------------

    try:
        past_hours = max(
            0.0,
            float(os.getenv("DAM_EVENT_PAST_HOURS", "24")),
        )
    except (TypeError, ValueError):
        past_hours = 24.0

    try:
        future_days = max(
            0.0,
            float(os.getenv("DAM_EVENT_FUTURE_DAYS", "14")),
        )
    except (TypeError, ValueError):
        future_days = 14.0

    start_dt = now.delta(minutes=-(past_hours * 60))
    end_dt = now.delta(minutes=(future_days * 24 * 60))

    log.info(
        "Event window: %s -> %s "
        "(past=%.1fh, future=%.1fd)",
        start_dt,
        end_dt,
        past_hours,
        future_days,
    )

    for stream_group in api_data.get("streams", []):
        if stream_group.get("category") == "24/7-streams":
            continue

        for event in stream_group.get("streams", []):
            values = [
                event.get(x)
                for x in (
                    "name",
                    "league",
                    "starts_at",
                    "id",
                )
            ]

            if not all(values):
                continue

            name, sport, start_ts, stream_id = values

            stream_id = str(stream_id)

            if stream_id.lower().startswith("dl-"):
                continue

            if stream_id.startswith("247") or sport.startswith("24/7"):
                continue

            try:
                # Handle timestamp conversion similar to original
                event_dt = Time.from_ts(int(f"{start_ts}"[:-3]) if isinstance(start_ts, (int, float)) and len(str(int(start_ts))) > 10 else start_ts)
            except (TypeError, ValueError, OverflowError):
                log.warning(
                    "Invalid starts_at for %s: %r",
                    name,
                    start_ts,
                )
                continue

            key = f"[{sport}] {name} ({TAG})"

            cached_event = cached_urls.get(key)

            if cached_event and cached_event.get("source"):
                continue

            # Accept live/recent + upcoming events.
            if not start_dt <= event_dt <= end_dt:
                continue

            events.append(
                DAMIEvent(
                    sport=sport,
                    name=name,
                    logo=event.get("poster"),
                    stream_id=stream_id,
                    timestamp=event_dt.timestamp(),
                )
            )

    log.info(
        "Found %d eligible live/upcoming event(s)",
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
        for s in ["MLB", "NBA", "NHL", "NFL", "WNBA", "Football", "Soccer", "Leagues Cup"]:
            if s in event_name:
                sport = s
                break
        
        tvg_id = event_info.get("tvg-id", "Live.Event.us")
        logo = event_info.get("logo", "")
        referer = BASE_URL
        
        # VLC format
        vlc_entry = f'#EXTINF:-1 tvg-chno="{channel_counter}" tvg-id="{tvg_id}" tvg-name="{event_name}" tvg-logo="{logo}" group-title="{sport}",{event_name}'
        vlc_content.append(vlc_entry)
        vlc_content.append(f'#EXTVLCOPT:http-referrer={referer}/')
        vlc_content.append(f'#EXTVLCOPT:http-origin={referer}')
        vlc_content.append('#EXTVLCOPT:http-user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36')
        vlc_content.append(source_url)
        
        # TiviMate format (pipe-separated with encoded user agent)
        user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
        encoded_user_agent = quote(user_agent, safe='')
        
        tivimate_entry = f'#EXTINF:-1 tvg-chno="{channel_counter}" tvg-id="{tvg_id}" tvg-name="{event_name}" tvg-logo="{logo}" group-title="{sport}",{event_name}'
        tivimate_content.append(tivimate_entry)
        tivimate_content.append(f'{source_url}|referer={referer}/|origin={referer}|user-agent={encoded_user_agent}')
        
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
