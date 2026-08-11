from collections.abc import KeysView
from dataclasses import dataclass
from functools import partial
from urllib.parse import urljoin
from pathlib import Path
import os

from playwright.async_api import Browser
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "BUZZEA"

CACHE_FILE = Cache(TAG, exp=5_400)

HTML_FILE = Cache(f"{TAG}-html", exp=28_800)

# Use environment variable with fallback
BASE_URL = os.getenv("BUZZEA_BASE_URL")

# Constants for output files
REFERER = "https://exposestrat.com/"
ORIGIN = "https://exposestrat.com"
USER_AGENT = "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
USER_AGENT_ENCODED = "Mozilla%2F5.0%20(Linux%3B%20Android%2010%3B%20K)%20AppleWebKit%2F537.36%20(KHTML%2C%20like%20Gecko)%20Chrome%2F120.0.0.0%20Mobile%20Safari%2F537.36"


@dataclass(kw_only=True, slots=True)
class BZEvent(Event):
    event_ts: int | float


async def refresh_html_cache(now: Time) -> dict[str, dict[str, str | float]]:
    events = {}

    if not (html_data := await network.request(BASE_URL, log=log)):
        return events

    soup = HTMLParser(html_data.content)

    for game in soup.css("tr.event-group"):
        if not all(
            values := [
                game.css_first(x)
                for x in (
                    "td.category-name",
                    "td.team-name",
                    "td",
                    "a.watch-btn",
                )
            ]
        ):
            continue

        sport, event_name, event_date, ch_id = (x.text(strip=True) for x in values)

        event_dt = Time.from_str(event_date.replace("\t", " "), timezone="EST")

        key = f"[{sport}] {event_name} ({TAG})"

        events[key] = {
            "sport": sport,
            "name": event_name,
            "link": urljoin(str(html_data.url), f"set.php?{ch_id}"),
            "event_ts": event_dt.timestamp(),
            "timestamp": now.timestamp(),
        }

    return events


async def get_events(cached_keys: KeysView[str]) -> list[BZEvent]:
    now = Time.clean(Time.now())

    if not (events := HTML_FILE.load()):
        log.info("Refreshing HTML cache")

        events = await refresh_html_cache(now)

        HTML_FILE.write(events)

    # Expanded time window to get more events (6 hours before to 2 hours after)
    start_ts = now.delta(hours=-6).timestamp()
    end_ts = now.delta(hours=2).timestamp()

    return [
        BZEvent(**v)
        for k, v in events.items()
        if k not in cached_keys and start_ts <= v["event_ts"] <= end_ts
    ]


def generate_m3u8_files(events_data: dict[str, dict]) -> None:
    """Generate VLC and TiviMate M3U8 files from event data"""
    
    # Sort events by sport and time for better organization
    sorted_events = sorted(
        [(k, v) for k, v in events_data.items() if v.get("source")],
        key=lambda x: (x[1].get("sport", ""), x[1].get("timestamp", 0))
    )
    
    vlc_lines = []
    tivimate_lines = []
    valid_streams = 0
    
    for idx, (key, data) in enumerate(sorted_events, start=1):
        if not data.get("source"):
            continue
            
        valid_streams += 1
        
        # Extract event info from key
        # Key format: "[Sport] Event Name (TAG)"
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
    vlc_output_path = Path("buzzea_vlc.m3u8")
    with open(vlc_output_path, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        f.write("\n".join(vlc_lines))
    
    # Write TiviMate file
    tivimate_output_path = Path("buzzea_tivimate.m3u8")
    with open(tivimate_output_path, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        f.write("\n".join(tivimate_lines))
    
    log.info(f"Generated {vlc_output_path} with {valid_streams} streams")
    log.info(f"Generated {tivimate_output_path} with {valid_streams} streams")
    
    # Verify files were created
    if vlc_output_path.exists():
        log.info(f" {vlc_output_path} exists ({vlc_output_path.stat().st_size} bytes)")
    else:
        log.error(f" {vlc_output_path} was not created!")
        
    if tivimate_output_path.exists():
        log.info(f" {tivimate_output_path} exists ({tivimate_output_path.stat().st_size} bytes)")
    else:
        log.error(f" {tivimate_output_path} was not created!")


async def scrape(browser: Browser) -> None:
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
                    handler = partial(
                        network.process_event,
                        url=ev.link,
                        url_num=i,
                        page=page,
                        log=log,
                    )

                    source = await network.safe_process(
                        handler,
                        url_num=i,
                        semaphore=network.PW_S,
                        log=log,
                    )

                    tvg_id, logo = leagues.get_tvg_info(ev.sport, ev.name)

                    key = f"[{ev.sport}] {ev.name} ({TAG})"

                    entry = {
                        "source": source,
                        "logo": logo,
                        "refer": REFERER,
                        "timestamp": ev.event_ts,
                        "tvg-id": tvg_id or "Live.Event.us",
                        "link": ev.link,
                        "sport": ev.sport,  # Store sport for sorting
                    }

                    cached_urls[key] = entry

                    if source:
                        valid_count += 1
                        urls[key] = entry

        log.info(f"Collected and cached {valid_count - cached_count} new event(s)")

    else:
        log.info("No new events found")

    CACHE_FILE.write(cached_urls)
    
    # Generate M3U8 files after updating cache
    generate_m3u8_files(cached_urls)


async def main() -> None:
    """Main function to run the updater"""
    log.info("Starting BUZZEA updater...")
    
    # Browser is passed from the calling script
    # This function is meant to be called with a browser instance
    # If called directly, it will raise an error
    
    # Note: This is a placeholder - the actual browser is passed from the main script
    # that imports and calls this function
    log.info("BUZZEA updater needs to be called with a browser instance")
    
    # The actual scraping is done in the scrape() function which requires a browser
    # This main function is just for consistency with other modules


if __name__ == "__main__":
    import asyncio
    from playwright.async_api import async_playwright
    
    async def run():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                await scrape(browser)
            finally:
                await browser.close()
    
    asyncio.run(run())
