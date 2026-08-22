import asyncio
import json
import os
import adblock
from collections.abc import KeysView
from functools import partial
from typing import Any
from urllib.parse import urlencode, urljoin

from playwright.async_api import Browser, Page

from utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "HOOF"

CACHE_FILE = Cache(TAG, exp=10_800)

BASE_DOMAIN = "hoofoot.ru"
BASE_URL = f"https://{BASE_DOMAIN}/iptv/schedule"
API_URL = f"https://{BASE_DOMAIN}/api/events"

# Output files
OUTPUT_VLC = "hoof_vlc.m3u8"
OUTPUT_TIVIMATE = "hoof_tivimate.m3u8"

# Headers for m3u8 streams
REFERER = "https://hoofoot.ru/"
ORIGIN = "https://hoofoot.ru"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"


def build_wfty_url(live: bool = True) -> str:
    """Build the API URL for fetching events."""
    params = {
        "live": str(live).lower(),
        "limit": "100",
    }
    return f"{API_URL}?{urlencode(params)}"


def build_event_url(event_id: str) -> str:
    """Build the URL for a specific event."""
    return f"https://{BASE_DOMAIN}/iptv/live-player?id={event_id}"


def build_stream_url(channel_id: str) -> str:
    """Build the stream URL from channel ID."""
    return f"https://{BASE_DOMAIN}/gl?id={channel_id}"


async def pre_process(url: str, url_num: int) -> str | None:
    """
    Fetch event data and extract stream URL.
    """
    if not (event_data := await network.request(url, url_num, log=log)):
        return

    try:
        api_data: list[dict] = event_data.json()
    except json.JSONDecodeError:
        log.warning(f"URL {url_num}) Invalid JSON response.")
        return

    if not api_data:
        log.warning(f"URL {url_num}) No API data found.")
        return

    # Get the first event from the list
    event = api_data[0] if isinstance(api_data, list) else api_data
    
    if not event:
        log.warning(f"URL {url_num}) No event data found.")
        return

    # Check if event has channels with valid stream URLs
    channels = event.get("Channels", [])
    if not channels:
        log.warning(f"URL {url_num}) No channels found.")
        return

    # Find the first channel with an ID
    for channel in channels:
        channel_id = channel.get("id")
        if channel_id:
            stream_url = build_stream_url(channel_id)
            log.info(f"URL {url_num}) Found stream: {stream_url}")
            return stream_url

    log.warning(f"URL {url_num}) No valid channel ID found.")
    return


