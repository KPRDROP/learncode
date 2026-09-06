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

TAG = "OZOG"

CACHE_FILE = Cache(TAG, exp=28_800)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = os.getenv(
    "OZOG_BASE_URL",
).strip()

if not BASE_URL:
    BASE_URL = "https://gozo.st/"

BASE_URL = BASE_URL.rstrip("/") + "/"

USER_AGENT = os.getenv(
    "OZOG_USER_AGENT",
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/152.0.0.0 Safari/537.36"
    ),
).strip()


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def get_origin(url: str) -> str:
    """
    Return scheme + hostname (+ port when present).

    Example:
        https://unxer123.gozo.zip/games/test/

    becomes:
        https://unxer123.gozo.zip
    """

    parsed = urlparse(url)

    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"

    return ""


def normalize_source(
    source: str | None,
    base_url: str,
) -> str | None:
    """
    Convert a decoded source into an absolute HTTP/HTTPS URL.
    """

    if not source:
        return None

    source = source.strip()
    source = source.strip("\"'")

    if not source:
        return None

    # JavaScript escaped slash.
    source = source.replace("\\/", "/")

    # Protocol-relative URL.
    if source.startswith("//"):
        scheme = urlparse(base_url).scheme or "https"
        source = f"{scheme}:{source}"

    # Relative URL.
    elif not re.match(r"^https?://", source, re.I):
        source = urljoin(base_url, source)

    if re.match(r"^https?://", source, re.I):
        return source

    return None


# ---------------------------------------------------------------------------
# Original OZOG decoder
# ---------------------------------------------------------------------------

def rot13(c: str) -> str:
    """
    Same ROT13 implementation used by the original OZOG JavaScript.
    """

    if "A" <= c <= "Z":
        return chr(
            (ord(c) - ord("A") + 13) % 26
            + ord("A")
        )

    if "a" <= c <= "z":
        return chr(
            (ord(c) - ord("a") + 13) % 26
            + ord("a")
        )

    return c


def decrypt(
    enc: str,
    xor_key: int,
    num_list: list[int],
) -> str | None:
    """
    Python equivalent of the original JavaScript _decrypt().

    Original order:

        1. Unshuffle
        2. XOR
        3. Hex decode
        4. ROT13
        5. Reverse
        6. Base64 decode
    """

    try:
        if not enc:
            return None

        if len(enc) % 2 != 0:
            return None

        if len(num_list) != len(enc):
            log.debug(
                "Decoder: _dri length does not match _dd length"
            )
            return None

        # ---------------------------------------------------------------
        # Layer 6: unshuffle
        # ---------------------------------------------------------------

        chars = list(enc)

        unshuffled = [""] * len(chars)

        for i, num in enumerate(num_list):

            if not isinstance(num, int):
                return None

            if num < 0 or num >= len(chars):
                return None

            unshuffled[num] = chars[i]

        xor_encoded = "".join(
            unshuffled
        )

        # ---------------------------------------------------------------
        # Layer 5: XOR
        # ---------------------------------------------------------------

        hex_encoded = ""

        for i in range(
            0,
            len(xor_encoded),
            2,
        ):

            byte = int(
                xor_encoded[i:i + 2],
                16,
            )

            hex_encoded += chr(
                byte ^ xor_key
            )

        # ---------------------------------------------------------------
        # Layer 4: hex decode
        # ---------------------------------------------------------------

        rot13_str = ""

        for i in range(
            0,
            len(hex_encoded),
            2,
        ):

            rot13_str += chr(
                int(
                    hex_encoded[i:i + 2],
                    16,
                )
            )

        # ---------------------------------------------------------------
        # Layer 3: ROT13
        # ---------------------------------------------------------------

        reversed_text = "".join(
            rot13(c)
            if (
                "a" <= c <= "z"
                or "A" <= c <= "Z"
            )
            else c
            for c in rot13_str
        )

        # ---------------------------------------------------------------
        # Layer 2: reverse
        # ---------------------------------------------------------------

        encoded_base64 = (
            reversed_text[::-1]
        )

        # ---------------------------------------------------------------
        # Layer 1: Base64
        # ---------------------------------------------------------------

        decoded = base64.b64decode(
            encoded_base64.encode(
                "utf-8"
            )
        ).decode(
            "utf-8"
        )

        return decoded

    except Exception as exc:
        log.debug(
            f"Decoder failed: {exc}"
        )
        return None


# ---------------------------------------------------------------------------
# Extract _dd / _dk / _dri
# ---------------------------------------------------------------------------

