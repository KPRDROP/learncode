import ast
import base64
import os
import re
from collections.abc import KeysView
from functools import partial
from urllib.parse import quote, urljoin, urlparse

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TAG = "OZOG"

CACHE_FILE = Cache(TAG, exp=28_800)

BASE_URL = "https://gozo.st/"

REFERER = "https://unxer123.gozo.zip/"
ORIGIN = "https://unxer123.gozo.zip"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def get_origin(url: str) -> str:
    """Return scheme + hostname from a URL."""
    try:
        parsed = urlparse(url)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    except Exception:
        pass
    return ORIGIN


def normalize_source(source: str | None, base_url: str) -> str | None:
    """Convert a decoded stream/source URL into an absolute HTTP/HTTPS URL."""
    if not source:
        return None

    source = source.strip().strip("\"'")
    if not source:
        return None

    # JavaScript escaped slashes
    source = source.replace("\\/", "/")

    # Protocol-relative URL
    if source.startswith("//"):
        scheme = urlparse(base_url).scheme or "https"
        source = f"{scheme}:{source}"

    # Relative URL
    elif not re.match(r"^https?://", source, re.IGNORECASE):
        source = urljoin(base_url, source)

    if re.match(r"^https?://", source, re.IGNORECASE):
        return source

    return None


# ---------------------------------------------------------------------------
# ROT13
# ---------------------------------------------------------------------------

def rot13(c: str) -> str:
    """Same ROT13 operation used by the original OZOG JavaScript."""
    code = ord(c)
    if "A" <= c <= "Z":
        base = ord("A")
    elif "a" <= c <= "z":
        base = ord("a")
    else:
        return c
    return chr((code - base + 13) % 26 + base)


# ---------------------------------------------------------------------------
# OZOG decrypt
# ---------------------------------------------------------------------------

def decrypt(enc: str, xor_key: int, num_list: list[int]) -> str | None:
    """
    Python equivalent of the original OZOG JavaScript _decrypt().
    Processing order: 1. Unshuffle 2. XOR 3. Hex decode 4. ROT13 5. Reverse 6. Base64 decode
    """
    try:
        if not enc or len(enc) % 2 != 0:
            return None

        if len(num_list) != len(enc):
            return None

        # Layer 6: unshuffle
        chars = list(enc)
        unshuffled = [""] * len(chars)
        for i, num in enumerate(num_list):
            if not isinstance(num, int) or num < 0 or num >= len(chars):
                return None
            unshuffled[num] = chars[i]
        xor_encoded = "".join(unshuffled)

        # Layer 5: XOR
        hex_encoded = ""
        for i in range(0, len(xor_encoded), 2):
            byte = int(xor_encoded[i:i + 2], 16)
            hex_encoded += chr(byte ^ xor_key)

        # Layer 4: Hex decode
        rot13_string = ""
        for i in range(0, len(hex_encoded), 2):
            rot13_string += chr(int(hex_encoded[i:i + 2], 16))

        # Layer 3: ROT13
        rotated = "".join(rot13(c) for c in rot13_string)

        # Layer 2: Reverse
        base64_data = rotated[::-1]

        # Layer 1: Base64 decode
        return base64.b64decode(base64_data.encode("utf-8")).decode("utf-8")

    except Exception as exc:
        log.debug(f"OZOG decoder failed: {exc}")
        return None


# ---------------------------------------------------------------------------
# Extract decoder variables
# ---------------------------------------------------------------------------

def extract_decoder_data(text: str) -> tuple[str, int, list[int]] | None:
    """Extract _dd, _dk and _dri from the OZOG player HTML."""
    if not text:
        return None

    # _dd
    dd_match = re.search(
        r"""
        (?:
            var\s+|
            let\s+|
            const\s+
        )?
        _dd
        \s*=\s*
        (?P<quote>["'])
        (?P<value>.*?)
        (?P=quote)
        \s*;
        """,
        text,
        re.IGNORECASE | re.DOTALL | re.VERBOSE,
    )
    if not dd_match:
        return None
    dd = dd_match.group("value")

    # _dk
    dk_match = re.search(
        r"""
        (?:
            var\s+|
            let\s+|
            const\s+
        )?
        _dk
        \s*=\s*
        (?P<value>-?\d+)
        \s*;
        """,
        text,
        re.IGNORECASE | re.VERBOSE,
    )
    if not dk_match:
        return None
    try:
        dk = int(dk_match.group("value"))
    except ValueError:
        return None

    # _dri
    dri_match = re.search(
        r"""
        (?:
            var\s+|
            let\s+|
            const\s+
        )?
        _dri
        \s*=\s*
        (?P<value>\[[^\]]*\])
        \s*;
        """,
        text,
        re.IGNORECASE | re.DOTALL | re.VERBOSE,
    )
    if not dri_match:
        return None
    try:
        dri = ast.literal_eval(dri_match.group("value"))
    except (ValueError, SyntaxError):
        return None

    if not isinstance(dri, list) or not all(isinstance(item, int) for item in dri):
        return None

    return (dd, dk, dri)


