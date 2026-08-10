import ast
import asyncio
import re
import base64
from functools import partial
from pathlib import Path
from urllib.parse import urljoin, quote
from bs4 import BeautifulSoup

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
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
    "MLB": {"base": "https://mlbwebcast.com", "api": "stream/check_stream.php", "pattern": "/{team}-live"},
    "NFL": {"base": "https://nflwebcast.com", "api": "live/check_stream.php", "pattern": "/{team}-live-stream-online-free/"},
}

# Headers for cloudscraper
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
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


def find_event_links_from_homepage(html: str, base: str) -> list:
    """Extract event links from homepage HTML."""
    soup = BeautifulSoup(html, "html.parser")
    links = []

    # Look for team links in the team logo section
    for a in soup.select(".team-logo a"):
        href = a.get("href")
        if not href:
            continue
        href = urljoin(base, href)
        title = a.get("title", "")
        links.append((href, title))

    # Look for event links in the table
    for a in soup.select("td.teamvs a"):
        href = a.get("href")
        if not href:
            continue
        href = urljoin(base, href)
        text = a.text.strip()
        # Remove date if present
        date_span = a.find("span", class_="mtdate")
        if date_span:
            text = text.replace(date_span.text, "").strip()
        links.append((href, text))

    return links


async def capture_m3u8_from_page(page_url: str, timeout_ms: int = 30000) -> tuple[str | None, str | None]:
    """Capture m3u8 URL from a page using Playwright."""
    captured = None
    page_html = None
    
    async with async_playwright() as p:
        browser = await p.firefox.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 720}
        )
        page = await context.new_page()

        def handle_response(response):
            nonlocal captured
            try:
                url = response.url
                if ".m3u8" in url and not captured:
                    captured = url
                    log.info(f"Captured m3u8 from response: {url[:100]}...")
            except Exception:
                pass

        try:
            page.on("response", handle_response)
            
            # Navigate to the page
            try:
                await page.goto(page_url, wait_until="domcontentloaded", timeout=timeout_ms)
            except PlaywrightTimeoutError:
                log.warning(f"Timeout loading {page_url}, continuing...")
            except Exception as e:
                log.warning(f"Error navigating {page_url}: {e}")

            # Wait a bit for network requests
            await asyncio.sleep(3)

            # Try to click on any play buttons
            try:
                # Look for play button or iframe
                for selector in [
                    ".btn-primary",
                    ".hdplay_button a",
                    ".lplay_button a",
                    ".play-button",
                    "button:has-text('Play')",
                    "button:has-text('Watch')",
                    "a:has-text('Watch')",
                    "a:has-text('HD')"
                ]:
                    try:
                        element = await page.locator(selector).first
                        if await element.count() > 0:
                            await element.click(timeout=2000)
                            await asyncio.sleep(2)
                            break
                    except Exception:
                        continue
            except Exception:
                pass

            # Try to find iframe and get its src
            try:
                iframes = await page.query_selector_all("iframe")
                for iframe in iframes:
                    src = await iframe.get_attribute("src")
                    if src and "stream" in src:
                        # Navigate to iframe src
                        try:
                            await page.goto(src, wait_until="domcontentloaded", timeout=10000)
                            await asyncio.sleep(2)
                        except Exception:
                            pass
            except Exception:
                pass

            # Get page content
            page_html = await page.content()

            # Search for m3u8 in page content
            if not captured:
                # Look for m3u8 URL patterns
                m3u8_patterns = [
                    r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*',
                    r'https?://[^\s"\'<>]+/play/[^\s"\'<>]+\.m3u8',
                    r'https?://webcast-origin[^\s"\'<>]+\.m3u8',
                ]
                for pattern in m3u8_patterns:
                    matches = re.findall(pattern, page_html)
                    if matches:
                        captured = matches[0]
                        log.info(f"Found m3u8 in page content: {captured[:100]}...")
                        break

            # Try base64 decoding
            if not captured:
                b64_candidates = set(re.findall(r'["\']([A-Za-z0-9+/=]{40,200})["\']', page_html))
                for candidate in b64_candidates:
                    try:
                        decoded = base64.b64decode(candidate).decode(errors="ignore")
                        if ".m3u8" in decoded:
                            captured = decoded.strip()
                            log.info("Found m3u8 from base64 decoding")
                            break
                    except Exception:
                        continue

            # Look for Clappr data
            if not captured:
                clappr_pattern = re.compile(r'var\s+\w*=\[([^"]*)\];', re.I)
                match = clappr_pattern.search(page_html)
                if match:
                    try:
                        values = ast.literal_eval(match[1])
                        if len(values) >= 3:
                            ev_id, ev_ts, ev_pt = values[:3]
                            # This is a fallback - the API call might be needed
                            log.info(f"Found Clappr data: id={ev_id}")
                    except Exception:
                        pass

        except Exception as e:
            log.error(f"Error in capture_m3u8_from_page: {e}")
        finally:
            await browser.close()

    return captured, page_html