def extract_decoder_data(
    text: str,
) -> tuple[str, int, list[int]] | None:
    """
    Extract the direct-stream encryption variables from the event page.

    Supports:

        const _dk = 87;
        const _dd = "...";
        const _dri = [...];

    as well as var/let/no declaration.
    """

    if not text:
        return None

    # ---------------------------------------------------------------
    # _dd
    # ---------------------------------------------------------------

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

    dd = dd_match.group(
        "value"
    )

    # ---------------------------------------------------------------
    # _dk
    # ---------------------------------------------------------------

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
        dk = int(
            dk_match.group(
                "value"
            )
        )
    except ValueError:
        return None

    # ---------------------------------------------------------------
    # _dri
    # ---------------------------------------------------------------

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
        dri = ast.literal_eval(
            dri_match.group(
                "value"
            )
        )
    except (
        ValueError,
        SyntaxError,
    ):
        return None

    if not isinstance(
        dri,
        list,
    ):
        return None

    if not all(
        isinstance(
            item,
            int,
        )
        for item in dri
    ):
        return None

    return (
        dd,
        dk,
        dri,
    )


# ---------------------------------------------------------------------------
# HTTP request
# ---------------------------------------------------------------------------

async def request_page(
    url: str,
    url_num: int,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
):
    """
    Use the existing project network.request() implementation.

    This intentionally keeps the same network layer used by the
    original working OZOG code.
    """

    request_headers = {
        "User-Agent": USER_AGENT,
    }

    if headers:
        request_headers.update(
            headers
        )

    return await network.request(
        url,
        url_num,
        headers=request_headers,
        params=params,
        log=log,
    )


# ---------------------------------------------------------------------------
# Process event
# ---------------------------------------------------------------------------

async def process_event(
    url: str,
    url_num: int,
) -> tuple[str | None, str | None]:

    if not url:
        return None, None

    # Normalize event URL.
    event_url = url.rstrip("/") + "/"

    log.info(
        f'URL {url_num}) Processing "{event_url}"'
    )

    # -----------------------------------------------------------------------
    # STEP 1
    #
    # Load the EVENT PAGE.
    #
    # This is important because the current OZOG page contains _dd/_dk/_dri
    # directly in the event HTML.
    # -----------------------------------------------------------------------

    html_data = await request_page(
        event_url,
        url_num,
    )

    if not html_data:
        log.error(
            f'URL {url_num}) Failed to fetch "{event_url}"'
        )

        return None, None

    page_text = getattr(
        html_data,
        "text",
        "",
    )

    if not page_text:
        page_text = html_data.content

    # -----------------------------------------------------------------------
    # STEP 2
    #
    # PRIMARY METHOD:
    #
    # Decode the direct source directly from the event page.
    # -----------------------------------------------------------------------

    decoder_data = extract_decoder_data(
        page_text
    )

    if decoder_data:

        dd, dk, dri = decoder_data

        log.info(
            f"URL {url_num}) Found direct "
            f"_dd/_dk/_dri configuration"
        )

        source = decrypt(
            dd,
            dk,
            dri,
        )

        source = normalize_source(
            source,
            event_url,
        )

        if source:

            log.info(
                f"URL {url_num}) Captured stream source"
            )

            # IMPORTANT:
            # Return the EVENT URL as referer.
            return (
                source,
                event_url,
            )

        log.warning(
            f"URL {url_num}) Direct decoder "
            f"returned an invalid source"
        )

    else:

        log.info(
            f"URL {url_num}) No direct decoder "
            f"found on event page"
        )

    # -----------------------------------------------------------------------
    # STEP 3
    #
    # FALLBACK:
    #
    # Preserve the original working iframe method.
    # -----------------------------------------------------------------------

    soup = HTMLParser(
        html_data.content
    )

    iframe = (
        soup.css_first(
            'iframe[name="srcFrame"]'
        )
        or soup.css_first(
            'iframe[src*="stream"]'
        )
        or soup.css_first(
            'iframe[src*="player"]'
        )
        or soup.css_first(
            "iframe"
        )
    )

    if not iframe:

        log.warning(
            f"URL {url_num}) No iframe element found "
            f"and no direct stream configuration"
        )

        return None, None

    iframe_src = iframe.attributes.get(
        "src"
    )

    if not iframe_src:

        log.warning(
            f"URL {url_num}) No iframe source found"
        )

        return None, None

    # Correct relative iframe URLs.
    iframe_src = urljoin(
        event_url,
        iframe_src,
    )

    log.info(
        f"URL {url_num}) Found iframe: "
        f"{iframe_src}"
    )

    # -----------------------------------------------------------------------
    # STEP 4
    #
    # Fetch iframe using EVENT URL as Referer.
    # -----------------------------------------------------------------------

    iframe_data = await request_page(
        iframe_src,
        url_num,
        headers={
            "Referer": event_url,
        },
    )

    if not iframe_data:

        log.warning(
            f"URL {url_num}) Failed to fetch iframe source"
        )

        return None, None

    iframe_text = getattr(
        iframe_data,
        "text",
        "",
    )

    if not iframe_text:
        iframe_text = iframe_data.content

    # -----------------------------------------------------------------------
    # STEP 5
    #
    # Try the same _dd/_dk/_dri decoder inside the iframe.
    # -----------------------------------------------------------------------

    decoder_data = extract_decoder_data(
        iframe_text
    )

    if decoder_data:

        dd, dk, dri = decoder_data

        log.info(
            f"URL {url_num}) Found decoder "
            f"inside iframe"
        )

        source = decrypt(
            dd,
            dk,
            dri,
        )

        source = normalize_source(
            source,
            iframe_src,
        )

        if source:

            log.info(
                f"URL {url_num}) Captured stream source "
                f"from iframe"
            )

            return (
                source,
                event_url,
            )

    # -----------------------------------------------------------------------
    # No supported stream configuration found.
    # -----------------------------------------------------------------------

    log.warning(
        f"URL {url_num}) Failed to extract stream source"
    )

    return None, None


