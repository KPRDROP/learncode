import ast
import asyncio
import re
from functools import partial
from pathlib import Path
from urllib.parse import urljoin, quote

import cloudscraper
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "CAST"

CACHE_FILE = Cache(TAG, exp=12_600)

# Output files
OUT_VLC = Path("cast_vlc.m3u8")
OUT_TIVI = Path("cast_tivimate.m3u8")

# User agent for outputs
UA_RAW = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
UA_ENC = quote(UA_RAW)

BASE_URLS = {
    "MLB": {"base": "https://mlbwebcast.com", "api": "stream/check_stream.php"},
    "NFL": {"base": "https://nflwebcast.com", "api": "live/check_stream.php"},
}

# Headers to avoid 403 errors
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}


def fix_event(s: str) -> str:
    # Handle "Team1 @ Team2" format
    if " @" in s:
        return s.replace(" @ ", " vs ")
    return s


def clean_channel_name(name: str) -> str:
    """Clean channel/event name by removing emojis and cleaning special chars."""
    # Remove emojis
    emoji_pattern = re.compile(
        "["
        "\U0001F600-\U0001F64F"  # emoticons
        "\U0001F300-\U0001F5FF"  # symbols & pictographs
        "\U0001F680-\U0001F6FF"  # transport & map symbols
        "\U0001F1E0-\U0001F1FF"  # flags (iOS)
        "\U00002702-\U000027B0"
        "\U000024C2-\U0001F251"
        "\U0001F900-\U0001F9FF"  # supplemental symbols
        "\U0001FA70-\U0001FAFF"  # additional symbols
        "\U00002300-\U000023FF"  # misc symbols
        "\U00002600-\U000027FF"  # misc symbols
        "\U00002900-\U000029FF"  # arrows
        "\U00002B00-\U00002BFF"  # arrows
        "]+",
        flags=re.UNICODE,
    )
    name = emoji_pattern.sub("", name).strip()
    
    # Remove any remaining non-ASCII characters
    name = re.sub(r'[^\x00-\x7F]+', '', name).strip()
    
    # Clean up multiple spaces
    name = re.sub(r'\s+', ' ', name).strip()
    
    return name


def get_cloudscraper():
    """Get a cloudscraper instance."""
    return cloudscraper.create_scraper(
        browser={
            'browser': 'chrome',
            'platform': 'windows',
            'mobile': False
        },
        delay=1
    )


async def fetch_with_cloudscraper(url: str, max_retries: int = 3) -> str | None:
    """Fetch URL using cloudscraper to bypass Cloudflare protection."""
    scraper = get_cloudscraper()
    
    for attempt in range(max_retries):
        try:
            response = scraper.get(
                url,
                headers=HEADERS,
                timeout=30,
                allow_redirects=True
            )
            
            if response.status_code == 200:
                return response.text
            else:
                log.warning(f"Attempt {attempt + 1} for {url} returned status {response.status_code}")
                
        except Exception as e:
            log.warning(f"Attempt {attempt + 1} failed for {url}: {e}")
            
        if attempt < max_retries - 1:
            await asyncio.sleep(2 ** attempt)  # Exponential backoff
            
    return None


async def fetch_json_with_cloudscraper(url: str, params: dict = None, headers: dict = None) -> dict | None:
    """Fetch JSON using cloudscraper."""
    scraper = get_cloudscraper()
    
    try:
        req_headers = HEADERS.copy()
        if headers:
            req_headers.update(headers)
            
        response = scraper.get(
            url,
            headers=req_headers,
            params=params,
            timeout=30
        )
        
        if response.status_code == 200:
            return response.json()
        else:
            log.warning(f"JSON request to {url} returned status {response.status_code}")
            
    except Exception as e:
        log.warning(f"JSON request to {url} failed: {e}")
        
    return None


