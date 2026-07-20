import asyncio
import os
import urllib.parse
import re
import json
from functools import partial
from urllib.parse import urljoin

from playwright.async_api import Browser

from utils import Cache, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "STFREE"

CACHE_FILE = Cache(TAG, exp=10_800)

API_CACHE = Cache(f"{TAG}-api", exp=19_800)

# Get API_URL from environment variable (secret) with validation
API_URL = os.environ.get("STRM_FREE_API_URL")
# Ensure URL has protocol
if API_URL and not API_URL.startswith(('http://', 'https://')):
    API_URL = f"https://{API_URL}"

# Constants for output files
VLC_OUTPUT_FILE = "strmfree_vlc.m3u8"
TIVIMATE_OUTPUT_FILE = "strmfree_tivimate.m3u8"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
REFERER = "https://streamfree.top/"
ORIGIN = "https://streamfree.top"

BASE_URL = "https://streamfree.top"

def encode_user_agent(user_agent: str) -> str:
    """Encode user agent for TiviMate format"""
    return urllib.parse.quote(user_agent)

def generate_output_files():
    """Generate both VLC and TiviMate M3U8 files"""
    if not urls:
        log.info("No URLs to write to output files")
        return
    
    log.info(f"Generating output files with {len(urls)} events")
    
    # Generate VLC format
    vlc_content = "#EXTM3U\n"
    tivimate_content = "#EXTM3U\n"
    
    # Sort by timestamp to maintain order
    sorted_urls = sorted(urls.items(), key=lambda x: x[1].get("timestamp", 0))
    
    chno = 1  # Start channel number from 1
    for key, data in sorted_urls:
        if not data.get("url"):
            continue
            
        # Extract data
        sport_match = key.split("[")[1].split("]")[0] if "[" in key else "Live Events"
        sport = sport_match
        event_name = key.split("]")[-1].strip().replace(f"({TAG})", "").strip() if "]" in key else key
        logo = data.get("logo", "")
        tvg_id = data.get("id", "Live.Event.us")
        url = data.get("url", "")
        link = data.get("link", "")
        
        # Keep the full URL with token parameters
        full_url = url
        
        # Skip if no URL
        if not full_url:
            continue
        
        # For VLC referer, use the player page URL which contains the channel info
        vlc_referer = link if link else REFERER
        
        # EXTINF line (same for both formats)
        extinf = f'#EXTINF:-1 tvg-chno="{chno}" tvg-id="{tvg_id}" tvg-name="{key}" tvg-logo="{logo}" group-title="{sport}",{event_name}\n'
        
        # VLC format
        vlc_content += extinf
        vlc_content += f"#EXTVLCOPT:http-referrer={vlc_referer}\n"
        vlc_content += f"#EXTVLCOPT:http-origin={ORIGIN}\n"
        vlc_content += f"#EXTVLCOPT:http-user-agent={USER_AGENT}\n"
        vlc_content += f"{full_url}\n\n"
        
        # TiviMate format (with pipe and encoded user agent)
        encoded_ua = encode_user_agent(USER_AGENT)
        tivimate_url = f"{full_url}|referer={REFERER}|origin={ORIGIN}|user-agent={encoded_ua}"
        
        tivimate_content += extinf
        tivimate_content += f"{tivimate_url}\n\n"
        
        chno += 1
    
    # Write VLC file
    try:
        with open(VLC_OUTPUT_FILE, "w", encoding="utf-8") as f:
            f.write(vlc_content)
        log.info(f"Successfully wrote {VLC_OUTPUT_FILE} with {chno-1} events")
    except Exception as e:
        log.error(f"Error writing {VLC_OUTPUT_FILE}: {e}")
    
    # Write TiviMate file
    try:
        with open(TIVIMATE_OUTPUT_FILE, "w", encoding="utf-8") as f:
            f.write(tivimate_content)
        log.info(f"Successfully wrote {TIVIMATE_OUTPUT_FILE} with {chno-1} events")
    except Exception as e:
        log.error(f"Error writing {TIVIMATE_OUTPUT_FILE}: {e}")

async def get_categories() -> list[str]:
    """Fetch available categories from the API."""
    api_url = f"{BASE_URL}/api/v1/categories"
    log.info(f"Fetching categories from: {api_url}")
    
    if r := await network.request(api_url, log=log, headers={"User-Agent": USER_AGENT}):
        try:
            data = r.json()
            categories = data.get("categories", [])
            log.info(f"Found {len(categories)} categories: {categories}")
            return categories
        except Exception as e:
            log.error(f"Error parsing categories: {e}")
    
    # Fallback to hardcoded list if API fails
    log.warning("Using fallback category list")
    return ["soccer", "basketball", "hockey", "combat", "baseball", "football", "racing", "tennis", "cricket"]

