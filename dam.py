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


async def get_events(cached_keys: KeysView[str]) -> list[DAMIEvent]:
    now = Time.clean(Time.now())

    events: list[Event] = []

    if not (api_data := API_FILE.load(per_entry=False)):
        log.info("Refreshing API cache")

        api_data = {"timestamp": now.timestamp()}

        if r := await network.request(
            urljoin(BASE_URL, "papi/api/streams"),
            log=log,
        ):
            api_data = r.json()

        API_FILE.write(api_data)

    start_dt = now.delta(minutes=-3)
    end_dt = now.delta(minutes=30)

    for stream_group in api_data.get("streams", []):
        if stream_group["category"] == "24/7-streams":
            continue

        for event in stream_group.get("streams", []):
            if not all(
                values := [
                    event.get(x)
                    for x in (
                        "name",
                        "league",
                        "starts_at",
                        "id",
                    )
                ]
            ):
                continue

            name, sport, start_ts, stream_id = values

            if stream_id.lower().startswith("dl-"):
                continue

            event_dt = Time.from_ts(start_ts)

            if f"[{sport}] {name} ({TAG})" in cached_keys:
                continue

            elif not start_dt <= event_dt <= end_dt:
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

    valid_urls = {k: v for k, v in cached_urls.items() if v["source"]}

    valid_count = cached_count = len(valid_urls)

    urls.update(valid_urls)

    log.info(f"Loaded {cached_count} event(s) from cache")

    log.info(f'Scraping from "{BASE_URL}"')

    if events := await get_events(cached_urls.keys()):
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
                semaphore=network.PW_S,
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