async def process_event(
    url: str,
    url_num: int,
    sport: str,
) -> str | None:

    log.info(f"URL {url_num}) Processing: {url}")
    
    # Try Playwright first to capture the m3u8
    m3u8, html_content = await capture_m3u8_from_page(url)
    
    if m3u8:
        log.info(f"URL {url_num}) Successfully captured M3U8")
        return m3u8
    
    # If Playwright fails, try the traditional method
    log.warning(f"URL {url_num}) Playwright capture failed, trying fallback method")
    
    try:
        import cloudscraper
        scraper = cloudscraper.create_scraper(
            browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False}
        )
        
        response = scraper.get(url, headers=HEADERS, timeout=30)
        if response.status_code != 200:
            return None
            
        soup = HTMLParser(response.text)
        
        # Look for iframe
        iframe = soup.css_first('iframe[name="srcFrame"]')
        if iframe:
            iframe_src = iframe.attributes.get("src")
            if iframe_src and iframe_src != "about:blank":
                # Fetch iframe content
                iframe_response = scraper.get(iframe_src, headers=HEADERS, timeout=30)
                if iframe_response.status_code == 200:
                    # Look for Clappr data in iframe
                    clappr_pattern = re.compile(r'var\s+\w*=\[([^"]*)\];', re.I)
                    match = clappr_pattern.search(iframe_response.text)
                    if match:
                        try:
                            values = ast.literal_eval(match[1])
                            if len(values) >= 3:
                                ev_id, ev_ts, ev_pt = values[:3]
                                
                                # Make API request
                                api_url = urljoin(BASE_URLS[sport]["base"], BASE_URLS[sport]["api"])
                                api_response = scraper.get(
                                    api_url,
                                    params={"id": ev_id, "ts": ev_ts, "pt": ev_pt},
                                    headers={"Referer": iframe_src},
                                    timeout=30
                                )
                                
                                if api_response.status_code == 200:
                                    data = api_response.json()
                                    if not data.get("error") and data.get("url"):
                                        return data["url"]
                        except Exception as e:
                            log.warning(f"URL {url_num}) Fallback method failed: {e}")
    except Exception as e:
        log.warning(f"URL {url_num}) Fallback method error: {e}")
    
    return None


async def get_events() -> list[Event]:
    events: list[Event] = []
    
    for sport, config in BASE_URLS.items():
        base_url = config["base"]
        log.info(f"Fetching events from {base_url}")
        
        try:
            import cloudscraper
            scraper = cloudscraper.create_scraper(
                browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False}
            )
            
            response = scraper.get(base_url, headers=HEADERS, timeout=30)
            if response.status_code != 200:
                log.error(f"Failed to fetch {base_url}: Status {response.status_code}")
                continue
                
            # Find event links
            links = find_event_links_from_homepage(response.text, base_url)
            
            for href, title in links:
                if not href or not title:
                    continue
                    
                # Clean up the title
                event_name = fix_event(title)
                
                # Remove any "Live Stream" suffix
                event_name = re.sub(r'\s*Live\s*Stream\s*$', '', event_name, flags=re.I)
                event_name = re.sub(r'\s*Stream\s*$', '', event_name, flags=re.I)
                
                log.info(f"Found event: {event_name} - {href}")
                
                events.append(
                    Event(
                        sport=sport,
                        name=event_name,
                        link=href,
                    )
                )
                
        except Exception as e:
            log.error(f"Error fetching events from {base_url}: {e}")
        
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