async def get_events(cached_keys: list[str]) -> list[dict[str, str]]:
    """Fetch events from all categories and normalize them."""
    now = Time.clean(Time.now())
    
    events = []
    
    # Get categories dynamically
    categories = await get_categories()
    if not categories:
        log.error("No categories available")
        return events
    
    log.info(f"Fetching streams from {len(categories)} categories...")
    
    # Fetch streams from each category
    for category in categories:
        api_url = f"{BASE_URL}/api/v1/streams?category={category}"
        log.info(f"Fetching from API: {api_url}")
        
        if r := await network.request(
            api_url,
            log=log,
            headers={
                "Referer": REFERER,
                "Origin": ORIGIN,
                "User-Agent": USER_AGENT
            }
        ):
            try:
                data = r.json()
                streams = data.get("streams", [])
                
                if streams:
                    log.info(f"API returned {len(streams)} streams for category: {category}")
                else:
                    log.debug(f"No streams found for category: {category}")
                    continue
                
                # Process each stream
                for stream in streams:
                    try:
                        # Extract metadata
                        name = stream.get("name", "")
                        if not name:
                            continue
                        
                        league = stream.get("league", category.capitalize())
                        embed_url = stream.get("embed_url", "")
                        stream_key = stream.get("stream_key", "")
                        thumbnail = stream.get("thumbnail_url", "")
                        timestamp = stream.get("match_timestamp", now.timestamp())
                        
                        if not embed_url or not stream_key:
                            log.debug(f"Skipping {name}: missing embed_url or stream_key")
                            continue
                        
                        # Create event key
                        key = f"[{league}] {name} ({TAG})"
                        
                        if key in cached_keys:
                            log.debug(f"Event already in cache: {key}")
                            continue
                        
                        # Parse event time
                        event_dt = now
                        try:
                            event_dt = Time.from_timestamp(timestamp)
                        except Exception as e:
                            log.debug(f"Could not parse timestamp for {name}: {e}")
                        
                        # Add to events list
                        events.append({
                            "sport": league,
                            "event": name,
                            "link": embed_url,  # Use embed_url as the link to process
                            "timestamp": event_dt.timestamp(),
                            "logo": thumbnail,
                            "stream_key": stream_key,
                            "category": category
                        })
                        
                        log.info(f"Found new event: {key} at {event_dt}")
                        
                    except Exception as e:
                        log.error(f"Error processing stream: {e}")
                        continue
                        
            except Exception as e:
                log.error(f"Error parsing API response for {category}: {e}")
                continue
    
    log.info(f"Total new events found: {len(events)}")
    return events

