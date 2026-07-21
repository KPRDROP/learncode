from collections.abc import KeysView
from urllib.parse import urljoin
import os
import re
import json
import base64
from typing import Dict, Any
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from .utils import Cache, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "TOOH"

CACHE_FILE = Cache(TAG, exp=10_800)

API_FILE = Cache(f"{TAG}-api", exp=19_800)

BASE_URL = os.getenv("BASE_URL")

# User agents
VLC_USER_AGENT = os.getenv("VLC_USER_AGENT")
TIVIMATE_USER_AGENT = os.getenv("TIVIMATE_USER_AGENT")

# AES decryption key from event.js
KEY_HEX = '7e7b186136253a23a29938d220d87aef'


def hex_to_bytes(hex_str: str) -> bytes:
    """Convert hex string to bytes."""
    return bytes.fromhex(hex_str)


def base64_to_bytes(base64_str: str) -> bytes:
    """Convert base64 string to bytes."""
    return base64.b64decode(base64_str)


def decrypt_response(encrypted_data: Dict[str, str]) -> Dict[str, Any]:
    """
    Decrypt AES-CBC encrypted response from the API.
    
    Args:
        encrypted_data: Dictionary with 'iv' and 'encrypted' keys
        
    Returns:
        Decrypted JSON data as dictionary
    """
    try:
        # Get IV and encrypted data
        iv = base64_to_bytes(encrypted_data['iv'])
        encrypted = base64_to_bytes(encrypted_data['encrypted'])
        
        # Create AES cipher
        key = hex_to_bytes(KEY_HEX)
        cipher = AES.new(key, AES.MODE_CBC, iv)
        
        # Decrypt and unpad
        decrypted = unpad(cipher.decrypt(encrypted), AES.block_size)
        
        # Parse JSON
        return json.loads(decrypted.decode('utf-8'))
    except Exception as e:
        log.error(f"Decryption failed: {e}")
        raise


def get_event_info(name: str) -> tuple[str, str]:
    return (
        tuple(x.strip() for x in name.split(":")[:2])
        if ":" in name
        else ("Live Event", name)
    )


def clean_display_name(name: str) -> str:
    """
    Clean display name by removing commas and extra spaces.
    
    Args:
        name: Display name
        
    Returns:
        Cleaned display name
    """
    # Remove commas but keep the text around them
    cleaned = re.sub(r',\s*', ' ', name)
    # Remove extra spaces
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned


async def get_events(cached_keys: KeysView[str]) -> dict[str, dict[str, str | float]]:
    events = {}

    now = Time.clean(Time.now())

    if not (api_data := API_FILE.load(per_entry=False)):
        log.info("Refreshing API cache")

        api_data = {"timestamp": now.timestamp()}

        if r := await network.request(
            urljoin(BASE_URL, "api/events"),
            headers={"Referer": BASE_URL},
            log=log,
        ):
            # Check if response is encrypted
            response_data = r.json()
            
            # If the response has iv and encrypted fields, decrypt it
            if response_data and 'iv' in response_data and 'encrypted' in response_data:
                try:
                    log.info("Decrypting API response")
                    api_data = decrypt_response(response_data)
                    api_data["timestamp"] = now.timestamp()
                except Exception as e:
                    log.error(f"Failed to decrypt API response: {e}")
                    api_data = {"events": [], "timestamp": now.timestamp()}
            else:
                # Not encrypted, use as-is
                api_data = response_data
                api_data["timestamp"] = now.timestamp()

        API_FILE.write(api_data)

    # Get events from the decrypted data
    events_list = api_data.get("events", [])
    log.info(f"Total events in API: {len(events_list)}")

    for event in events_list:
        if not all(
            values := [
                event.get(k)
                for k in (
                    "Match",
                    "League",
                    "Date",
                    "Time",
                )
            ]
        ):
            continue

        name, sport, event_date, event_time = values

        if sport.lower() == "unknown league":
            sport, name = get_event_info(name)

        # Handle timezone/day adjustment
        event_dt = Time.from_str(f"{event_date} {event_time}", timezone="UTC")
        event_dt = event_dt.delta(days=1) if event_time.startswith("0") else event_dt

        if event_dt.date() != now.date():
            continue

        elif not (event_channels := event.get("Channels")):
            continue

        event_urls: dict[str, str] = {}
        for channel in event_channels:
            channel_id = channel.get("id")
            channel_name = channel.get("name", "")
            
            if channel_id and not channel_name.lower().startswith("backup"):
                # Some channels might have URL directly
                if "URL" in channel:
                    event_urls[channel_name] = channel["URL"]
                else:
                    event_urls[channel_name] = channel_id

        for ch_name, ch_id in event_urls.items():
            # Clean the sport and name for display
            clean_sport = clean_display_name(sport)
            clean_name = clean_display_name(name)
            
            if (key := f"[{clean_sport}] {clean_name} | {ch_name} ({TAG})") in cached_keys:
                continue

            tvg_id, logo = leagues.get_tvg_info(sport, name)

            # Determine if we have a direct URL or need to build it
            if ch_id.startswith("http"):
                source = ch_id
            else:
                source = urljoin(BASE_URL, f"stream/{ch_id}")

            events[key] = {
                "source": source,
                "logo": logo,
                "refer": BASE_URL,
                "timestamp": now.timestamp(),
                "tvg-id": tvg_id or "Live.Event.us",
                "sport": clean_sport,
                "name": clean_name,
                "channel_name": ch_name,
            }

    log.info(f"Processed {len(events)} valid event(s)")
    return events


