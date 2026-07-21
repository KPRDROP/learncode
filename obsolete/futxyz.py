#!/usr/bin/env python3
"""
Futbol-X Updater - Fetches streams from multiple sport categories
"""

import json
import os
import time
import base64
from datetime import datetime
from urllib.parse import quote_plus
from collections import defaultdict

import cloudscraper
import requests

# ================= CONFIG =================

BASE_URL = "https://futbol-x.xyz"
API_URLS = [
    f"{BASE_URL}/api/football.json",
    #f"{BASE_URL}/api/tennis.json",
    f"{BASE_URL}/api/basketball.json",
    f"{BASE_URL}/api/fights.json",
    f"{BASE_URL}/api/motorsports.json",
    f"{BASE_URL}/api/nfl.json",
    #f"{BASE_URL}/api/nhl.json",
    f"{BASE_URL}/api/mlb.json",
    #f"{BASE_URL}/api/rugby.json",
    #f"{BASE_URL}/api/golf.json",
    f"{BASE_URL}/api/others.json",
    f"{BASE_URL}/api/wrestling.json",
    #f"{BASE_URL}/api/darts.json",
]

OUTPUT_FILE = "futxyz_tivimate.m3u8"

# Headers for TiviMate format
REFERER = "https://futbol-x.xyz/"
ORIGIN = "https://futbol-x.xyz"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
ENCODED_UA = quote_plus(USER_AGENT)

TAG = "FUTXYZ"

DEFAULT_LOGO = "https://i.gyazo.com/4a5e9fa2525808ee4b65002b56d3450e.png"

# GitHub config
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN')
GITHUB_REPO = os.getenv('GITHUB_REPO')

# Cache file
CACHE_FILE = "futxyz_cache.json"
CACHE_EXPIRY = 1800  # 30 minutes

# ================= HELPER FUNCTIONS =================

def log(msg):
    print(msg, flush=True)


def load_cache():
    """Load cached data"""
    if not os.path.exists(CACHE_FILE):
        return None
    try:
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            cache = json.load(f)
            if time.time() - cache.get('timestamp', 0) < CACHE_EXPIRY:
                log(f"✓ Using cached data from {datetime.fromtimestamp(cache['timestamp']).strftime('%Y-%m-%d %H:%M:%S')}")
                return cache.get('streams', [])
    except Exception:
        pass
    return None


def save_cache(streams):
    """Save streams to cache"""
    try:
        cache = {
            'timestamp': time.time(),
            'streams': streams
        }
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(cache, f, indent=2)
        log("✓ Cached streams for future use")
    except Exception as e:
        log(f"⚠ Could not save cache: {e}")


def fetch_category(url, scraper):
    """Fetch a single category JSON"""
    try:
        headers = {
            'User-Agent': USER_AGENT,
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Referer': BASE_URL + '/',
            'Origin': BASE_URL,
            'Connection': 'keep-alive',
        }
        
        response = scraper.get(url, headers=headers, timeout=30)
        
        if response.status_code != 200:
            log(f"  ✗ {url} returned status {response.status_code}")
            return None
        
        data = response.json()
        
        if not data.get('success', False):
            log(f"  ✗ {url} returned success=false")
            return None
        
        return data
        
    except Exception as e:
        log(f"  ✗ Error fetching {url}: {e}")
        return None


def fetch_all_categories():
    """Fetch all category JSONs"""
    log(f"📡 Fetching from {len(API_URLS)} categories...")
    
    # Try cache first
    cached = load_cache()
    if cached:
        return cached
    
    try:
        scraper = cloudscraper.create_scraper(
            browser={
                "browser": "chrome",
                "platform": "windows",
                "mobile": False
            }
        )
        
        all_streams = []
        
        for url in API_URLS:
            log(f"  Fetching: {url}")
            data = fetch_category(url, scraper)
            
            if data and data.get('streams'):
                streams_data = data['streams']
                if streams_data and len(streams_data) > 0:
                    # The first item contains category info and streams
                    category_info = streams_data[0]
                    category = category_info.get('category', 'Unknown')
                    streams_list = category_info.get('streams', [])
                    
                    log(f"    ✓ Found {len(streams_list)} events in {category}")
                    
                    for event in streams_list:
                        event['_category'] = category
                        all_streams.append(event)
                else:
                    log(f"    ⚠ No streams found in {url}")
            else:
                log(f"    ✗ Failed to fetch {url}")
        
        log(f"✓ Total events found: {len(all_streams)}")
        
        # Save to cache
        save_cache(all_streams)
        
        return all_streams
        
    except Exception as e:
        log(f"✗ Error fetching categories: {e}")
        return load_cache()


def extract_streams(event):
    """Extract all m3u8 streams from an event"""
    streams = event.get('streams', [])
    event_name = event.get('name', 'Unknown Event')
    poster = event.get('poster', DEFAULT_LOGO)
    tag = event.get('tag', '')
    category = event.get('_category', 'Unknown')
    
    stream_entries = []
    
    for stream in streams:
        url = stream.get('url', '')
        title = stream.get('title', 'Stream')
        quality = stream.get('quality', '')
        
        # Skip empty URLs
        if not url:
            continue
        
        # Only include Main Feed and Alt Feed
        if 'Main Feed' in title or 'Alt Feed' in title:
            # Create a descriptive name
            suffix = f" - {title}" if title else ""
            suffix += f" ({quality})" if quality else ""
            
            stream_entries.append({
                'name': f"{event_name}{suffix}",
                'url': url,
                'poster': poster,
                'tag': tag,
                'category': category,
                'title': title,
                'quality': quality,
            })
    
    return stream_entries


def format_channel_name(name, tag=TAG):
    """Format channel name with tag"""
    return f"[{tag}] {name}"