async def capture_m3u8_from_embed(browser: Browser, stream_key: str, embed_url: str, timeout: int = 30) -> str | None:
    """
    Capture M3U8 URL by following the player's flow:
    1. Load the embed page
    2. Extract the nonce from the page
    3. Get the stream key from the API
    4. Construct the M3U8 URL with token parameters
    """
    log.debug(f"Capturing M3U8 for stream_key: {stream_key}")
    
    try:
        # Step 1: Load the embed page to get the nonce
        async with network.event_context(browser) as context:
            async with network.event_page(context) as page:
                # Navigate to the embed URL
                log.debug(f"Loading embed page: {embed_url}")
                await page.goto(embed_url, timeout=15000, wait_until="domcontentloaded")
                
                # Wait for page to load
                await page.wait_for_timeout(3000)
                
                # Step 2: Extract the nonce from the page
                # The nonce is in the JavaScript: const NONCE = 'xxxxx';
                nonce = await page.evaluate('''
                    () => {
                        const script = document.querySelector('script');
                        const match = script.textContent.match(/const NONCE = ['"]([^'"]+)['"]/);
                        return match ? match[1] : null;
                    }
                ''')
                
                if not nonce:
                    log.warning("Could not extract nonce from page")
                    # Try to get from page source as fallback
                    html = await page.content()
                    match = re.search(r"const NONCE = ['\"]([^'\"]+)['\"]", html)
                    if match:
                        nonce = match.group(1)
                
                if not nonce:
                    log.warning("Could not extract nonce, using default")
                    # Generate a random nonce as fallback (the player uses this format)
                    import random
                    import string
                    nonce = ''.join(random.choices(string.ascii_lowercase + string.digits, k=16))
                
                log.debug(f"Extracted NONCE: {nonce}")
                
                # Step 3: Get the stream key data from the API
                # The player calls: /get-stream-key/{stream_key}
                stream_key_url = f"{BASE_URL}/get-stream-key/{stream_key}"
                log.debug(f"Fetching stream key data: {stream_key_url}")
                
                if r := await network.request(
                    stream_key_url,
                    log=log,
                    headers={
                        "Referer": embed_url,
                        "Origin": ORIGIN,
                        "User-Agent": USER_AGENT
                    }
                ):
                    try:
                        stream_data = r.json()
                        server_name = stream_data.get("server_name", "origin")
                        is_external = stream_data.get("is_external", False)
                        external_url = stream_data.get("external_url", "")
                        
                        # Step 4: Construct the M3U8 URL
                        # Based on the player code:
                        # let url = serverName !== 'origin'
                        #     ? `/live-cdn/{stream_key}{quality}/index.m3u8`
                        #     : `/live/{stream_key}{quality}/index.m3u8`;
                        
                        # Use 720p as default quality
                        quality_suffix = "720p"
                        
                        # Determine if using CDN or origin
                        if server_name != "origin":
                            m3u8_path = f"/live-cdn/{stream_key}{quality_suffix}/index.m3u8"
                        else:
                            m3u8_path = f"/live/{stream_key}{quality_suffix}/index.m3u8"
                        
                        # Add token parameters using the nonce and timestamp
                        # The player uses: _t, _e, _n where _n is the nonce
                        # _e is expiration timestamp, _t is token
                        # For the token, we need to get it from the stream data or generate
                        
                        # Try to get token from stream data first
                        token = stream_data.get("token")
                        expiration = stream_data.get("expiration")
                        
                        if not token or not expiration:
                            # If not in stream data, try to get from the player's _0x object
                            # The player has: _0x = {"720p": {"_e": ..., "_n": ..., "_t": ...}}
                            # We can try to extract this from the page
                            token_data = await page.evaluate(f'''
                                () => {{
                                    // Try to get the _0x object from the page
                                    if (typeof _0x !== 'undefined' && _0x['720p']) {{
                                        return _0x['720p'];
                                    }}
                                    return null;
                                }}
                            ''')
                            
                            if token_data:
                                token = token_data.get('_t')
                                expiration = token_data.get('_e')
                                if not nonce:
                                    nonce = token_data.get('_n')
                        
                        # Construct the full M3U8 URL
                        if token and expiration and nonce:
                            m3u8_url = f"{BASE_URL}{m3u8_path}?_t={token}&_e={expiration}&_n={nonce}"
                        else:
                            # Fallback: use the path without tokens (may not work)
                            m3u8_url = f"{BASE_URL}{m3u8_path}"
                            log.warning(f"Using M3U8 URL without tokens: {m3u8_url}")
                        
                        log.debug(f"Constructed M3U8 URL: {m3u8_url}")
                        
                        # Validate the URL by checking if it's accessible
                        if r := await network.request(
                            m3u8_url,
                            log=log,
                            headers={
                                "Referer": embed_url,
                                "Origin": ORIGIN,
                                "User-Agent": USER_AGENT
                            }
                        ):
                            if r.status == 200:
                                log.info(f" Successfully captured M3U8 for {stream_key}")
                                return m3u8_url
                            else:
                                log.warning(f"M3U8 URL returned status {r.status}: {m3u8_url}")
                        
                        return m3u8_url
                        
                    except Exception as e:
                        log.error(f"Error processing stream data: {e}")
                
                # Step 5: Fallback - try to extract M3U8 from page source
                log.debug("Attempting fallback: extracting M3U8 from page source")
                html = await page.content()
                
                # Look for m3u8 patterns in the page source
                patterns = [
                    r'(https://streamfree\.top/live-cdn/[^"\s]+\.m3u8[^"\s]*)',
                    r'(https://streamfree\.top/live/[^"\s]+\.m3u8[^"\s]*)',
                    r'src="(https://streamfree\.top/live-cdn/[^"]+\.m3u8[^"]*)"',
                    r'"(https://streamfree\.top/live-cdn/[^"]+\.m3u8[^"]*)"',
                ]
                
                for pattern in patterns:
                    match = re.search(pattern, html)
                    if match:
                        m3u8_url = match.group(1)
                        log.debug(f"Found M3U8 in page source: {m3u8_url}")
                        return m3u8_url
                
                # Step 6: Try network request interception
                log.debug("Attempting fallback: network request interception")
                m3u8_future = asyncio.Future()
                
                def on_request(request):
                    url = request.url
                    if "streamfree.top" in url and ".m3u8" in url:
                        if not m3u8_future.done():
                            m3u8_future.set_result(url)
                            log.debug(f"Captured M3U8 from network: {url}")
                
                page.on("request", on_request)
                
                # Click play button to trigger the request
                try:
                    play_button = await page.query_selector('button[aria-label*="play" i], .vjs-big-play-button, video')
                    if play_button:
                        await play_button.click()
                        log.debug("Clicked play button")
                except Exception:
                    pass
                
                try:
                    m3u8_url = await asyncio.wait_for(m3u8_future, timeout=timeout)
                    return m3u8_url
                except asyncio.TimeoutError:
                    log.debug("No M3U8 found in network requests")
                finally:
                    page.remove_listener("request", on_request)
                
                log.warning(f"No M3U8 captured for {stream_key}")
                return None
                
    except Exception as e:
        log.error(f"Error capturing M3U8: {e}")
        return None