# ---------------------------------------------------------------------------
# Process event
# ---------------------------------------------------------------------------

async def process_event(url: str, url_num: int) -> tuple[str | None, str | None]:
    if not url:
        return None, None

    # Ensure the URL has a trailing slash for proper joining
    event_url = url.rstrip("/") + "/"

    log.info(f'URL {url_num}) Processing "{event_url}"')

    # Step 1: Fetch the event page from gozo.st
    html_data = await network.request(event_url, url_num, log=log)
    if not html_data:
        log.error(f'URL {url_num}) Failed to fetch "{event_url}"')
        return None, None

    page_text = getattr(html_data, "text", html_data.content)

    # Step 2: Look for iframe with the player URL
    soup = HTMLParser(html_data.content)
    iframe = soup.css_first(".player-box iframe")
    
    if not iframe:
        log.warning(f"URL {url_num}) No player iframe found")
        return None, None

    iframe_src = iframe.attributes.get("src")
    if not iframe_src:
        log.warning(f"URL {url_num}) Iframe has no src attribute")
        return None, None

    # Make relative iframe URL absolute
    iframe_src = urljoin(event_url, iframe_src)
    log.info(f"URL {url_num}) Found player iframe: {iframe_src}")

    # Step 3: Fetch the player iframe content (this contains the encrypted stream)
    iframe_data = await network.request(
        iframe_src,
        url_num,
        headers={"Referer": event_url},
        log=log,
    )
    if not iframe_data:
        log.warning(f"URL {url_num}) Failed to fetch player iframe")
        return None, None

    iframe_text = getattr(iframe_data, "text", iframe_data.content)

    # Step 4: Extract decoder data from the iframe
    decoder_data = extract_decoder_data(iframe_text)
    if not decoder_data:
        log.warning(f"URL {url_num}) Failed to extract decoder data from player")
        return None, None

    dd, dk, dri = decoder_data
    log.info(f"URL {url_num}) Found player decoder")

    # Step 5: Decrypt the stream URL
    source = decrypt(dd, dk, dri)
    source = normalize_source(source, iframe_src)

    if not source:
        log.warning(f"URL {url_num}) Failed to decrypt stream URL")
        return None, None

    log.info(f"URL {url_num}) Captured stream source: {source}")
    
    # Return the stream URL with the event page as referer
    return source, event_url


# ---------------------------------------------------------------------------
# Event discovery
# ---------------------------------------------------------------------------

async def get_events(cached_keys: KeysView[str]) -> list[Event]:
    events: list[Event] = []

    if not (html_data := await network.request(BASE_URL, log=log)):
        log.error(f'Failed to fetch "{BASE_URL}"')
        return events

    soup = HTMLParser(html_data.content)

    for card in soup.css(".card-inner"):
        if not all(
            values := [
                card.css_first(selector)
                for selector in (".sport-tag", ".teams", "a.watch-btn")
            ]
        ):
            continue

        sport_elem, teams_elem, watch_btn_elem = values
        sport = sport_elem.text(strip=True).capitalize()
        sport = "Live Event" if sport == "Sports" else sport

        teams = teams_elem.css(".team-name")
        if not teams:
            continue

        event_name = " vs ".join(team.text(strip=True) for team in teams)
        key = f"[{sport}] {event_name} ({TAG})"

        if key in cached_keys:
            continue

        href = watch_btn_elem.attributes.get("href")
        if not href:
            continue

        events.append(
            Event(
                sport=sport,
                name=event_name,
                link=urljoin(BASE_URL, href),
            )
        )

    return events


# ---------------------------------------------------------------------------
# Scrape
# ---------------------------------------------------------------------------