async def process_event(
    url: str,
    url_num: int,
    page: Page,
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

    if not (iframe_url := await pre_process(url, url_num)):
        return nones

    try:
        resp = await page.goto(
            iframe_url,
            wait_until="domcontentloaded",
            timeout=6_000,
        )

        if not resp or resp.status != 200:
            log.error(f"URL {url_num}) Status Code: {resp.status if resp else 'None'}")
            return nones

        wait_task = asyncio.create_task(got_one.wait())

        try:
            await asyncio.wait_for(wait_task, timeout=6)
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
            return captured[0], iframe_url

    except Exception as e:
        log.warning(f"URL {url_num}) {e}")
        return nones

    finally:
        page.remove_listener("request", handler)


async def get_events(cached_keys: KeysView[str]) -> list[Event]:
    """Fetch events from the API."""
    events: list[Event] = []

    live_url = build_wfty_url(live=True)

    if not (live_data := await network.request(live_url, log=log)):
        return events

    try:
        api_data: list[dict[str, Any]] = live_data.json()
    except json.JSONDecodeError:
        log.warning("Invalid JSON response from API.")
        return events

    if not api_data:
        return events

    for event_data in api_data:
        # Skip if not live
        if event_data.get("Status") != "Live":
            continue

        event_id = event_data.get("id")
        if not event_id:
            continue

        match_name = event_data.get("Match", "Unknown Match")
        sport = event_data.get("Sport", "Unknown Sport")
        league = event_data.get("League", "Unknown League")

        # Create cache key
        cache_key = f"[{sport}] {match_name} ({TAG})"

        if cache_key in cached_keys:
            continue

        event_url = build_event_url(event_id)

        events.append(
            Event(
                sport=sport,
                name=match_name,
                link=event_url,
                league=league,
            )
        )

    return events


def write_m3u8_files(events_data: dict[str, dict]) -> None:
    """Write the collected events to VLC and Tivimate m3u8 files."""
    
    # VLC format (simple)
    vlc_lines = ["#EXTM3U"]
    
    # Tivimate format (with headers)
    tivimate_lines = ["#EXTM3U"]
    
    for key, data in events_data.items():
        if not data.get("source"):
            continue
            
        stream_url = data["source"]
        match_name = key.replace(f" ({TAG})", "").strip()
        
        # Extract sport and name from key
        # Format: "[Sport] Match Name (TAG)"
        if "]" in key:
            sport_part = key.split("]")[0].replace("[", "").strip()
            name_part = key.split("]")[1].replace(f" ({TAG})", "").strip()
        else:
            sport_part = "Unknown"
            name_part = key.replace(f" ({TAG})", "").strip()
        
        # VLC format
        vlc_lines.append(f"#EXTINF:-1,{sport_part} - {name_part}")
        vlc_lines.append(stream_url)
        
        # Tivimate format
        encoded_ua = USER_AGENT.replace("%", "%25").replace(" ", "%20")
        tivimate_line = f"{stream_url}|referer={REFERER}|origin={ORIGIN}|user-agent={encoded_ua}"
        tivimate_lines.append(f"#EXTINF:-1,{sport_part} - {name_part}")
        tivimate_lines.append(tivimate_line)
    
    # Write VLC file
    try:
        with open(OUTPUT_VLC, "w", encoding="utf-8") as f:
            f.write("\n".join(vlc_lines))
        log.info(f"VLC playlist written to {OUTPUT_VLC} with {len(vlc_lines)-1} streams")
    except Exception as e:
        log.error(f"Failed to write VLC playlist: {e}")
    
    # Write Tivimate file
    try:
        with open(OUTPUT_TIVIMATE, "w", encoding="utf-8") as f:
            f.write("\n".join(tivimate_lines))
        log.info(f"Tivimate playlist written to {OUTPUT_TIVIMATE} with {len(tivimate_lines)-1} streams")
    except Exception as e:
        log.error(f"Failed to write Tivimate playlist: {e}")


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

        now = Time.rn()

        async with network.event_context(browser, stealth=False) as context:
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
                        semaphore=network.PW_S,
                        log=log,
                        timeout=20,
                    )

                    key = f"[{ev.sport}] {ev.name} ({TAG})"

                    tvg_id, logo = leagues.get_tvg_info(ev.sport, ev.name)

                    entry = {
                        "source": source,
                        "logo": logo,
                        "refer": iframe,
                        "timestamp": now.timestamp(),
                        "tvg-id": tvg_id or "Live.Event.us",
                        "link": ev.link,
                        "sport": ev.sport,
                        "league": ev.league if hasattr(ev, 'league') else "",
                    }

                    cached_urls[key] = entry

                    if source:
                        valid_count += 1
                        urls[key] = entry

        log.info(f"Collected and cached {valid_count - cached_count} new event(s)")
        
        # Write m3u8 files
        write_m3u8_files(cached_urls)

    else:
        log.info("No new events found")

    CACHE_FILE.write(cached_urls)


async def main():
    """Main entry point for the script."""
    from playwright.async_api import async_playwright
    
    log.info("Starting hoofoot scraper...")
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            await scrape(browser)
        finally:
            await browser.close()
    
    log.info("Scraping completed.")


if __name__ == "__main__":
    asyncio.run(main())
