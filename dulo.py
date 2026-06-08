#!/usr/bin/env python3
"""
Dulo.tv API Scraper - Fetches live TV channels and creates TiviMate M3U playlist
"""

import json
import os
import base64
import requests
from datetime import datetime
from urllib.parse import quote_plus

# ================= CONFIG =================

# API URL as secret variable (can be overridden by environment variable)
API_URL = os.getenv('DULO_API_URL')
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

# GitHub config (for auto-push)
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN')
GITHUB_REPO = os.getenv('GITHUB_REPO')

# ================= HELPER FUNCTIONS =================

def log(msg):
    print(msg, flush=True)


def fetch_channels():
    """Fetch channels from Dulo.tv API with proper headers to avoid 403"""
    log(f"📡 Fetching channels from: {API_URL}")
    
    # Headers to mimic a real browser request
    headers = {
        'User-Agent': USER_AGENT,
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
        'Pragma': 'no-cache'
    }
    
    try:
        response = requests.get(API_URL, timeout=30, headers=headers)
        response.raise_for_status()
        
        data = response.json()
        channels = data.get('channels', [])
        log(f"✓ Found {len(channels)} channels")
        return channels
        
    except requests.RequestException as e:
        log(f"✗ Error fetching API: {e}")
        if hasattr(e, 'response') and e.response is not None:
            log(f"  Status: {e.response.status_code}")
            log(f"  Response: {e.response.text[:200]}")
        return []
    except json.JSONDecodeError as e:
        log(f"✗ Error parsing JSON: {e}")
        return []


def format_channel_name(name, tag=TAG):
    """Format channel name with tag"""
    return f"[{tag}] {name}"


def get_category_mapping(category):
    """
    Map API categories to TiviMate group titles
    Common categories: sports, news, entertainment, movies, documentary, kids
    """
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
    
    # Return capitalized category if not in map, otherwise return mapped value
    return category_map.get(category, category.capitalize())


def generate_m3u_entry(channel):
    """
    Generate a single M3U entry for a channel
    Format: #EXTINF:-1 tvg-id="ID" tvg-name="NAME" tvg-logo="LOGO" group-title="GROUP",DISPLAY_NAME
    URL with TiviMate headers
    """
    # Extract channel data
    name = channel.get('name', 'Unknown Channel')
    category = channel.get('category', 'general')
    source_url = channel.get('source_url', '')
    logo_url = channel.get('logo_url', DEFAULT_LOGO)
    
    # Skip channels without source URL or invalid source URL
    if not source_url or source_url == 'v':
        return None
    
    # Format channel name with tag
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
    log(f"💾 Saving {len(entries)} channels to {output_file}")
    
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write("#EXTM3U\n")
        
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
        log("  Set GITHUB_TOKEN and GITHUB_REPO environment variables to enable auto-push")
        return None
    
    log(f"📤 Pushing {filename} to GitHub...")
    
    # Read file content
    with open(filename, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # Encode to base64
    content_base64 = base64.b64encode(content.encode('utf-8')).decode('utf-8')
    
    # GitHub API endpoint
    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
    headers = {
        'Authorization': f'token {GITHUB_TOKEN}',
        'Accept': 'application/vnd.github.v3+json'
    }
    
    # Get existing file SHA if any
    sha = None
    try:
        response = requests.get(api_url, headers=headers)
        if response.status_code == 200:
            sha = response.json().get('sha')
            log(f"✓ Found existing file, will update")
    except:
        pass
    
    # Prepare commit payload
    payload = {
        'message': f'Update {filename} - {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}',
        'content': content_base64,
        'branch': 'main'
    }
    if sha:
        payload['sha'] = sha
    
    # Push to GitHub
    response = requests.put(api_url, headers=headers, json=payload)
    
    if response.status_code in [200, 201]:
        log(f"✓ Successfully pushed to GitHub: {GITHUB_REPO}/{filename}")
        return response.json()
    else:
        log(f"✗ GitHub push failed: {response.status_code}")
        if response.text:
            log(f"  Response: {response.text[:200]}")
        return None


def print_statistics(entries):
    """Print statistics about the channels"""
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
        log(" No channels found. Exiting.")
        return
    
    # Process each channel
    log("\n Processing channels...")
    entries = []
    skipped = 0
    
    for i, channel in enumerate(channels, 1):
        entry = generate_m3u_entry(channel)
        if entry:
            entries.append(entry)
        else:
            skipped += 1
        
        if i % 50 == 0:  # Progress update every 50 channels
            log(f"  Processed {i}/{len(channels)} channels...")
    
    log(f"\n✓ Successfully processed {len(entries)}/{len(channels)} channels (skipped {skipped})")
    
    # Print statistics
    print_statistics(entries)
    
    # Save M3U playlist
    log("\n Saving playlist...")
    output_file = save_m3u_playlist(entries, OUTPUT_FILE)
    
    # Push to GitHub if configured
    log("\n Pushing to GitHub...")
    push_to_github(output_file)
    
    log("\n" + "=" * 60)
    log(f" Complete! Playlist saved to: {output_file}")
    log("=" * 60)


if __name__ == "__main__":
    main()
