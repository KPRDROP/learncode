#!/usr/bin/env python3
"""
Dulo.tv API Updater - Uses cloudscraper to bypass Cloudflare protection
"""

import json
import os
import base64
import cloudscraper
import requests
from datetime import datetime
from urllib.parse import quote_plus

# ================= CONFIG =================

API_URL = "https://dulo.tv/api/live-tv/channels"
OUTPUT_FILE = "dulo_tivimate.m3u8"

# Headers for TiviMate format
REFERER = "https://hey.dulo.tv/"
ORIGIN = "https://hey.dulo.tv"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
ENCODED_UA = quote_plus(USER_AGENT)

TAG = "DULO"

# Default logo for channels without logo
DEFAULT_LOGO = "https://i.gyazo.com/4a5e9fa2525808ee4b65002b56d3450e.png"

# GitHub config
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN')
GITHUB_REPO = os.getenv('GITHUB_REPO')

# Cache file to store channels
CACHE_FILE = "dulo_cache.json"
CACHE_EXPIRY = 3600  # 1 hour

# ================= HELPER FUNCTIONS =================

def log(msg):
    print(msg, flush=True)


def load_cache():
    """Load cached channels"""
    if not os.path.exists(CACHE_FILE):
        return None
    try:
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            cache = json.load(f)
            if time.time() - cache.get('timestamp', 0) < CACHE_EXPIRY:
                log(f"✓ Using cached data from {datetime.fromtimestamp(cache['timestamp']).strftime('%Y-%m-%d %H:%M:%S')}")
                return cache.get('channels', [])
    except Exception:
        pass
    return None


def save_cache(channels):
    """Save channels to cache"""
    try:
        cache = {
            'timestamp': time.time(),
            'channels': channels
        }
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(cache, f, indent=2)
        log("✓ Cached channels for future use")
    except Exception as e:
        log(f" Could not save cache: {e}")


def fetch_channels():
    """Fetch channels from dulo.tv API using cloudscraper"""
    log(f"📡 Fetching channels from: {API_URL}")
    
    # Try cache first
    cached = load_cache()
    if cached:
        return cached
    
    try:
        # Create cloudscraper instance to bypass Cloudflare protection
        scraper = cloudscraper.create_scraper(
            browser={
                "browser": "chrome",
                "platform": "windows",
                "mobile": False
            }
        )
        
        # Add headers to mimic a real browser
        headers = {
            'User-Agent': USER_AGENT,
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Referer': 'https://dulo.tv/',
            'Origin': 'https://dulo.tv',
            'Connection': 'keep-alive',
        }
        
        response = scraper.get(API_URL, headers=headers, timeout=30)
        
        if response.status_code != 200:
            log(f"✗ API returned status {response.status_code}")
            return None
        
        data = response.json()
        
        # Extract channels from response
        channels = data.get("channels", data) if isinstance(data, dict) else data
        
        if not channels:
            log("✗ No channels found in response")
            return None
        
        log(f"✓ Found {len(channels)} channels")
        
        # Save to cache
        save_cache(channels)
        
        return channels
        
    except cloudscraper.exceptions.CloudflareChallengeError as e:
        log(f"✗ Cloudflare challenge failed: {e}")
        log("  Trying cached data if available...")
        return load_cache()
    except Exception as e:
        log(f"✗ Error fetching channels: {e}")
        return load_cache()


def format_channel_name(name, tag=TAG):
    """Format channel name with tag"""
    # Clean up the name
    name = name.replace('HD |', '|').replace('HD', '').strip()
    return f"[{tag}] {name}"


def get_category_mapping(category):
    """Map API categories to TiviMate group titles"""
    category_map = {
        'sports': 'Sports',
        'news': 'News',
        'entertainment': 'Entertainment',
        'movies': 'Movies',
        'documentary': 'Documentary',
        'kids': 'Kids',
        'music': 'Music',
        'lifestyle': 'Lifestyle'
    }
    return category_map.get(category, category.capitalize() if category else 'General')


