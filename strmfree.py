import os
import re
import json
import time
import asyncio
from functools import partial
from urllib.parse import quote

from playwright.async_api import Browser, async_playwright
from utils import Cache, Time, get_logger, leagues, network

# ================= CONFIG =================
log = get_logger(__name__)

SOURCE_URL = os.environ.get("STRM_FREE_API_URL")
OUTPUT_FILE = "strmfree_tivimate.m3u8"

BASE_URL = "https://streamfree.top"
TAG = "STFREE"
CACHE_FILE = Cache(TAG, exp=19_800)
API_CACHE = Cache(f"{TAG}-api", exp=19_800)

USER_AGENT_RAW = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.0.0 Safari/537.36"
)
USER_AGENT = quote(USER_AGENT_RAW, safe="")

# ===========================================

def normalize_stream(stream: dict) -> dict | None:
    """Normalize stream data from API into a consistent structure."""
    name = stream.get("name")
    category = stream.get("category")
    stream_key = stream.get("stream_key")
    embed_url = stream.get("embed_url")
    
    # Validate required fields
    if not all([name, category, stream_key, embed_url]):
        log.debug(f"Skipping stream {name}: missing required fields")
        return None
    
    # Get league or fallback to category
    league = stream.get("league") or category.capitalize()
    
    return {
        "id": stream.get("id"),
        "name": name,
        "league": league,
        "category": category,
        "stream_key": stream_key,
        "embed_url": embed_url,
        "thumbnail": stream.get("thumbnail_url", ""),
        "timestamp": stream.get("match_timestamp", int(time.time())),
        "viewers": stream.get("viewers", 0),
        "external": stream.get("is_external", False),
        "team1": stream.get("team1"),
        "team2": stream.get("team2"),
    }

async def get_categories() -> list[str]:
    """Fetch available categories from the API."""
    api_url = f"{BASE_URL}/api/v1/categories"
    log.info(f"Fetching categories from: {api_url}")
    
    if r := await network.request(api_url, log=log, headers={"User-Agent": USER_AGENT_RAW}):
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

async def fetch_streams_for_category(category: str) -> list[dict]:
    """Fetch streams for a single category."""
    api_url = f"{BASE_URL}/api/v1/streams?category={category}"
    log.debug(f"Fetching streams for {category}: {api_url}")
    
    streams = []
    if r := await network.request(api_url, log=log, headers={"User-Agent": USER_AGENT_RAW}):
        try:
            data = r.json()
            raw_streams = data.get("streams", [])
            for s in raw_streams:
                normalized = normalize_stream(s)
                if normalized:
                    streams.append(normalized)
            log.info(f"{category}: found {len(streams)} streams")
        except Exception as e:
            log.error(f"Error parsing streams for {category}: {e}")
    
    return streams

async def fetch_all_streams() -> list[dict]:
    """Fetch streams from all categories."""
    all_streams = []
    categories = await get_categories()
    
    if not categories:
        log.error("No categories available")
        return all_streams
    
    log.info(f"Fetching streams for {len(categories)} categories...")
    
    # Fetch all categories concurrently
    tasks = [fetch_streams_for_category(cat) for cat in categories]
    results = await asyncio.gather(*tasks)
    
    for streams in results:
        all_streams.extend(streams)
    
    log.info(f"Total streams found: {len(all_streams)}")
    return all_streams

async def process_stream_with_playwright(browser: Browser, stream: dict) -> tuple[str | None, str | None]:
    """Use Playwright to load the embed page and capture the M3U8 URL."""
    embed_url = stream.get("embed_url")
    name = stream.get("name", "Unknown")
    
    if not embed_url:
        log.warning(f"No embed URL for {name}")
        return None, None
    
    log.debug(f"Processing with Playwright: {embed_url}")
    
    try:
        async with network.event_context(browser) as context:
            async with network.event_page(context) as page:
                # Navigate to the embed URL
                await page.goto(embed_url, timeout=15000)
                
                # Wait for the player to load
                await page.wait_for_timeout(3000)
                
                # Try to find and click the play button if it exists
                try:
                    play_button = await page.query_selector('button[aria-label*="play"]')
                    if play_button:
                        await play_button.click()
                        await page.wait_for_timeout(2000)
                except Exception:
                    pass
                
                # Wait for the m3u8 request to appear in network
                # Use network.process_event which handles token extraction
                handler = partial(
                    network.process_event,
                    url=embed_url,
                    url_num=1,
                    page=page,
                    log=log,
                    timeout=15,
                )
                
                m3u8_url = await network.safe_process(
                    handler,
                    url_num=1,
                    semaphore=network.PW_S,
                    log=log,
                )
                
                if m3u8_url:
                    log.info(f" Captured M3U8 for {name}")
                    return m3u8_url, embed_url
                
                log.warning(f"No M3U8 captured for {name}")
                return None, None
                
    except Exception as e:
        log.error(f"Error processing {name}: {e}")
        return None, None

