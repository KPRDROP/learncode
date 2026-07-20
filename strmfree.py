import os
import re
import json
import time
import urllib.request
import urllib.parse
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor, as_completed

from utils import Cache, Time, get_logger, leagues, network

# ================= CONFIG =================
SOURCE_URL = os.environ.get("STRM_FREE_API_URL")
OUTPUT_FILE = "strmfree_tivimate.m3u8"

BASE_URL = "https://streamfree.top"
USER_AGENT_RAW = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.0.0 Safari/537.36"
)
USER_AGENT = quote(USER_AGENT_RAW, safe="")

# All known categories from the API
CATEGORIES = [
    "soccer", "basketball", "hockey", "combat",
    "baseball", "football", "racing", "tennis", "cricket"
]

# ===========================================

def fetch_json(url: str) -> dict | None:
    """Fetch JSON data from a URL with a timeout."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT_RAW})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f" Failed to fetch {url}: {e}")
        return None

def extract_m3u8_from_embed(embed_url: str) -> str | None:
    """
    Fetch the embed page and extract the m3u8 URL.
    Handles both direct iframe sources and JavaScript-loaded streams.
    """
    req = urllib.request.Request(embed_url, headers={"User-Agent": USER_AGENT_RAW})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            html = r.read().decode("utf-8")
    except Exception as e:
        print(f" Failed to fetch embed {embed_url}: {e}")
        return None

    # Pattern 1: Look for iframe src that contains the m3u8
    # This handles cases where the m3u8 is directly in the iframe's src attribute.
    iframe_pattern = r'src="(https://streamfree\.top/live-cdn/[^"]+\.m3u8[^"]*)"'
    match = re.search(iframe_pattern, html)
    if match:
        return match.group(1)

    # Pattern 2: Look for the m3u8 URL in the page's JavaScript or video source.
    # This handles cases where the m3u8 is loaded dynamically.
    m3u8_pattern = r'(https://streamfree\.top/live-cdn/[^"\s]+\.m3u8[^"\s]*)'
    match = re.search(m3u8_pattern, html)
    if match:
        return match.group(1)

    # Pattern 3: Look for a player setup script that might contain the stream URL.
    # This is a fallback for more complex embedding scenarios.
    player_pattern = r'player\.setup\s*\(\s*\{[^}]*file:\s*"([^"]+)"[^}]*\}\)'
    match = re.search(player_pattern, html, re.DOTALL)
    if match:
        return match.group(1)

    # Pattern 4: Look for a source URL in a video tag.
    video_pattern = r'<video[^>]*>.*?<source[^>]+src="([^"]+\.m3u8[^"]*)"'
    match = re.search(video_pattern, html, re.DOTALL)
    if match:
        return match.group(1)

    return None

def process_stream(stream: dict) -> tuple[str, str]:
    """Process a single stream: get metadata and capture m3u8 URL."""
    name = stream.get("name", "Unknown Event")
    category = stream.get("category", "unknown")
    stream_key = stream.get("stream_key", "")
    embed_url = stream.get("embed_url", "")
    thumbnail = stream.get("thumbnail_url", "")
    league = stream.get("league", category.capitalize())

    # Construct the embed URL if missing
    if not embed_url and stream_key and category:
        embed_url = f"{BASE_URL}/embed/{category}/{stream_key}"

    if not embed_url:
        return None, None

    print(f" Processing: {name} ({league})")
    m3u8_url = extract_m3u8_from_embed(embed_url)

    # If the first attempt fails, try again after a short delay
    # This can help if the page needs time to load dynamic content.
    if not m3u8_url:
        print(f" Retrying {name} after delay...")
        time.sleep(2)
        m3u8_url = extract_m3u8_from_embed(embed_url)

    if not m3u8_url:
        print(f" No m3u8 found for {name}")
        return None, None

    # Prepare TiviMate entry
    entry = (
        f'#EXTINF:-1 tvg-id="{category}.{stream_key}" '
        f'tvg-name="[{league}] {name} | (STFREE)" '
        f'tvg-logo="{thumbnail}" '
        f'group-title="{league}",{name}\n'
        f'{m3u8_url}|referer={embed_url}|origin={embed_url}|user-agent={USER_AGENT}'
    )
    return embed_url, entry

def main():
    if not SOURCE_URL:
        raise RuntimeError("STRM_FREE_API_URL secret is missing")

    all_streams = []
    print("📡 Fetching streams from all categories...")

    # Fetch all categories in parallel for speed
    with ThreadPoolExecutor(max_workers=5) as executor:
        future_to_category = {
            executor.submit(fetch_json, f"{BASE_URL}/api/v1/streams?category={cat}"): cat
            for cat in CATEGORIES
        }

        for future in as_completed(future_to_category):
            category = future_to_category[future]
            data = future.result()
            if data and "streams" in data:
                streams = data["streams"]
                print(f" {category}: found {len(streams)} streams")
                all_streams.extend(streams)
            else:
                print(f" {category}: no streams or invalid data")

    if not all_streams:
        raise RuntimeError("No streams found in any category")

    print(f"\n Processing {len(all_streams)} streams to capture M3U8 URLs...")
    output_lines = ["#EXTM3U"]
    processed_count = 0

    # Process each stream sequentially to avoid rate limiting
    for stream in all_streams:
        embed_url, entry = process_stream(stream)
        if entry:
            output_lines.append(entry)
            processed_count += 1
        # Add a delay between requests to avoid being blocked
        time.sleep(1)

    if processed_count == 0:
        raise RuntimeError("No M3U8 URLs captured")

    # Write the playlist file
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(output_lines))

    print(f"\n Saved {OUTPUT_FILE} with {processed_count} entries")

if __name__ == "__main__":
    main()