def generate_m3u_entry(channel):
    """Generate a single M3U entry for a channel"""
    # Extract channel data
    if isinstance(channel, dict):
        ch_id = channel.get('id', '')
        name = channel.get('name', 'Unknown Channel')
        category = channel.get('category', 'general')
        source_url = channel.get('source_url', '')
        logo_url = channel.get('logo_url', DEFAULT_LOGO)
    else:
        ch_id = getattr(channel, 'id', '')
        name = getattr(channel, 'name', 'Unknown Channel')
        category = getattr(channel, 'category', 'general')
        source_url = getattr(channel, 'source_url', '')
        logo_url = getattr(channel, 'logo_url', DEFAULT_LOGO)
    
    # Skip channels without valid source URL
    if not source_url or source_url == 'v' or len(source_url) < 10:
        return None
    
    # Ensure URL starts with http
    if not source_url.startswith('http'):
        if source_url.startswith('/'):
            source_url = f"https://dulo.tv{source_url}"
        else:
            return None
    
    # Format channel name
    display_name = format_channel_name(name, TAG)
    
    # Get group title
    group_title = get_category_mapping(category)
    
    # Use channel ID as tvg-id
    tvg_id = ch_id if ch_id else "Live.Event.us"
    
    # Add TiviMate headers to URL
    url_with_headers = (
        f"{source_url}"
        f"|referer={REFERER}"
        f"|origin={ORIGIN}"
        f"|user-agent={ENCODED_UA}"
    )
    
    # Build the EXTINF line
    extinf_line = (
        f'#EXTINF:-1 '
        f'tvg-id="{tvg_id}" '
        f'tvg-name="{display_name}" '
        f'tvg-logo="{logo_url}" '
        f'group-title="{group_title}",{display_name}'
    )
    
    return {
        'extinf': extinf_line,
        'url': url_with_headers,
        'name': name,
        'category': category,
        'id': ch_id
    }


def save_m3u_playlist(entries, output_file):
    """Save all entries to M3U file"""
    log(f" Saving {len(entries)} channels to {output_file}")
    
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write("#EXTM3U\n")
        f.write(f"# Playlist generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        
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
    
    log(f" Pushing {filename} to GitHub...")
    
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
    
    categories = {}
    for entry in entries:
        if entry:
            cat = entry.get('category', 'unknown')
            categories[cat] = categories.get(cat, 0) + 1
    
    log("\n" + "=" * 50)
    log(" Channel Statistics:")
    log("=" * 50)
    for cat, count in sorted(categories.items(), key=lambda x: x[1], reverse=True):
        log(f"  {cat.capitalize()}: {count} channels")
    log(f"\n  Total: {len(entries)} channels")
    log("=" * 50)


# ================= MAIN FUNCTION =================

def main():
    log("=" * 60)
    log("Dulo API Updater - TiviMate M3U Generator")
    log("=" * 60)
    
    # Fetch channels from API
    channels = fetch_channels()
    
    if not channels:
        log("\n Could not fetch channels. Exiting.")
        return
    
    log(f"\n🔄 Processing {len(channels)} channels...")
    
    entries = []
    skipped = 0
    
    for i, channel in enumerate(channels, 1):
        entry = generate_m3u_entry(channel)
        if entry:
            entries.append(entry)
        else:
            skipped += 1
        
        if i % 100 == 0:
            log(f"  Processed {i}/{len(channels)} channels...")
    
    log(f"\n✓ Successfully processed {len(entries)}/{len(channels)} channels (skipped {skipped})")
    
    print_statistics(entries)
    
    if entries:
        log("\n Saving playlist...")
        output_file = save_m3u_playlist(entries, OUTPUT_FILE)
        
        log("\n Pushing to GitHub...")
        push_to_github(output_file)
        
        log("\n" + "=" * 60)
        log(f" Complete! Playlist saved to: {output_file}")
        log("=" * 60)
    else:
        log("\n No valid channels found to save")


if __name__ == "__main__":
    import time
    main()