async def scrape() -> None:
    cached_urls = CACHE_FILE.load()

    valid_count = len(
        valid_urls := {k: v for k, v in cached_urls.items() if v["source"]}
    )

    urls.update(valid_urls)

    log.info(f"Loaded {valid_count} event(s) from cache")

    log.info(f'Scraping from "{BASE_URL}"')

    urls.update(await get_events(cached_urls.keys()))

    (
        log.info(f"Collected and cached {new_count} new event(s)")
        if (new_count := len(urls) - valid_count)
        else log.info("No new events found")
    )

    CACHE_FILE.write(urls)

    # Generate M3U8 files after scraping
    await generate_m3u8_files(urls)


async def generate_m3u8_files(channels_data: Dict[str, Dict[str, str | float]], output_dir: str = ".") -> None:
    """
    Generate two M3U8 files from channel data.
    
    Args:
        channels_data: Dictionary containing channel information
        output_dir: Directory where files will be saved
    """
    # Ensure output directory exists
    os.makedirs(output_dir, exist_ok=True)
    
    vlc_path = os.path.join(output_dir, "fooh_vlc.m3u8")
    tivimate_path = os.path.join(output_dir, "fooh_tivimate.m3u8")
    
    # Filter out channels without source
    valid_channels = {k: v for k, v in channels_data.items() if v.get("source")}
    
    if not valid_channels:
        log.warning("No valid channels found to generate M3U8 files")
        # Create empty files with headers
        with open(vlc_path, 'w', encoding='utf-8') as f:
            f.write("#EXTM3U\n")
        with open(tivimate_path, 'w', encoding='utf-8') as f:
            f.write("#EXTM3U\n")
        os.chmod(vlc_path, 0o644)
        os.chmod(tivimate_path, 0o644)
        return
    
    # Generate VLC format
    with open(vlc_path, 'w', encoding='utf-8') as vlc_file:
        vlc_file.write("#EXTM3U\n")
        chno = 1
        for key, channel in valid_channels.items():
            vlc_line = format_vlc_channel(key, channel, chno)
            vlc_file.write(vlc_line + "\n")
            chno += 1
    
    # Generate Tivimate format
    with open(tivimate_path, 'w', encoding='utf-8') as tivimate_file:
        tivimate_file.write("#EXTM3U\n")
        chno = 1
        for key, channel in valid_channels.items():
            tivimate_line = format_tivimate_channel(key, channel, chno)
            tivimate_file.write(tivimate_line + "\n")
            chno += 1
    
    # Set write permissions (read/write for owner, read for others)
    os.chmod(vlc_path, 0o644)
    os.chmod(tivimate_path, 0o644)
    
    log.info(f"Generated {vlc_path} with {chno-1} channel(s)")
    log.info(f"Generated {tivimate_path} with {chno-1} channel(s)")


