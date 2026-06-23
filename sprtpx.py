import os
import re
import requests
from pathlib import Path
from urllib.parse import quote

# ================= CONFIG =================

BASE_URL = os.getenv("PXL_BASE_URL", "").strip()
if not BASE_URL:
    raise RuntimeError("PXL_BASE_URL secret is not set or empty")

OUT_VLC = Path("pxl_vlc.m3u8")
OUT_TIVI = Path("pxl_tivimate.m3u8")

REFERER = "https://pixelsport.tv/"
ORIGIN = "https://pixelsport.tv"

UA_RAW = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:152.0) "
    "Gecko/20100101 Firefox/152.0"
)
UA_ENC = quote(UA_RAW)

# Sport filter - only include these sports (based on group-title)
SPORT_FILTER = [
    "24/7 Channels",
    "MLB",
    "NFL",
    "NHL",
]

# =========================================


def fetch_playlist() -> str:
    """Fetch the playlist from the base URL."""
    try:
        r = requests.get(
            BASE_URL,
            timeout=30,
            headers={
                "User-Agent": UA_RAW,
                "Referer": REFERER,
                "Origin": ORIGIN,
            },
        )
        r.raise_for_status()
        return r.text.strip()
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Failed to fetch playlist: {e}")


def clean_channel_name(name: str) -> str:
    """Clean channel/event name by removing emojis and converting 'at' to 'vs'."""
    # Remove emojis (Unicode emoji range)
    emoji_pattern = re.compile(
        "["
        "\U0001F600-\U0001F64F"  # emoticons
        "\U0001F300-\U0001F5FF"  # symbols & pictographs
        "\U0001F680-\U0001F6FF"  # transport & map symbols
        "\U0001F1E0-\U0001F1FF"  # flags (iOS)
        "\U00002702-\U000027B0"
        "\U000024C2-\U0001F251"
        "]+",
        flags=re.UNICODE,
    )
    name = emoji_pattern.sub("", name).strip()
    
    # Convert "at" to "vs" (case insensitive, whole word)
    name = re.sub(r'\bat\b', 'vs', name, flags=re.IGNORECASE)
    
    # Clean up multiple spaces
    name = re.sub(r'\s+', ' ', name).strip()
    
    return name


def should_include_channel(extinf_line: str) -> bool:
    """Check if the channel should be included based on SPORT_FILTER."""
    if not SPORT_FILTER:
        return True
    
    # Extract group-title from EXTINF line
    # Format: #EXTINF:-1 tvg-logo="..." tvg-name="..." group-title="GROUP",Channel Name
    match = re.search(r'group-title="([^"]+)"', extinf_line)
    if not match:
        # If no group-title, include it (or you can set to False to exclude)
        return True
    
    group_title = match.group(1).strip()
    
    # Check if group-title matches any sport filter (case insensitive)
    group_lower = group_title.lower()
    for sport in SPORT_FILTER:
        if sport.lower() == group_lower:
            return True
    
    return False


def parse_playlist(m3u: str) -> list:
    """Parse the M3U playlist into structured data."""
    lines = m3u.splitlines()
    entries = []
    current_entry = None
    
    for line in lines:
        line = line.strip()
        
        if not line:
            continue
            
        if line.startswith("#EXTINF"):
            # Start a new entry
            current_entry = {"extinf": line, "url": None}
        elif line.startswith("#"):
            # Skip other comments
            continue
        else:
            # This is the URL
            if current_entry:
                current_entry["url"] = line
                entries.append(current_entry)
                current_entry = None
    
    return entries


def format_extinf(extinf: str, channel_num: int) -> str:
    """Format EXTINF line with tvg-chno at the beginning."""
    # Clean the channel name
    if ',' in extinf:
        parts = extinf.split(',', 1)
        clean_name = clean_channel_name(parts[1])
        extinf = f"{parts[0]},{clean_name}"
    
    # Remove existing tvg-chno if present
    extinf = re.sub(r'\s+tvg-chno="[^"]*"', '', extinf)
    
    # Insert tvg-chno right after #EXTINF:-1
    extinf = re.sub(
        r'^(#EXTINF:-?\d+)',
        rf'\1 tvg-chno="{channel_num}"',
        extinf
    )
    
    return extinf


def build_vlc_playlist(entries: list) -> str:
    """Build VLC-compatible playlist from entries."""
    out = ["#EXTM3U"]
    channel_counter = 1
    
    for entry in entries:
        extinf = entry["extinf"]
        url = entry["url"]
        
        # Check sport filter
        if not should_include_channel(extinf):
            continue
        
        # Format EXTINF with tvg-chno at the beginning
        extinf = format_extinf(extinf, channel_counter)
        
        out.append(extinf)
        out.append(f"#EXTVLCOPT:http-user-agent={UA_RAW}")
        out.append(f"#EXTVLCOPT:http-referrer={REFERER}")
        out.append(f"#EXTVLCOPT:http-origin={ORIGIN}")
        out.append("#EXTVLCOPT:http-icy-metadata=1")
        # VLC uses the URL as-is without any parameters
        out.append(url)
        
        channel_counter += 1
    
    return "\n".join(out) + "\n"


def build_tivimate_playlist(entries: list) -> str:
    """Build TiviMate-compatible playlist from entries."""
    out = ["#EXTM3U"]
    channel_counter = 1
    
    for entry in entries:
        extinf = entry["extinf"]
        url = entry["url"]
        
        # Check sport filter
        if not should_include_channel(extinf):
            continue
        
        # Format EXTINF with tvg-chno at the beginning
        extinf = format_extinf(extinf, channel_counter)
        
        out.append(extinf)
        
        # TiviMate uses pipe parameters with URL encoded user-agent
        # Remove any existing parameters from URL
        if '|' in url:
            url = url.split('|')[0]
        
        # Add parameters
        url_with_params = (
            f"{url}"
            f"|referer={REFERER}"
            f"|origin={ORIGIN}"
            f"|user-agent={UA_ENC}"
            f"|icy-metadata=1"
        )
        
        out.append(url_with_params)
        channel_counter += 1
    
    return "\n".join(out) + "\n"


def main():
    print("Fetching PixelSports playlist...")
    raw = fetch_playlist()
    
    print("Parsing playlist...")
    entries = parse_playlist(raw)
    print(f"Found {len(entries)} entries")
    
    print("Applying sport filter...")
    print(f"Sport filter: {', '.join(SPORT_FILTER)}")
    
    # Count filtered entries
    filtered_count = sum(1 for e in entries if should_include_channel(e["extinf"]))
    print(f"Keeping {filtered_count} entries after filter")
    
    print("Writing VLC playlist...")
    OUT_VLC.write_text(build_vlc_playlist(entries), encoding="utf-8")
    
    print("Writing TiviMate playlist...")
    OUT_TIVI.write_text(build_tivimate_playlist(entries), encoding="utf-8")
    
    print("Done:")
    print(f" - {OUT_VLC}")
    print(f" - {OUT_TIVI}")


if __name__ == "__main__":
    main()
