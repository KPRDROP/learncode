#!/usr/bin/env python3
"""
Dulo.tv API Updater
"""

import json
import os
import base64
import time
import requests
from datetime import datetime
from urllib.parse import quote_plus

# ================= CONFIG =================

# Try multiple possible API endpoints
API_ENDPOINTS = [
    'https://dulo.tv/api/live-tv/channels',
    'https://dulo.tv/api/channels',
    'https://api.dulo.tv/v1/channels',
    'https://dulo.tv/api/live-tv/channels?all=true',
]

OUTPUT_FILE = "dulo_tivimate.m3u8"

# Headers for TiviMate format
REFERER = "https://hey.dulo.tv/"
ORIGIN = "https://hey.dulo.tv"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
ENCODED_UA = quote_plus(USER_AGENT)

TVG_ID = "Live.Event.us"
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
        log(f"⚠ Could not save cache: {e}")


def fetch_with_retry(url, max_retries=3):
    """Fetch URL with retry logic and multiple headers"""
    
    # Different user agents to try
    user_agents = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
    ]
    
    session = requests.Session()
    
    # Get initial cookies by visiting main page first
    try:
        log("  🔄 Getting initial session cookies...")
        session.get('https://dulo.tv/', timeout=10, headers={'User-Agent': user_agents[0]})
        time.sleep(1)
    except:
        pass
    
    for retry in range(max_retries):
        for ua in user_agents:
            try:
                headers = {
                    'User-Agent': ua,
                    'Accept': 'application/json, text/plain, */*',
                    'Accept-Language': 'en-US,en;q=0.9',
                    'Accept-Encoding': 'gzip, deflate, br',
                    'Referer': 'https://dulo.tv/',
                    'Origin': 'https://dulo.tv',
                    'Connection': 'keep-alive',
                    'Sec-Fetch-Dest': 'empty',
                    'Sec-Fetch-Mode': 'cors',
                    'Sec-Fetch-Site': 'same-origin',
                    'Cache-Control': 'no-cache',
                    'Pragma': 'no-cache',
                    'X-Requested-With': 'XMLHttpRequest',
                }
                
                response = session.get(url, timeout=15, headers=headers)
                
                if response.status_code == 200:
                    # Try to parse as JSON
                    try:
                        data = response.json()
                        if data and (data.get('channels') or isinstance(data, list)):
                            return data
                    except:
                        pass
                    
                    # Try to extract JSON from response if it's wrapped
                    text = response.text
                    if text.startswith('{') or text.startswith('['):
                        try:
                            return json.loads(text)
                        except:
                            pass
                
                time.sleep(0.5)
                
            except Exception as e:
                continue
        
        if retry < max_retries - 1:
            log(f"  ⚠ Retry {retry + 1}/{max_retries}...")
            time.sleep(2)
    
    return None


def fetch_channels():
    """Fetch channels from multiple endpoints"""
    log(f"📡 Attempting to fetch channels...")
    
    # Try cache first
    cached = load_cache()
    if cached:
        return cached
    
    # Try multiple endpoints
    for endpoint in API_ENDPOINTS:
        log(f"  Trying: {endpoint}")
        result = fetch_with_retry(endpoint)
        
        if result:
            if isinstance(result, list):
                channels = result
                log(f"✓ Found {len(channels)} channels from list endpoint")
                save_cache(channels)
                return channels
            elif isinstance(result, dict) and 'channels' in result:
                channels = result['channels']
                log(f"✓ Found {len(channels)} channels from {endpoint}")
                save_cache(channels)
                return channels
            elif isinstance(result, dict) and 'data' in result:
                channels = result['data']
                log(f"✓ Found {len(channels)} channels from data field")
                save_cache(channels)
                return channels
    
    log("✗ Could not fetch channels from any endpoint")
    return []


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
    # Handle both dict and object formats
    if isinstance(channel, dict):
        name = channel.get('name', 'Unknown Channel')
        category = channel.get('category', 'general')
        source_url = channel.get('source_url', '')
        logo_url = channel.get('logo_url', DEFAULT_LOGO)
    else:
        # If it's not a dict, try to access attributes
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
        f'tvg-id="{TVG_ID}" '
        f'tvg-name="{display_name}" '
        f'tvg-logo="{logo_url}" '
        f'group-title="{group_title}",{display_name}'
    )
    
    return {
        'extinf': extinf_line,
        'url': url_with_headers,
        'name': name,
        'category': category
    }


def save_m3u_playlist(entries, output_file):
    """Save all entries to M3U file"""
    log(f" Saving {len(entries)} channels to {output_file}")
    
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write("#EXTM3U\n")
        f.write(f"# Playlist generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        
        for entry in entries:
            if entry:
                f.write(f"{entry['extinf']}\n")
                f.write(f"{entry['url']}\n")
    
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
    log("Dulo.tv API Scraper - TiviMate M3U Generator")
    log("=" * 60)
    
    channels = fetch_channels()
    
    if not channels:
        log("\n Could not fetch channels. Creating sample playlist from cache if available...")
        # Try one more time with different approach
        log("\n If the issue persists, the API may have changed.")
        log("   You can manually provide a channels.json file.")
        return
    
    log(f"\n Processing {len(channels)} channels...")
    
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
    main()