# ---------------------------------------------------------------------------
# Event discovery
# ---------------------------------------------------------------------------

async def get_events(
    cached_keys: KeysView[str],
) -> list[Event]:

    events: list[Event] = []

    html_data = await network.request(
        BASE_URL,
        log=log,
    )

    if not html_data:
        log.error(
            f'Failed to fetch "{BASE_URL}"'
        )
        return events

    soup = HTMLParser(
        html_data.content
    )

    for card in soup.css(
        ".card-inner"
    ):

        values = [
            card.css_first(selector)
            for selector in (
                ".sport-tag",
                ".teams",
                "a.watch-btn",
            )
        ]

        if not all(values):
            continue

        (
            sport_elem,
            teams_elem,
            watch_btn_elem,
        ) = values

        sport = sport_elem.text(
            strip=True
        ).capitalize()

        if sport == "Sports":
            sport = "Live Event"

        teams = teams_elem.css(
            ".team-name"
        )

        if not teams:
            continue

        event_name = " vs ".join(
            team.text(strip=True)
            for team in teams
        )

        key = (
            f"[{sport}] "
            f"{event_name} "
            f"({TAG})"
        )

        if key in cached_keys:
            continue

        href = watch_btn_elem.attributes.get(
            "href"
        )

        if not href:
            continue

        event_url = urljoin(
            BASE_URL,
            href,
        )

        events.append(
            Event(
                sport=sport,
                name=event_name,
                link=event_url,
            )
        )

    return events


# ---------------------------------------------------------------------------
# Scrape
# ---------------------------------------------------------------------------

async def scrape() -> None:

    cached_urls = CACHE_FILE.load()

    # Keep compatibility with both old and new cache entries.
    valid_urls = {
        key: value
        for key, value in cached_urls.items()
        if value.get("source")
    }

    cached_count = len(valid_urls)
    valid_count = cached_count

    urls.clear()
    urls.update(valid_urls)

    log.info(
        f"Loaded {cached_count} event(s) from cache"
    )

    log.info(
        f'Scraping from "{BASE_URL}"'
    )

    events = await get_events(
        cached_urls.keys()
    )

    if not events:

        log.info(
            "No new events found"
        )

        CACHE_FILE.write(
            cached_urls
        )

        return

    log.info(
        f"Processing {len(events)} new URL(s)"
    )

    now = Time.rn()

    for i, ev in enumerate(
        events,
        start=1,
    ):

        handler = partial(
            process_event,
            url=ev.link,
            url_num=i,
        )

        source, referer = await network.safe_process(
            handler,
            url_num=i,
            timeout_return=(
                None,
                None,
            ),
            semaphore=network.HTTP_S,
            log=log,
        )

        key = (
            f"[{ev.sport}] "
            f"{ev.name} "
            f"({TAG})"
        )

        tvg_id, logo = leagues.get_tvg_info(
            ev.sport,
            ev.name,
        )

        event_url = (
            ev.link.rstrip("/")
            + "/"
        )

        origin = get_origin(
            event_url
        )

        entry = {
            "source": source,
            "logo": logo,
            "refer": (
                referer
                or event_url
            ),
            "origin": origin,
            "timestamp": now.timestamp(),
            "tvg-id": (
                tvg_id
                or "Live.Event.us"
            ),
            "link": event_url,
        }

        cached_urls[key] = entry

        if source:

            valid_count += 1

            urls[key] = entry

            log.info(
                f"URL {i}) Stream saved: {key}"
            )

        else:

            log.warning(
                f"No stream source for: {key}"
            )

    log.info(
        f"Collected and cached "
        f"{valid_count - cached_count} "
        f"new event(s)"
    )

    CACHE_FILE.write(
        cached_urls
    )


