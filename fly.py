import asyncio
import re
import os
import json
from collections.abc import KeysView
from functools import partial
from typing import Dict

from playwright.async_api import async_playwright, Browser, Page

from utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "FLY"

CACHE_FILE = Cache(TAG, exp=7_200)

API_FILE = Cache(f"{TAG}-api", exp=19_800)

# Secret variables
FLY_API_URL = os.getenv("FLY_API_URL")
FLY_BASE_URL = os.getenv("FLY_BASE_URL")
VLC_USER_AGENT = os.getenv("VLC_USER_AGENT")
TIVIMATE_USER_AGENT = os.getenv("TIVIMATE_USER_AGENT")


def clean_name(s: str) -> str:
    return re.sub(r"(\r|\n)", "", s).strip()


def clean_m3u(s: str) -> str:
    return re.sub(r"\.live\n", ".pro", s)


def clean_display_name(name: str) -> str:
    """
    Clean display name by removing commas and extra spaces.
    
    Args:
        name: Display name
        
    Returns:
        Cleaned display name
    """
    if not name:
        return ""
    # Remove commas but keep the text around them
    cleaned = re.sub(r',\s*', ' ', name)
    # Remove extra spaces
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned


def encode_user_agent(user_agent: str) -> str:
    """
    Encode the user agent for URL parameters.
    
    Args:
        user_agent: User agent string
        
    Returns:
        URL-encoded user agent
    """
    # URL encode the user agent
    encoded = user_agent.replace(' ', '%20')
    encoded = encoded.replace('(', '%28')
    encoded = encoded.replace(')', '%29')
    encoded = encoded.replace(';', '%3B')
    encoded = encoded.replace(',', '%2C')
    return encoded


async def process_event(
    url: str,
    url_num: int,
    page: Page,
    timeout: int | float = 10,
) -> tuple[str | None, str | None]:

    nones = None, None

    captured: list[str] = []

    got_one = asyncio.Event()

    handler = partial(
        network.capture_req,
        captured=captured,
        got_one=got_one,
    )

    page.on("request", handler)

    try:
        resp = await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=6_000,
        )

        if not resp or resp.status != 200:
            log.warning(
                f"URL {url_num}) Status Code: {resp.status if resp else 'None'}"
            )
            return nones

        iframe = page.locator("iframe")

        iframe_src = await iframe.get_attribute("src", timeout=1_500)

        wait_task = asyncio.create_task(got_one.wait())

        try:
            await asyncio.wait_for(wait_task, timeout=timeout)
        except TimeoutError:
            log.warning(f"URL {url_num}) Timed out waiting for M3U8.")
            return nones

        finally:
            if not wait_task.done():
                wait_task.cancel()

                try:
                    await wait_task
                except asyncio.CancelledError:
                    pass

        if captured:
            log.info(f"URL {url_num}) Captured M3U8")
            return captured[0], iframe_src

    except Exception as e:
        log.warning(f"URL {url_num}) {e}")
        return nones

    finally:
        page.remove_listener("request", handler)


async def get_events(cached_keys: KeysView[str]) -> list[Event]:
    # Fix: Use Time.rn() which exists in your Time class
    now = Time.rn()

    events: list[Event] = []

    # Fix: Use ts_index=-1 instead of index=-1
    if not (api_data := API_FILE.load(per_entry=False, ts_index=-1)):
        log.info("Refreshing API cache")

        api_data = [{"timestamp": now.timestamp()}]

        if r := await network.request(
            FLY_API_URL,  # Using secret variable
            log=log,
        ):
            api_data: list[dict[str, str]] = r.json()

            api_data[-1]["timestamp"] = now.timestamp()

        API_FILE.write(api_data)

    # Adjust time window for more events
    start_dt = now.delta(hours=-8)
    end_dt = now.delta(minutes=60)  # Increased from 2 to 60 minutes

    for event_group in api_data:
        if not all(
            values := [
                event_group.get(x)
                for x in (
                    "League",
                    "Team 1 ",
                    "Team2",
                    "Date",
                    "Time",
                    "iframeURL",
                )
            ]
        ):
            continue

        sport, away, home, date, time, link = values

        # Fix: Use tz_name parameter correctly
        event_dt = Time.from_str(f"{date.replace(' ','')} {time}", tz_name="GMT")

        if not start_dt <= event_dt <= end_dt:
            continue

        sport, name = clean_name(sport), clean_name(f"{away} vs {home}")

        if f"[{sport}] {name} ({TAG})" in cached_keys:
            continue

        events.append(
            Event(
                sport=sport,
                name=name,
                link=link,
                timestamp=now.timestamp(),
            )
        )

    return events