def format_vlc_channel(key: str, channel: Dict[str, str | float], chno: int) -> str:
    """
    Format a channel for VLC M3U8 format.
    
    Args:
        key: Channel key
        channel: Channel data dictionary
        chno: Channel number
        
    Returns:
        Formatted string for VLC
    """
    # Extract channel info
    sport = channel.get("sport", "Live Event")
    name = channel.get("name", key)
    channel_name = channel.get("channel_name", "")
    
    # Clean display name
    display_name = clean_display_name(key.replace(f" ({TAG})", ""))
    
    # VLC format
    tvg_name = f"[{sport}] {name} | {channel_name} ({TAG})"
    
    extinf = (f'#EXTINF:-1 tvg-chno="{chno}" '
              f'tvg-id="{channel.get("tvg-id", "Live.Event.us")}" '
              f'tvg-name="{tvg_name}" '
              f'tvg-logo="{channel.get("logo", "")}" '
              f'group-title="{sport}",'
              f'{display_name}')
    
    # Add VLC options
    options = [
        f"#EXTVLCOPT:http-referrer={BASE_URL}",
        f"#EXTVLCOPT:http-origin={BASE_URL}",
        f'#EXTVLCOPT:http-user-agent={VLC_USER_AGENT}'
    ]
    
    url = channel.get("source", "")
    
    return f"{extinf}\n" + "\n".join(options) + f"\n{url}"


def format_tivimate_channel(key: str, channel: Dict[str, str | float], chno: int) -> str:
    """
    Format a channel for Tivimate M3U8 format using pipe separator.
    
    Args:
        key: Channel key
        channel: Channel data dictionary
        chno: Channel number
        
    Returns:
        Formatted string for Tivimate
    """
    # Extract channel info
    sport = channel.get("sport", "Live Event")
    name = channel.get("name", key)
    channel_name = channel.get("channel_name", "")
    
    # Clean display name for Tivimate
    display_name = clean_display_name(key.replace(f" ({TAG})", f" (FOOH)"))
    
    # Tivimate format
    tvg_name = f"[{sport}] {name} | {channel_name} (FOOH)"
    
    # Tivimate format with pipe separator
    extinf = (f'#EXTINF:-1 tvg-chno="{chno}" '
              f'tvg-id="{channel.get("tvg-id", "Live.Event.us")}" '
              f'tvg-name="{tvg_name}" '
              f'tvg-logo="{channel.get("logo", "")}" '
              f'group-title="{sport}",'
              f'{display_name}')
    
    # Encode the user agent for Tivimate
    encoded_user_agent = encode_user_agent(TIVIMATE_USER_AGENT)
    
    # Build the URL with parameters
    url = channel.get("source", "")
    params = [
        f"referer={BASE_URL}/",
        f"origin={BASE_URL}",
        f"user-agent={encoded_user_agent}"
    ]
    
    return f"{extinf}\n{url}|{'|'.join(params)}"


def encode_user_agent(user_agent: str) -> str:
    """
    Encode the user agent for URL parameters.
    
    Args:
        user_agent: User agent string
        
    Returns:
        URL-encoded user agent
    """
    # URL encode the user agent
    encoded = user_agent.replace(' ', '%20')
    encoded = encoded.replace('(', '%28')
    encoded = encoded.replace(')', '%29')
    encoded = encoded.replace(';', '%3B')
    encoded = encoded.replace(',', '%2C')
    return encoded


async def main() -> None:
    """
    Main function to run the scraper and generate M3U8 files.
    """
    log.info(f"Starting {TAG} scraper")
    await scrape()
    log.info(f"{TAG} scraper completed")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