# ---------------------------------------------------------------------------
# VLC playlist
# ---------------------------------------------------------------------------

def generate_vlc_m3u8() -> str:

    content = "#EXTM3U\n"

    playlist_index = 0

    for title, data in urls.items():

        source = data.get(
            "source"
        )

        if not source:
            continue

        playlist_index += 1

        tvg_id = data.get(
            "tvg-id",
            "Live.Event.us",
        )

        logo = data.get(
            "logo",
            "",
        )

        referer = data.get(
            "refer",
            data.get(
                "link",
                "",
            ),
        )

        origin = data.get(
            "origin",
            get_origin(
                str(referer)
            ),
        )

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
            f"#EXTVLCOPT:http-referrer="
            f"{referer}\n"
        )

        content += (
            f"#EXTVLCOPT:http-origin="
            f"{origin}\n"
        )

        content += (
            f"#EXTVLCOPT:http-user-agent="
            f"{USER_AGENT}\n"
        )

        content += (
            f"{source}\n"
        )

    return content


# ---------------------------------------------------------------------------
# TiviMate playlist
# ---------------------------------------------------------------------------

def generate_tivimate_m3u8() -> str:

    content = "#EXTM3U\n"

    # Keep User-Agent URL encoded.
    encoded_user_agent = quote(
        USER_AGENT,
        safe="",
    )

    playlist_index = 0

    for title, data in urls.items():

        source = data.get(
            "source"
        )

        if not source:
            continue

        playlist_index += 1

        tvg_id = data.get(
            "tvg-id",
            "Live.Event.us",
        )

        logo = data.get(
            "logo",
            "",
        )

        referer = data.get(
            "refer",
            data.get(
                "link",
                "",
            ),
        )

        origin = data.get(
            "origin",
            get_origin(
                str(referer)
            ),
        )

        content += (
            f'#EXTINF:-1 '
            f'tvg-chno="{playlist_index}" '
            f'tvg-id="{tvg_id}" '
            f'tvg-name="{title}" '
            f'tvg-logo="{logo}" '
            f'group-title="Live Events",'
            f'{title}\n'
        )

        # IMPORTANT:
        # Referer = plain text
        # Origin  = plain text
        # User-Agent = URL encoded
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

    output_dir = (
        os.getenv(
            "OUTPUT_DIR",
            ".",
        )
        or "."
    )

    os.makedirs(
        output_dir,
        exist_ok=True,
    )

    # ---------------------------------------------------------------
    # VLC
    # ---------------------------------------------------------------

    vlc_file = os.path.join(
        output_dir,
        "ozog_vlc.m3u8",
    )

    vlc_content = generate_vlc_m3u8()

    with open(
        vlc_file,
        "w",
        encoding="utf-8",
        newline="\n",
    ) as file:

        file.write(
            vlc_content
        )

    # ---------------------------------------------------------------
    # TiviMate
    # ---------------------------------------------------------------

    tivimate_file = os.path.join(
        output_dir,
        "ozog_tivimate.m3u8",
    )

    tivimate_content = (
        generate_tivimate_m3u8()
    )

    with open(
        tivimate_file,
        "w",
        encoding="utf-8",
        newline="\n",
    ) as file:

        file.write(
            tivimate_content
        )

    log.info(
        f"Generated VLC playlist: "
        f"{vlc_file}"
    )

    log.info(
        f"Generated TiviMate playlist: "
        f"{tivimate_file}"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:

    log.info(
        "Starting OZOG updater"
    )

    await scrape()

    if urls:

        write_output_files()

        log.info(
            f"Successfully processed "
            f"{len(urls)} event(s)"
        )

    else:

        log.warning(
            "No events found to write "
            "to output files"
        )


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":

    import asyncio

    asyncio.run(
        main()
    )