async def main():
    """Main function to run the updater."""
    log.info("Starting STRFREE updater")
    
    # Validate SOURCE_URL
    if not SOURCE_URL:
        log.error("STRM_FREE_API_URL environment variable is not set")
        return
    
    log.info(f"Using API URL: {SOURCE_URL}")
    
    # Fetch all streams
    all_streams = await fetch_all_streams()
    
    if not all_streams:
        log.error("No streams found")
        return
    
    log.info(f"Processing {len(all_streams)} streams to capture M3U8 URLs...")
    
    # Load cached URLs to avoid reprocessing
    cached_urls = CACHE_FILE.load() or {}
    urls = {}
    urls.update(cached_urls)
    
    # Process streams with Playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            processed_count = 0
            for i, stream in enumerate(all_streams, start=1):
                name = stream.get("name", "Unknown")
                embed_url = stream.get("embed_url", "")
                
                log.info(f"Processing {i}/{len(all_streams)}: {name}")
                
                # Check if already cached
                key = f"[{stream['league']}] {name} ({TAG})"
                if key in cached_urls:
                    log.debug(f"Already cached: {key}")
                    continue
                
                # Process with Playwright
                m3u8_url, referer = await process_stream_with_playwright(browser, stream)
                
                if m3u8_url:
                    # Get tvg info from leagues helper
                    tvg_id, logo = leagues.get_tvg_info(stream['category'], name)
                    
                    # Use thumbnail from API if available
                    final_logo = stream.get("thumbnail", logo) if stream.get("thumbnail") else logo
                    final_id = tvg_id or f"{stream['category']}.{stream['stream_key']}"
                    
                    entry = {
                        "url": str(m3u8_url),
                        "logo": final_logo,
                        "base": referer or embed_url,
                        "timestamp": stream.get("timestamp", int(time.time())),
                        "id": final_id,
                        "link": embed_url,
                        "referer_url": embed_url,
                    }
                    
                    # Store in both urls and cached_urls
                    urls[key] = entry
                    cached_urls[key] = entry
                    processed_count += 1
                    log.info(f" Added URL for: {name}")
                else:
                    log.warning(f" Failed to get M3U8 for: {name}")
                
                # Small delay between requests
                await asyncio.sleep(1)
                
        finally:
            await browser.close()
    
    # Save updated cache
    if cached_urls:
        CACHE_FILE.write(cached_urls)
        log.info(f"Saved {len(cached_urls)} events to cache")
    
    # Generate output files
    generate_output_files(urls)

def generate_output_files(urls: dict):
    """Generate TiviMate M3U8 file from collected URLs."""
    if not urls:
        log.info("No URLs to write to output file")
        # Create empty file with header
        try:
            with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
                f.write("#EXTM3U\n")
            log.info(f"Created empty {OUTPUT_FILE}")
        except Exception as e:
            log.error(f"Error creating output file: {e}")
        return
    
    log.info(f"Generating output file with {len(urls)} events")
    
    # Generate TiviMate format
    content = "#EXTM3U\n"
    
    # Sort by timestamp
    sorted_urls = sorted(urls.items(), key=lambda x: x[1].get("timestamp", 0))
    
    chno = 1
    for key, data in sorted_urls:
        if not data.get("url"):
            continue
            
        # Extract data
        sport_match = key.split("[")[1].split("]")[0] if "[" in key else "Live Events"
        event_name = key.split("]")[-1].strip().replace(f"({TAG})", "").strip() if "]" in key else key
        logo = data.get("logo", "")
        tvg_id = data.get("id", "Live.Event.us")
        url = data.get("url", "")
        referer_url = data.get("referer_url", "")
        
        if not url:
            continue
        
        # Use referer URL for headers
        vlc_referer = referer_url if referer_url else "https://streamfree.top/"
        
        # EXTINF line
        extinf = f'#EXTINF:-1 tvg-chno="{chno}" tvg-id="{tvg_id}" tvg-name="{key}" tvg-logo="{logo}" group-title="{sport_match}",{event_name}\n'
        
        # TiviMate format with pipe and encoded user agent
        tivimate_url = f"{url}|referer={vlc_referer}|origin={vlc_referer}|user-agent={USER_AGENT}"
        
        content += extinf
        content += f"{tivimate_url}\n\n"
        chno += 1
    
    # Write file
    try:
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            f.write(content)
        log.info(f"Successfully wrote {OUTPUT_FILE} with {chno-1} events")
    except Exception as e:
        log.error(f"Error writing {OUTPUT_FILE}: {e}")

def run():
    """Synchronous entry point for the updater."""
    asyncio.run(main())

if __name__ == "__main__":
    run()