def get_group_title(tag, category):
    """Generate group title from tag and category"""
    if tag:
        return f"{category} - {tag}"
    return category


def generate_m3u_entry(stream_data):
    """Generate a single M3U entry for a stream"""
    name = stream_data.get('name', 'Unknown Stream')
    url = stream_data.get('url', '')
    poster = stream_data.get('poster', DEFAULT_LOGO)
    tag = stream_data.get('tag', '')
    category = stream_data.get('category', 'General')
    
    # Ensure URL is valid
    if not url or not url.startswith('http'):
        return None
    
    # Format channel name
    display_name = format_channel_name(name, TAG)
    
    # Get group title
    group_title = get_group_title(tag, category)
    
    # Generate tvg-id from name (simple slug)
    tvg_id = name.lower().replace(' ', '_').replace('-', '_')[:50]
    tvg_id = re.sub(r'[^a-z0-9_]', '', tvg_id) or "Live.Event.us"
    
    # Add TiviMate headers to URL
    url_with_headers = (
        f"{url}"
        f"|referer={REFERER}"
        f"|origin={ORIGIN}"
        f"|user-agent={ENCODED_UA}"
    )
    
    # Build the EXTINF line
    extinf_line = (
        f'#EXTINF:-1 '
        f'tvg-id="{tvg_id}" '
        f'tvg-name="{display_name}" '
        f'tvg-logo="{poster}" '
        f'group-title="{group_title}",{display_name}'
    )
    
    return {
        'extinf': extinf_line,
        'url': url_with_headers,
        'name': name,
        'category': category,
        'tag': tag,
    }


def save_m3u_playlist(entries, output_file):
    """Save all entries to M3U file"""
    log(f"💾 Saving {len(entries)} streams to {output_file}")
    
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write("#EXTM3U\n")
        f.write(f"# Playlist generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"# Total streams: {len(entries)}\n\n")
        
        for entry in entries:
            if entry:
                f.write(f"{entry['extinf']}\n")
                f.write(f"{entry['url']}\n\n")
    
    log(f"✓ Playlist saved successfully")
    return output_file


def push_to_github(filename):
    """Push file to GitHub using token"""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        log("⚠ No GitHub credentials found, skipping push")
        return None
    
    log(f"📤 Pushing {filename} to GitHub...")
    
    with open(filename, 'r', encoding='utf-8') as f:
        content = f.read()
    
    content_base64 = base64.b64encode(content.encode('utf-8')).decode('utf-8')
    
    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
    headers = {
        'Authorization': f'token {GITHUB_TOKEN}',
        'Accept': 'application/vnd.github.v3+json'
    }
    
    # Get existing file SHA
    sha = None
    try:
        response = requests.get(api_url, headers=headers)
        if response.status_code == 200:
            sha = response.json().get('sha')
            log("✓ Found existing file, will update")
    except:
        pass
    
    payload = {
        'message': f'Update {filename} - {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}',
        'content': content_base64,
        'branch': 'main'
    }
    if sha:
        payload['sha'] = sha
    
    response = requests.put(api_url, headers=headers, json=payload)
    
    if response.status_code in [200, 201]:
        log(f"✓ Successfully pushed to GitHub")
        return response.json()
    else:
        log(f"✗ GitHub push failed: {response.status_code}")
        if response.text:
            log(f"  Response: {response.text[:200]}")
        return None


def print_statistics(entries):
    """Print statistics"""
    if not entries:
        return
    
    categories = defaultdict(int)
    tags = defaultdict(int)
    
    for entry in entries:
        if entry:
            categories[entry.get('category', 'unknown')] += 1
            tags[entry.get('tag', 'unknown')] += 1
    
    log("\n" + "=" * 50)
    log("📊 Stream Statistics:")
    log("=" * 50)
    log("\nBy Category:")
    for cat, count in sorted(categories.items(), key=lambda x: x[1], reverse=True):
        log(f"  {cat}: {count} streams")
    
    log("\nBy Tag:")
    for tag, count in sorted(tags.items(), key=lambda x: x[1], reverse=True)[:10]:
        if tag != 'unknown':
            log(f"  {tag}: {count} streams")
    
    log(f"\n  Total: {len(entries)} streams")
    log("=" * 50)


# ================= MAIN FUNCTION =================

def main():
    log("=" * 60)
    log("Futbol-X Updater - TiviMate M3U Generator")
    log("=" * 60)
    
    # Fetch all categories
    events = fetch_all_categories()
    
    if not events:
        log("\n✗ Could not fetch events. Exiting.")
        return
    
    log(f"\n📦 Processing {len(events)} events...")
    
    entries = []
    skipped = 0
    stream_count = 0
    
    for i, event in enumerate(events, 1):
        streams = extract_streams(event)
        
        if streams:
            for stream in streams:
                entry = generate_m3u_entry(stream)
                if entry:
                    entries.append(entry)
                    stream_count += 1
                else:
                    skipped += 1
        else:
            skipped += 1
        
        if i % 10 == 0:
            log(f"  Processed {i}/{len(events)} events... ({stream_count} streams)")
    
    log(f"\n✓ Successfully processed {stream_count} streams from {len(events)} events (skipped {skipped})")
    
    print_statistics(entries)
    
    if entries:
        # Sort entries by category and name
        entries.sort(key=lambda x: (x.get('category', ''), x.get('name', '')))
        
        log("\n💾 Saving playlist...")
        output_file = save_m3u_playlist(entries, OUTPUT_FILE)
        
        log("\n📤 Pushing to GitHub...")
        push_to_github(output_file)
        
        log("\n" + "=" * 60)
        log(f"✅ Complete! Playlist saved to: {output_file}")
        log("=" * 60)
    else:
        log("\n⚠ No valid streams found to save")


if __name__ == "__main__":
    import re
    main()