async def scrape() -> None:
    cached_urls = CACHE_FILE.load()
    valid_urls = {k: v for k, v in cached_urls.items() if v.get("source")}
    valid_count = cached_count = len(valid_urls)

    urls.clear()
    urls.update(valid_urls)

    log.info(f"Loaded {cached_count} event(s) from cache")
    log.info(f'Scraping from "{BASE_URL}"')

    events = await get_events(cached_urls.keys())
    if not events:
        log.info("No new events found")
        CACHE_FILE.write(cached_urls)
        return

    log.info(f"Processing {len(events)} new URL(s)")
    now = Time.rn()

    for i, ev in enumerate(events, start=1):
        handler = partial(process_event, url=ev.link, url_num=i)
        source, referer = await network.safe_process(
            handler,
            url_num=i,
            timeout_return=(None, None),
            semaphore=network.HTTP_S,
            log=log,
        )

        key = f"[{ev.sport}] {ev.name} ({TAG})"
        tvg_id, logo = leagues.get_tvg_info(ev.sport, ev.name)
        event_url = ev.link.rstrip("/") + "/"
        event_origin = get_origin(event_url)

        entry = {
            "source": source,
            "logo": logo,
            "refer": referer or event_url or REFERER,
            "origin": event_origin or ORIGIN,
            "timestamp": now.timestamp(),
            "tvg-id": tvg_id or "Live.Event.us",
            "link": event_url,
        }

        cached_urls[key] = entry

        if source:
            valid_count += 1
            urls[key] = entry
            log.info(f"URL {i}) Saved event: {key}")
        else:
            log.warning(f"No stream source for: {key}")

    log.info(f"Collected and cached {valid_count - cached_count} new event(s)")
    CACHE_FILE.write(cached_urls)


# ---------------------------------------------------------------------------
# VLC playlist
# ---------------------------------------------------------------------------

def generate_vlc_m3u8() -> str:
    content = "#EXTM3U\n"
    playlist_index = 0

    for title, data in urls.items():
        source = data.get("source")
        if not source:
            continue

        playlist_index += 1
        tvg_id = data.get("tvg-id", "Live.Event.us")
        logo = data.get("logo", "")
        referer = data.get("refer", REFERER)
        origin = data.get("origin", ORIGIN)

        content += (
            f'#EXTINF:-1 '
            f'tvg-chno="{playlist_index}" '
            f'tvg-id="{tvg_id}" '
            f'tvg-name="{title}" '
            f'tvg-logo="{logo}" '
            f'group-title="Live Events",'
            f'{title}\n'
        )
        content += f"#EXTVLCOPT:http-referrer={referer}\n"
        content += f"#EXTVLCOPT:http-origin={origin}\n"
        content += f"#EXTVLCOPT:http-user-agent={USER_AGENT}\n"
        content += f"{source}\n"

    return content


# ---------------------------------------------------------------------------
# TiviMate playlist
# ---------------------------------------------------------------------------

def generate_tivimate_m3u8() -> str:
    content = "#EXTM3U\n"
    encoded_user_agent = quote(USER_AGENT, safe="")
    playlist_index = 0

    for title, data in urls.items():
        source = data.get("source")
        if not source:
            continue

        playlist_index += 1
        tvg_id = data.get("tvg-id", "Live.Event.us")
        logo = data.get("logo", "")
        referer = data.get("refer", REFERER)
        origin = data.get("origin", ORIGIN)

        content += (
            f'#EXTINF:-1 '
            f'tvg-chno="{playlist_index}" '
            f'tvg-id="{tvg_id}" '
            f'tvg-name="{title}" '
            f'tvg-logo="{logo}" '
            f'group-title="Live Events",'
            f'{title}\n'
        )
        content += (
            f"{source}"
            f"|referer={referer}"
            f"|origin={origin}"
            f"|user-agent={encoded_user_agent}\n"
        )

    return content


# ---------------------------------------------------------------------------
# Write output files
# ---------------------------------------------------------------------------

def write_output_files() -> None:
    output_dir = os.getenv("OUTPUT_DIR", ".")
    os.makedirs(output_dir, exist_ok=True)

    # VLC
    vlc_file = os.path.join(output_dir, "ozog_vlc.m3u8")
    with open(vlc_file, "w", encoding="utf-8", newline="\n") as f:
        f.write(generate_vlc_m3u8())

    # TiviMate
    tivimate_file = os.path.join(output_dir, "ozog_tivimate.m3u8")
    with open(tivimate_file, "w", encoding="utf-8", newline="\n") as f:
        f.write(generate_tivimate_m3u8())

    log.info(f"Generated VLC playlist: {vlc_file}")
    log.info(f"Generated TiviMate playlist: {tivimate_file}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    log.info("Starting OZOG updater")
    await scrape()

    if urls:
        write_output_files()
        log.info(f"Successfully processed {len(urls)} event(s)")
    else:
        log.warning("No events found to write to output files")


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
