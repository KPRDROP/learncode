import ast
import base64
import os
import re
from collections.abc import KeysView
from functools import partial
from urllib.parse import quote, urljoin

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TAG = "OVOGOZO"

CACHE_FILE = Cache(TAG, exp=28_800)

BASE_URL = "https://ovostream.net/"

REFERER = "https://gozowatch.top/"
ORIGIN = "https://gozowatch.top"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/134.0.0.0 Safari/537.36 Edg/134.0.0.0"
)

# Browser headers to avoid 403 Forbidden
REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
    "DNT": "1",
}


# ---------------------------------------------------------------------------
# Request helper (Using curl_cffi for advanced TLS fingerprint spoofing)
# ---------------------------------------------------------------------------

async def fetch_page(url: str) -> str | None:
    """Fetch page content using curl_cffi to bypass Cloudflare 403 blocks"""
    try:
        from curl_cffi import requests as cffi_requests
        
        # impersonate="chrome" spoofs the exact TLS/JA4 fingerprint of Google Chrome
        response = cffi_requests.get(
            url,
            headers=REQUEST_HEADERS,
            impersonate="chrome",
            timeout=30,
        )
        
        if response.status_code == 200:
            log.debug(f"Successfully fetched {url}")
            return response.text
        else:
            log.error(f"Failed to fetch {url} - Status: {response.status_code}")
            return None
            
    except ImportError:
        log.error("curl_cffi not installed. Please install: pip install curl_cffi")
        return None
    except Exception as e:
        log.error(f"Curl_cffi error: {e}")
        return None


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def fix_event_name(name: str) -> str:
    """Fix event name capitalization and replace VS with vs"""
    name = name.replace(" VS ", " vs ")
    
    words = []
    for word in name.split():
        if word.lower() == "vs":
            words.append("vs")
        elif len(word) <= 4 and word.isupper():
            words.append(word.upper())
        else:
            words.append(word.capitalize())
    
    return " ".join(words)


def normalize_source(source: str | None) -> str | None:
    """Clean up and validate the source URL"""
    if not source:
        return None

    source = source.strip().strip("\"'")
    if not source:
        return None

    source = source.rstrip("/")

    if not re.match(r"^https?://", source, re.IGNORECASE):
        return None

    return source


# ---------------------------------------------------------------------------
# Process event
# ---------------------------------------------------------------------------

async def process_event(url: str, url_num: int) -> tuple[str | None, str | None]:
    nones = None, None

    log.info(f"URL {url_num}) Processing event page")

    page_content = await fetch_page(url)
    
    if not page_content:
        log.error(f"URL {url_num}) Failed to fetch event page")
        return nones

    source_pattern = re.compile(r'var\s+sourceUrl\s*=\s*"([^"]+)"', re.IGNORECASE)
    source_match = source_pattern.search(page_content)
    
    if not source_match:
        log.warning(f"URL {url_num}) No sourceUrl found in page")
        return nones

    stream_url = source_match[1]
    stream_url = normalize_source(stream_url)
    
    if not stream_url:
        log.warning(f"URL {url_num}) Invalid stream URL extracted")
        return nones

    log.info(f"URL {url_num}) Captured stream source: {stream_url}")
    
    return stream_url, url


# ---------------------------------------------------------------------------
# Event discovery
# ---------------------------------------------------------------------------

async def get_events(cached_keys: KeysView[str]) -> list[Event]:
    events: list[Event] = []

    log.info(f'Fetching events from "{BASE_URL}"')
    
    page_content = await fetch_page(BASE_URL)
    
    if not page_content:
        log.error(f'Failed to fetch "{BASE_URL}"')
        return events

    soup = HTMLParser(page_content)

    for card in soup.css(".card"):
        data_search = card.attributes.get("data-search")
        if not data_search:
            continue

        sport_elem = card.css_first(".sport-tag")
        if not sport_elem:
            continue
        
        sport = sport_elem.text(strip=True).capitalize()
        sport = "Live Event" if sport == "Sports" else sport

        event_name = fix_event_name(data_search)

        watch_btn = card.css_first("a.watch-btn")
        if not watch_btn:
            continue

        href = watch_btn.attributes.get("href")
        if not href:
            continue

        event_url = urljoin(BASE_URL, href)
        
        key = f"[{sport}] {event_name} ({TAG})"

        if key in cached_keys:
            continue

        events.append(
            Event(
                sport=sport,
                name=event_name,
                link=event_url,
            )
        )

    log.info(f"Found {len(events)} new event(s)")
    return events


# ---------------------------------------------------------------------------
# Scrape
# ---------------------------------------------------------------------------