async def process_event(
    url: str,
    url_num: int,
    sport: str,
) -> str | None:

    log.info(f"URL {url_num}) Processing: {url}")
    
    # Fetch the event page
    html_content = await fetch_with_cloudscraper(url)
    if not html_content:
        log.warning(f"URL {url_num}) Failed to fetch event page")
        return

    soup = HTMLParser(html_content)

    # Look for iframe with name="srcFrame"
    iframe = soup.css_first('iframe[name="srcFrame"]')
    if not iframe:
        # Try alternative selectors
        iframe = soup.css_first('iframe[src*="stream"]')
        if not iframe:
            # Try to find the iframe in the player div
            player_div = soup.css_first('.player-header')
            if player_div:
                iframe = player_div.css_first('iframe')
    
    if not iframe:
        log.warning(f"URL {url_num}) No iframe element found.")
        return

    iframe_src = iframe.attributes.get("src")
    if not iframe_src or iframe_src.lower() == "about:blank":
        iframe_src = iframe.attributes.get("data-litespeed-src")
    
    if not iframe_src:
        log.warning(f"URL {url_num}) No iframe source found.")
        return

    log.info(f"URL {url_num}) Found iframe: {iframe_src}")

    # Fetch iframe content
    iframe_html = await fetch_with_cloudscraper(iframe_src)
    if not iframe_html:
        log.warning(f"URL {url_num}) Failed to fetch iframe content")
        return

    # Look for Clappr source pattern - try different patterns
    patterns = [
        r'var\s+\w*=\[([^"]*)\];',
        r'id\s*:\s*([0-9]+)',
        r'ts\s*:\s*([0-9]+)',
        r'pt\s*:\s*([0-9]+)',
    ]
    
    # Try to extract id, ts, pt from the iframe
    ev_id = None
    ev_ts = None
    ev_pt = None
    
    # Look for the array pattern first
    array_pattern = re.compile(r'var\s+\w*=\[([^"]*)\];', re.I)
    array_match = array_pattern.search(iframe_html)
    
    if array_match:
        try:
            values = ast.literal_eval(array_match[1])
            if len(values) >= 3:
                ev_id, ev_ts, ev_pt = values[:3]
                log.info(f"URL {url_num}) Found Clappr data: id={ev_id}, ts={ev_ts}, pt={ev_pt}")
        except (ValueError, SyntaxError) as e:
            log.warning(f"URL {url_num}) Failed to parse array: {e}")
    
    # If not found, try individual pattern matching
    if not all([ev_id, ev_ts, ev_pt]):
        # Look for id, ts, pt in the iframe content
        id_match = re.search(r'id\s*:\s*([0-9]+)', iframe_html)
        ts_match = re.search(r'ts\s*:\s*([0-9]+)', iframe_html)
        pt_match = re.search(r'pt\s*:\s*([0-9]+)', iframe_html)
        
        if id_match and ts_match and pt_match:
            ev_id = int(id_match.group(1))
            ev_ts = int(ts_match.group(1))
            ev_pt = int(pt_match.group(1))
            log.info(f"URL {url_num}) Found individual values: id={ev_id}, ts={ev_ts}, pt={ev_pt}")
    
    if not all([ev_id, ev_ts, ev_pt]):
        log.warning(f"URL {url_num}) Could not extract event id, ts, pt")
        return

    params = {
        "id": ev_id,
        "ts": ev_ts,
        "pt": ev_pt
    }

    # Make API request
    api_url = urljoin(BASE_URLS[sport]["base"], BASE_URLS[sport]["api"])
    log.info(f"URL {url_num}) Calling API: {api_url} with params {params}")
    
    api_data = await fetch_json_with_cloudscraper(
        api_url,
        params=params,
        headers={"Referer": iframe_src}
    )
    
    if not api_data:
        log.warning(f"URL {url_num}) API request failed")
        return

    if api_data.get("error"):
        log.warning(f"URL {url_num}) API error: {api_data.get('error')}")
        return

    m3u8 = api_data.get("url")
    if not m3u8:
        log.warning(f"URL {url_num}) No M3U8 found in response: {api_data}")
        return

    log.info(f"URL {url_num}) Captured M3U8: {m3u8[:100]}...")
    return m3u8


async def get_events() -> list[Event]:
    events: list[Event] = []
    
    for sport, config in BASE_URLS.items():
        base_url = config["base"]
        log.info(f"Fetching events from {base_url}")
        
        # Fetch the main page
        html_content = await fetch_with_cloudscraper(base_url)
        if not html_content:
            log.error(f"Failed to fetch {base_url}")
            continue
        
        soup = HTMLParser(html_content)
        
        # Look for game rows - they're in table rows with class "singele_match_date"
        rows = soup.css("tr.singele_match_date")
        
        for row in rows:
            # Skip header rows
            if row.css_first(".mdatetitle"):
                continue
                
            # Look for the team vs link
            vs_node = row.css_first("td.teamvs a")
            if not vs_node:
                continue
            
            # Get the event name
            event_name = vs_node.text(strip=True)
            
            # Get the href
            href = vs_node.attributes.get("href")
            if not href:
                continue
            
            # Fix the URL if it's relative
            if href.startswith("/"):
                href = urljoin(base_url, href)
            
            # Check if there's an HD button (indicates a stream is available)
            hd_button = row.css_first("td.hdplay_button a")
            if not hd_button:
                # If no HD button, this event might not have a stream
                # But we'll still process it
                pass
            
            # Clean up the event name
            # Remove date span if present
            date_nodes = vs_node.css("span.mtdate")
            for date_node in date_nodes:
                date = date_node.text(strip=True)
                event_name = event_name.replace(date, "").strip()
            
            # Fix the event name (Team1 @ Team2 -> Team1 vs Team2)
            event_name = fix_event(event_name)
            
            log.info(f"Found event: {event_name} - {href}")
            
            events.append(
                Event(
                    sport=sport,
                    name=event_name,
                    link=href,
                )
            )
        
        log.info(f"Found {len([e for e in events if e.sport == sport])} events for {sport}")
    
    return events