async def scrape() -> None:
    cached_urls = CACHE_FILE.load()

    valid_urls = {k: v for k, v in cached_urls.items() if v["source"]}

    valid_count = cached_count = len(valid_urls)

    urls.update(valid_urls)

    log.info(f"Loaded {cached_count} event(s) from cache")

    log.info('Scraping from "flyembed"')

    if events := await get_events(cached_urls.keys()):
        log.info(f"Processing {len(events)} new URL(s)")

        # Launch browser using playwright directly
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-setuid-sandbox']
            )
            
            try:
                async with network.event_context(browser) as context:
                    for i, ev in enumerate(events, start=1):
                        async with network.event_page(context) as page:
                            handler = partial(
                                process_event,
                                url=ev.link,
                                url_num=i,
                                page=page,
                            )

                            source, iframe = await network.safe_process(
                                handler,
                                url_num=i,
                                timeout_return=(None, None),
                                semaphore=network.HTTP_S,
                                log=log,
                            )

                            key = f"[{ev.sport}] {ev.name} ({TAG})"

                            tvg_id, logo = leagues.get_tvg_info(ev.sport, ev.name)

                            entry = {
                                "source": source,
                                "logo": logo,
                                "refer": iframe,
                                "timestamp": ev.timestamp,
                                "tvg-id": tvg_id or "Live.Event.us",
                                "link": ev.link,
                                "sport": ev.sport,
                                "name": ev.name,
                            }

                            cached_urls[key] = entry

                            if source:
                                valid_count += 1

                                entry["source"] = clean_m3u(source)

                                urls[key] = entry
            finally:
                await browser.close()

        log.info(f"Collected and cached {valid_count - cached_count} new event(s)")

    else:
        log.info("No new events found")

    CACHE_FILE.write(cached_urls)

    # Generate M3U8 files after scraping
    await generate_m3u8_files(urls)


async def generate_m3u8_files(channels_data: Dict[str, Dict[str, str | float]], output_dir: str = ".") -> None:
    """
    Generate two M3U8 files from channel data.
    
    Args:
        channels_data: Dictionary containing channel information
        output_dir: Directory where files will be saved
    """
    # Ensure output directory exists
    os.makedirs(output_dir, exist_ok=True)
    
    vlc_path = os.path.join(output_dir, "fly_vlc.m3u8")
    tivimate_path = os.path.join(output_dir, "fly_tivimate.m3u8")
    
    # Filter out channels without source
    valid_channels = {k: v for k, v in channels_data.items() if v.get("source")}
    
    if not valid_channels:
        log.warning("No valid channels found to generate M3U8 files")
        # Create empty files with headers
        with open(vlc_path, 'w', encoding='utf-8') as f:
            f.write("#EXTM3U\n")
        with open(tivimate_path, 'w', encoding='utf-8') as f:
            f.write("#EXTM3U\n")
        os.chmod(vlc_path, 0o644)
        os.chmod(tivimate_path, 0o644)
        return
    
    # Generate VLC format
    with open(vlc_path, 'w', encoding='utf-8') as vlc_file:
        vlc_file.write("#EXTM3U\n")
        chno = 1
        for key, channel in valid_channels.items():
            vlc_line = format_vlc_channel(key, channel, chno)
            vlc_file.write(vlc_line + "\n")
            chno += 1
    
    # Generate Tivimate format
    with open(tivimate_path, 'w', encoding='utf-8') as tivimate_file:
        tivimate_file.write("#EXTM3U\n")
        chno = 1
        for key, channel in valid_channels.items():
            tivimate_line = format_tivimate_channel(key, channel, chno)
            tivimate_file.write(tivimate_line + "\n")
            chno += 1
    
    # Set write permissions (read/write for owner, read for others)
    os.chmod(vlc_path, 0o644)
    os.chmod(tivimate_path, 0o644)
    
    log.info(f"Generated {vlc_path} with {chno-1} channel(s)")
    log.info(f"Generated {tivimate_path} with {chno-1} channel(s)")


