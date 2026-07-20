import asyncio
import os
import urllib.parse
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
        
        async with network.event_context(browser) as context:
            for i, ev in enumerate(events, start=1):
                async with network.event_page(context) as page:
                    log.info(f"Processing event {i}/{len(events)}: {ev['sport']} - {ev['event']}")
                    
                    # The link is the embed URL
                    link = ev["link"]
                    
                    # Navigate to the embed page first
                    await page.goto(link, wait_until="domcontentloaded")
                    await page.wait_for_timeout(3000)  # Wait for player to load
                    
                    # Try to find and click play button
                    try:
                        # Try different selectors for the play button
                        play_selectors = [
                            'button[aria-label*="play" i]',
                            'button[aria-label*="Play"]',
                            '.vjs-big-play-button',
                            '.play-button',
                            'button:has-text("Play")',
                            '[role="button"]:has-text("Play")',
                            '.btn-play',
                            '#play-button',
                            'video'
                        ]
                        
                        play_button = None
                        for selector in play_selectors:
                            try:
                                play_button = await page.query_selector(selector)
                                if play_button:
                                    await play_button.click()
                                    log.debug(f"Clicked play button with selector: {selector}")
                                    await page.wait_for_timeout(2000)
                                    break
                            except Exception:
                                continue
                        
                        # If no play button found, try clicking the page
                        if not play_button:
                            await page.click('body')
                            await page.wait_for_timeout(2000)
                            
                    except Exception as e:
                        log.debug(f"Error clicking play: {e}")
                    
                    # Now process the event using network.process_event
                    handler = partial(
                        network.process_event,
                        url=link,  # Pass the embed URL
                        url_num=i,
                        page=page,
                        log=log,
                        timeout=30,  # Increased timeout for stream loading
                    )
                    
                    # Get the full URL with token from the event page
                    url = await network.safe_process(
                        handler,
                        url_num=i,
                        semaphore=network.PW_S,
                        log=log,
                    )
                    
                    if url:
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
                        full_url = url
                        
                        entry = {
                            "url": full_url,  # Store the full URL with token
                            "logo": final_logo,
                            "base": REFERER,
                            "timestamp": ts,
                            "id": final_id,
                            "link": link,  # Store the original embed URL for referer
                        }
                        
                        urls[key] = cached_urls[key] = entry
                        log.info(f"Successfully added URL for: {key}")
                    else:
                        log.warning(f"Failed to get URL for event: {ev['sport']} - {ev['event']}")
        
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
