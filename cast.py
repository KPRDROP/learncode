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
    return " vs ".join(s.split("@"))


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


async def fetch_with_cloudscraper(url: str, max_retries: int = 3) -> str | None:
    """Fetch URL using cloudscraper to bypass Cloudflare protection."""
    scraper = cloudscraper.create_scraper(
        browser={
            'browser': 'chrome',
            'platform': 'windows',
            'mobile': False
        }
    )
    
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


async def fetch_with_retry(url: str, url_num: int, max_retries: int = 3) -> any:
    """Fetch URL with retries and cloudscraper."""
    for attempt in range(max_retries):
        try:
            # First try with cloudscraper
            html_content = await fetch_with_cloudscraper(url, 1)
            if html_content:
                # Create a mock response object
                class MockResponse:
                    def __init__(self, content):
                        self.content = content
                        self.text = content
                        self.url = url
                        self.status_code = 200
                
                return MockResponse(html_content)
            
        except Exception as e:
            log.warning(f"Attempt {attempt + 1} failed for {url}: {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)
            continue
    return None


async def process_event(
    url: str,
    url_num: int,
    sport: str,
) -> str | None:

    # Fetch the event page with cloudscraper
    html_content = await fetch_with_cloudscraper(url)
    if not html_content:
        return

    soup = HTMLParser(html_content)

    # Look for iframe with name="srcFrame"
    iframe = soup.css_first('iframe[name="srcFrame"]')
    if not iframe:
        # Try alternative selectors
        iframe = soup.css_first('iframe[src*="stream"]')
    
    if not iframe:
        log.warning(f"URL {url_num}) No iframe element found.")
        return

    iframe_src = iframe.attributes.get("src")
    if not iframe_src or iframe_src.lower() == "about:blank":
        iframe_src = iframe.attributes.get("data-litespeed-src")
    
    if not iframe_src:
        log.warning(f"URL {url_num}) No iframe source found.")
        return

    # Fetch iframe content with cloudscraper
    iframe_html = await fetch_with_cloudscraper(iframe_src)
    if not iframe_html:
        return

    # Look for Clappr source pattern
    pattern = re.compile(r'var\s+\w*=\[([^"]*)\];', re.I)
    match = pattern.search(iframe_html)
    
    if not match:
        log.warning(f"URL {url_num}) No Clappr source found.")
        return

    try:
        ev_id, ev_ts, ev_pt = ast.literal_eval(match[1])
    except (ValueError, SyntaxError) as e:
        log.warning(f"URL {url_num}) Failed to parse event info: {e}")
        return

    params: dict[str, int | str] = dict(zip(["id", "ts", "pt"], [ev_id, ev_ts, ev_pt]))

    # Make API request with cloudscraper
    api_url = urljoin(BASE_URLS[sport]["base"], BASE_URLS[sport]["api"])
    
    scraper = cloudscraper.create_scraper(
        browser={
            'browser': 'chrome',
            'platform': 'windows',
            'mobile': False
        }
    )
    
    try:
        response = scraper.get(
            api_url,
            headers={"Referer": iframe_src, "User-Agent": HEADERS["User-Agent"]},
            params=params,
            timeout=30
        )
        
        if response.status_code != 200:
            log.warning(f"URL {url_num}) API request failed with status {response.status_code}")
            return
            
        data = response.json()
        
    except Exception as e:
        log.warning(f"URL {url_num}) API request failed: {e}")
        return

    if data.get("error"):
        log.warning(f"URL {url_num}) API error: {data.get('error')}")
        return

    m3u8 = data.get("url")
    if not m3u8:
        log.warning(f"URL {url_num}) No M3U8 found.")
        return

    log.info(f"URL {url_num}) Captured M3U8")
    return m3u8


async def get_events() -> list[Event]:
    events: list[Event] = []
    
    for sport, config in BASE_URLS.items():
        base_url = config["base"]
        log.info(f"Fetching events from {base_url}")
        
        # Fetch the main page with cloudscraper
        html_content = await fetch_with_cloudscraper(base_url)
        if not html_content:
            log.error(f"Failed to fetch {base_url}")
            continue
        
        soup = HTMLParser(html_content)
        
        # Look for game rows
        # The HTML uses class "singele_match_date" for game rows
        rows = soup.css("tr.singele_match_date")
        
        for row in rows:
            # Look for the team vs link
            vs_node = row.css_first("td.teamvs a")
            if not vs_node:
                continue
            
            # Get the event name
            event_name = vs_node.text(strip=True)
            
            # Remove date if present
            date_nodes = vs_node.css("span.mtdate")
            for date_node in date_nodes:
                date = date_node.text(strip=True)
                event_name = event_name.replace(date, "").strip()
            
            # Get the href
            href = vs_node.attributes.get("href")
            if not href:
                continue
            
            # Fix the URL if it's relative
            if href.startswith("/"):
                href = urljoin(base_url, href)
            
            # Clean up the event name
            event_name = fix_event(event_name)
            
            events.append(
                Event(
                    sport=sport,
                    name=event_name,
                    link=href,
                )
            )
        
        log.info(f"Found {len(rows)} events for {sport}")
    
    return events


def write_outputs():
    """Write the VLC and TiviMate playlist files."""
    if not urls:
        log.warning("No URLs to write to output files")
        return
    
    vlc_lines = ["#EXTM3U"]
    tivi_lines = ["#EXTM3U"]
    channel_counter = 1
    
    for key, data in urls.items():
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