def format_vlc_channel(key: str, channel: Dict[str, str | float], chno: int) -> str:
    """
    Format a channel for VLC M3U8 format.
    
    Args:
        key: Channel key
        channel: Channel data dictionary
        chno: Channel number
        
    Returns:
        Formatted string for VLC
    """
    # Extract channel info
    sport = channel.get("sport", "Live Event")
    name = channel.get("name", key)
    
    # Clean display name
    display_name = clean_display_name(key.replace(f" ({TAG})", ""))
    
    # VLC format
    tvg_name = f"[{sport}] {name} ({TAG})"
    
    extinf = (f'#EXTINF:-1 tvg-chno="{chno}" '
              f'tvg-id="{channel.get("tvg-id", "Live.Event.us")}" '
              f'tvg-name="{tvg_name}" '
              f'tvg-logo="{channel.get("logo", "")}" '
              f'group-title="{sport}",'
              f'{display_name}')
    
    # Get referrer from channel data or use default
    referer = channel.get("refer", FLY_BASE_URL)
    
    # Add VLC options
    options = [
        f"#EXTVLCOPT:http-referrer={referer}",
        f"#EXTVLCOPT:http-origin={referer}",
        f'#EXTVLCOPT:http-user-agent={VLC_USER_AGENT}'
    ]
    
    url = channel.get("source", "")
    
    return f"{extinf}\n" + "\n".join(options) + f"\n{url}"


def format_tivimate_channel(key: str, channel: Dict[str, str | float], chno: int) -> str:
    """
    Format a channel for Tivimate M3U8 format using pipe separator.
    
    Args:
        key: Channel key
        channel: Channel data dictionary
        chno: Channel number
        
    Returns:
        Formatted string for Tivimate
    """
    # Extract channel info
    sport = channel.get("sport", "Live Event")
    name = channel.get("name", key)
    
    # Clean display name for Tivimate
    display_name = clean_display_name(key.replace(f" ({TAG})", f" ({TAG}TV)"))
    
    # Tivimate format
    tvg_name = f"[{sport}] {name} ({TAG}TV)"
    
    # Tivimate format with pipe separator
    extinf = (f'#EXTINF:-1 tvg-chno="{chno}" '
              f'tvg-id="{channel.get("tvg-id", "Live.Event.us")}" '
              f'tvg-name="{tvg_name}" '
              f'tvg-logo="{channel.get("logo", "")}" '
              f'group-title="{sport}",'
              f'{display_name}')
    
    # Encode the user agent for Tivimate
    encoded_user_agent = encode_user_agent(TIVIMATE_USER_AGENT)
    
    # Get referrer from channel data or use default
    referer = channel.get("refer", FLY_BASE_URL)
    
    # Build the URL with parameters
    url = channel.get("source", "")
    params = [
        f"referer={referer}/",
        f"origin={referer}",
        f"user-agent={encoded_user_agent}"
    ]
    
    return f"{extinf}\n{url}|{'|'.join(params)}"


async def main() -> None:
    """
    Main function to run the scraper and generate M3U8 files.
    """
    log.info(f"Starting {TAG} scraper")
    await scrape()
    log.info(f"{TAG} scraper completed")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