def write_outputs():
    """Write the VLC and TiviMate playlist files."""
    if not urls:
        log.warning("No URLs to write to output files")
        return
    
    vlc_lines = ["#EXTM3U"]
    tivi_lines = ["#EXTM3U"]
    channel_counter = 1
    
    # Sort URLs by key for consistent ordering
    sorted_urls = sorted(urls.items())
    
    for key, data in sorted_urls:
        if not data.get("source"):
            continue
        
        # Extract sport from key
        sport_match = re.search(r'\[([^\]]+)\]', key)
        sport = sport_match.group(1) if sport_match else "Live Event"
        
        # Clean channel name
        clean_name = clean_channel_name(key)
        
        # Get logo and tvg-id
        logo = data.get("logo", "")
        tvg_id = data.get("tvg-id", "Live.Event.us")
        referer = data.get("refer", "")
        
        # Build EXTINF line
        extinf = (
            f'#EXTINF:-1 tvg-chno="{channel_counter}" '
            f'tvg-id="{tvg_id}" '
            f'tvg-name="{clean_name}" '
            f'tvg-logo="{logo}" '
            f'group-title="{sport}",{clean_name}'
        )
        
        # VLC format
        vlc_lines.append(extinf)
        vlc_lines.append(f"#EXTVLCOPT:http-referrer={referer}")
        vlc_lines.append(f"#EXTVLCOPT:http-origin={referer}")
        vlc_lines.append(f"#EXTVLCOPT:http-user-agent={UA_RAW}")
        vlc_lines.append(data["source"])
        
        # TiviMate format
        tivi_lines.append(extinf)
        url_with_params = (
            f"{data['source']}"
            f"|referer={referer}/"
            f"|origin={referer}/"
            f"|user-agent={UA_ENC}"
        )
        tivi_lines.append(url_with_params)
        
        channel_counter += 1
    
    # Write VLC output
    OUT_VLC.write_text("\n".join(vlc_lines) + "\n", encoding="utf-8")
    log.info(f"Written {OUT_VLC} with {channel_counter - 1} channels")
    
    # Write TiviMate output
    OUT_TIVI.write_text("\n".join(tivi_lines) + "\n", encoding="utf-8")
    log.info(f"Written {OUT_TIVI} with {channel_counter - 1} channels")


async def scrape() -> None:
    if cached_urls := CACHE_FILE.load():
        urls.update({k: v for k, v in cached_urls.items() if v["source"]})
        log.info(f"Loaded {len(urls)} event(s) from cache")
        return

    base_urls_str = " & ".join(i["base"] for i in BASE_URLS.values())
    log.info(f'Scraping from "{base_urls_str}"')

    if events := await get_events():
        log.info(f"Processing {len(events)} URL(s)")

        now = Time.clean(Time.now())
        cached_urls = {}

        for i, ev in enumerate(events, start=1):
            handler = partial(
                process_event,
                url=ev.link,
                url_num=i,
                sport=ev.sport,
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
                "logo": logo,
                "refer": BASE_URLS[ev.sport]["base"],
                "timestamp": now.timestamp(),
                "tvg-id": tvg_id or "Live.Event.us",
                "link": ev.link,
            }

            cached_urls[key] = entry

            if source:
                urls[key] = entry

        log.info(f"Collected and cached {len(urls)} event(s)")
        CACHE_FILE.write(cached_urls)
    else:
        log.info("No events found")


async def main():
    """Main function to run the scraper and generate outputs."""
    log.info("Starting CAST scraper...")
    
    # Scrape or load from cache
    await scrape()
    
    # Generate output files
    write_outputs()
    
    log.info("CAST scraper completed successfully")


if __name__ == "__main__":
    asyncio.run(main())