async def scrape(browser: Browser) -> None:
    """Main scraping function"""
    # Load cached URLs
    cached_urls = CACHE_FILE.load() or {}
    
    cached_count = len(cached_urls)
    
    # Update global urls with cached ones
    urls.update(cached_urls)
    
    log.info(f"Loaded {cached_count} event(s) from cache")
    log.info(f'Scraping from "{API_URL}"')
    
    if events := await get_events(list(cached_urls.keys())):
        log.info(f"Processing {len(events)} new URL(s)")
        
        for i, ev in enumerate(events, start=1):
            log.info(f"Processing event {i}/{len(events)}: {ev['sport']} - {ev['event']}")
            
            # Get the embed URL and stream key
            embed_url = ev["link"]
            stream_key = ev.get("stream_key", "")
            
            if not stream_key:
                log.warning(f"No stream_key for event: {ev['event']}")
                continue
            
            log.debug(f"Stream key: {stream_key}")
            log.debug(f"Embed URL: {embed_url}")
            
            # Capture M3U8 using the stream key
            m3u8_url = await capture_m3u8_from_embed(browser, stream_key, embed_url)
            
            if m3u8_url:
                sport, event, ts = (
                    ev["sport"],
                    ev["event"],
                    ev["timestamp"],
                )
                
                key = f"[{sport}] {event} ({TAG})"
                
                tvg_id, logo = leagues.get_tvg_info(sport, event)
                
                # Use logo from API if available
                final_logo = ev.get("logo", logo) if ev.get("logo") else logo
                final_id = tvg_id or f"{ev.get('category', 'sport')}.{ev.get('stream_key', 'event')}"
                
                # Keep the full URL with token
                full_url = m3u8_url
                
                entry = {
                    "url": full_url,  # Store the full URL with token
                    "logo": final_logo,
                    "base": REFERER,
                    "timestamp": ts,
                    "id": final_id,
                    "link": embed_url,  # Store the original embed URL for referer
                }
                
                urls[key] = cached_urls[key] = entry
                log.info(f"Successfully added URL for: {key}")
            else:
                log.warning(f"Failed to get URL for event: {ev['sport']} - {ev['event']}")
            
            # Small delay between requests
            await asyncio.sleep(1)
        
        log.info(f"Collected and cached {len(cached_urls) - cached_count} new event(s)")
    
    else:
        log.info("No new events found")
    
    # Save updated cache
    CACHE_FILE.write(cached_urls)
    
    # Generate output files
    generate_output_files()

async def main():
    """Main function to run the updater"""
    log.info("Starting STRFREE updater")
    
    # Validate API_URL
    if not API_URL or API_URL == "None":
        log.error("STRM_FREE_API_URL environment variable is not set correctly")
        return
    
    log.info(f"Using API URL: {API_URL}")
    
    from playwright.async_api import async_playwright
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            await scrape(browser)
        finally:
            await browser.close()
    
    log.info("STRFREE updater completed")

def run():
    """Synchronous entry point for the updater"""
    asyncio.run(main())

if __name__ == "__main__":
    run()