async def scrape() -> None:
    cached_urls = CACHE_FILE.load()

    valid_urls = {k: v for k, v in cached_urls.items() if v.get("source")}

    valid_count = cached_count = len(valid_urls)

    urls.clear()
    urls.update(valid_urls)

    log.info(f"Loaded {cached_count} event(s) from cache")
    log.info(f'Scraping from "{BASE_URL}"')

    events = await get_events(cached_urls.keys())
    if not events:
        log.info("No new events found")
        CACHE_FILE.write(cached_urls)
        return

    log.info(f"Processing {len(events)} new URL(s)")
    now = Time.rn()

    for i, ev in enumerate(events, start=1):
        handler = partial(
            process_event,
            url=ev.link,
            url_num=i,
        )

        source, referer = await network.safe_process(
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
            "logo": logo or "https://i.gyazo.com/4a5e9fa2525808ee4b65002b56d3450e.png",
            "refer": referer or ev.link or REFERER,
            "origin": ORIGIN,
            "timestamp": now.timestamp(),
            "tvg-id": tvg_id or "Live.Event.us",
            "link": ev.link,
        }

        cached_urls[key] = entry

        if source:
            valid_count += 1
            urls[key] = entry
            log.info(f"URL {i}) Saved event: {key}")
        else:
            log.warning(f"No stream source for: {key}")

    log.info(f"Collected and cached {valid_count - cached_count} new event(s)")
    CACHE_FILE.write(cached_urls)


# ---------------------------------------------------------------------------
# VLC playlist
# ---------------------------------------------------------------------------

def generate_vlc_m3u8() -> str:
    content = "#EXTM3U\n"
    playlist_index = 0

    for title, data in urls.items():
        source = data.get("source")
        if not source:
            continue

        playlist_index += 1
        tvg_id = data.get("tvg-id", "Live.Event.us")
        logo = data.get("logo", "https://i.gyazo.com/4a5e9fa2525808ee4b65002b56d3450e.png")
        referer = data.get("refer", REFERER)
        origin = data.get("origin", ORIGIN)

        content += (
            f'#EXTINF:-1 '
            f'tvg-chno="{playlist_index}" '
            f'tvg-id="{tvg_id}" '
            f'tvg-name="{title}" '
            f'tvg-logo="{logo}" '
            f'group-title="Live Events",'
            f'{title}\n'
        )
        content += f"#EXTVLCOPT:http-referrer={referer}\n"
        content += f"#EXTVLCOPT:http-origin={origin}\n"
        content += f"#EXTVLCOPT:http-user-agent={USER_AGENT}\n"
        content += f"{source}\n"

    return content


# ---------------------------------------------------------------------------
# TiviMate playlist
# ---------------------------------------------------------------------------

def generate_tivimate_m3u8() -> str:
    content = "#EXTM3U\n"
    encoded_user_agent = quote(USER_AGENT, safe="")
    playlist_index = 0

    for title, data in urls.items():
        source = data.get("source")
        if not source:
            continue

        playlist_index += 1
        tvg_id = data.get("tvg-id", "Live.Event.us")
        logo = data.get("logo", "https://i.gyazo.com/4a5e9fa2525808ee4b65002b56d3450e.png")
        referer = data.get("refer", REFERER)
        origin = data.get("origin", ORIGIN)

        content += (
            f'#EXTINF:-1 '
            f'tvg-chno="{playlist_index}" '
            f'tvg-id="{tvg_id}" '
            f'tvg-name="{title}" '
            f'tvg-logo="{logo}" '
            f'group-title="Live Events",'
            f'{title}\n'
        )
        content += (
            f"{source}"
            f"|referer={referer}"
            f"|origin={origin}"
            f"|user-agent={encoded_user_agent}\n"
        )

    return content


# ---------------------------------------------------------------------------
# Write output files
# ---------------------------------------------------------------------------

def write_output_files() -> None:
    output_dir = os.getenv("OUTPUT_DIR", ".")
    os.makedirs(output_dir, exist_ok=True)

    vlc_file = os.path.join(output_dir, "ozog_vlc.m3u8")
    with open(vlc_file, "w", encoding="utf-8", newline="\n") as f:
        f.write(generate_vlc_m3u8())

    tivimate_file = os.path.join(output_dir, "ozog_tivimate.m3u8")
    with open(tivimate_file, "w", encoding="utf-8", newline="\n") as f:
        f.write(generate_tivimate_m3u8())

    log.info(f"Generated VLC playlist: {vlc_file}")
    log.info(f"Generated TiviMate playlist: {tivimate_file}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    log.info("Starting OVOGOZO updater")
    await scrape()

    if urls:
        write_output_files()
        log.info(f"Successfully processed {len(urls)} event(s)")
    else:
        log.warning("No events found to write to output files")


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
