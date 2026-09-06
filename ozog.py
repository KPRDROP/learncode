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

if not BASE_URL:
    BASE_URL = "https://gozo.st/"

BASE_URL = BASE_URL.rstrip("/") + "/"


DEFAULT_REFERER = os.getenv("OZOG_REFERER", "").strip()

DEFAULT_ORIGIN = os.getenv("OZOG_ORIGIN", "").strip()


USER_AGENT = os.getenv(
    "OZOG_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/152.0.0.0 Safari/537.36",
).strip()


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def origin_from_url(url: str) -> str:
    """
    Return the origin of a URL.

    Example:
        https://unxer123.gozo.zip/games/test/
    becomes:
        https://unxer123.gozo.zip
    """

    parsed = urlparse(url)

    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"

    if DEFAULT_ORIGIN:
        return DEFAULT_ORIGIN.rstrip("/")

    return ""


def normalize_url(source: str, base_url: str) -> str | None:
    """
    Convert a decoded source into a valid absolute HTTP/HTTPS URL.
    """

    if not source:
        return None

    source = source.strip()
    source = source.strip("'\"")

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
# Decoder
# ---------------------------------------------------------------------------

def rot13(c: str) -> str:
    """
    ROT13 for alphabetic characters.
    """

    if "A" <= c <= "Z":
        return chr((ord(c) - 65 + 13) % 26 + 65)

    if "a" <= c <= "z":
        return chr((ord(c) - 97 + 13) % 26 + 97)

    return c


def decrypt(
    enc: str,
    xor_key: int,
    num_list: list[int],
) -> str | None:
    """
    Decode OZOG's _dd/_dk/_dri encoded source.
    """

    try:
        if not enc:
            return None

        if len(enc) % 2:
            return None

        if len(num_list) != len(enc):
            return None

        chars = list(enc)

        unshuffled = [""] * len(chars)

        for i, num in enumerate(num_list):
            if not isinstance(num, int):
                return None

            if num < 0 or num >= len(chars):
                return None

            unshuffled[num] = chars[i]

        xor_enc = "".join(unshuffled)

        if len(xor_enc) % 2:
            return None

        # Hex -> XOR.
        hex_enc = "".join(
            chr(
                int(
                    xor_enc[i:i + 2],
                    16,
                )
                ^ xor_key
            )
            for i in range(0, len(xor_enc), 2)
        )

        if len(hex_enc) % 2:
            return None

        # Hex -> characters.
        decoded = "".join(
            chr(
                int(
                    hex_enc[i:i + 2],
                    16,
                )
            )
            for i in range(0, len(hex_enc), 2)
        )

        # ROT13, then reverse.
        decoded = "".join(rot13(c) for c in decoded)
        decoded = decoded[::-1]

        # Base64.
        return base64.b64decode(
            decoded.encode("utf-8"),
            validate=True,
        ).decode("utf-8")

    except (
        ValueError,
        TypeError,
        UnicodeDecodeError,
        base64.binascii.Error,
    ):
        return None


def extract_decode_variables(
    text: str,
) -> tuple[str, int, list[int]] | None:
    """
    Extract:

        _dd = "...";
        _dk = 123;
        _dri = [...];

    from the player HTML/JavaScript.
    """

    dd_match = re.search(
        r"""
        (?:
            var\s+|
            let\s+|
            const\s+
        )?
        _dd
        \s*=\s*
        (['"])
        (.*?)
        \1
        \s*;
        """,
        text,
        re.IGNORECASE | re.DOTALL | re.VERBOSE,
    )

    dk_match = re.search(
        r"""
        (?:
            var\s+|
            let\s+|
            const\s+
        )?
        _dk
        \s*=\s*
        (-?\d+)
        \s*;
        """,
        text,
        re.IGNORECASE | re.VERBOSE,
    )

    dri_match = re.search(
        r"""
        (?:
            var\s+|
            let\s+|
            const\s+
        )?
        _dri
        \s*=\s*
        (\[[^;]*\])
        \s*;
        """,
        text,
        re.IGNORECASE | re.DOTALL | re.VERBOSE,
    )

    if not (
        dd_match
        and dk_match
        and dri_match
    ):
        return None

    try:
        dd = dd_match.group(2)

        dk = int(
            dk_match.group(1)
        )

        dri = ast.literal_eval(
            dri_match.group(1)
        )

        if not isinstance(dri, list):
            return None

        if not all(
            isinstance(x, int)
            for x in dri
        ):
            return None

        return dd, dk, dri

    except (
        ValueError,
        SyntaxError,
    ):
        return None


# ---------------------------------------------------------------------------
# Page fetching
# ---------------------------------------------------------------------------

async def request_page(
    url: str,
    url_num: int,
    referer: str | None = None,
):
    """
    Fetch a page using the project's network helper.

    If that fails, use Playwright as a browser fallback.
    """

    headers = {
        "User-Agent": USER_AGENT,
    }

    if referer:
        headers["Referer"] = referer

    result = await network.request(
        url,
        url_num,
        headers=headers,
        log=log,
    )

    if result:
        return result

    log.warning(
        f"URL {url_num}) HTTP request failed, "
        f"trying browser fallback: {url}"
    )

    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:

            browser = await playwright.chromium.launch(
                headless=True
            )

            context = await browser.new_context(
                user_agent=USER_AGENT,
            )

            page = await context.new_page()

            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=30_000,
            )

            content = await page.content()
            final_url = page.url

            await browser.close()

        class BrowserResponse:
            def __init__(
                self,
                content: str,
                url: str,
            ):
                self.content = content
                self.text = content
                self.url = url

        log.info(
            f"URL {url_num}) Browser fallback fetched page"
        )

        return BrowserResponse(
            content,
            final_url,
        )

    except Exception as exc:
        log.error(
            f"URL {url_num}) Browser fallback failed: {exc}"
        )

        return None


# ---------------------------------------------------------------------------
# Event processing
# ---------------------------------------------------------------------------

async def process_event(
    url: str,
    url_num: int,
) -> tuple[str | None, str | None]:

    if not url:
        return None, None

    # This is the URL that must be used as the playlist referer.
    event_url = url.rstrip("/") + "/"

    # -----------------------------------------------------------------------
    # Fetch event page
    # -----------------------------------------------------------------------

    html_data = await request_page(
        event_url,
        url_num,
    )

    if not html_data:
        log.error(
            f"URL {url_num}) Failed to fetch "
            f"\"{event_url}\""
        )

        return None, None

    page_url = getattr(
        html_data,
        "url",
        None,
    ) or event_url

    soup = HTMLParser(
        html_data.content
    )

    # -----------------------------------------------------------------------
    # Find iframe
    # -----------------------------------------------------------------------

    iframe = (
        soup.css_first(
            'iframe[src*="stream"]'
        )
        or soup.css_first(
            'iframe[src*="player"]'
        )
        or soup.css_first("iframe")
    )

    if not iframe:
        log.warning(
            f"URL {url_num}) No iframe element found"
        )

        return None, None

    iframe_src = iframe.attributes.get(
        "src",
        "",
    ).strip()

    if not iframe_src:
        log.warning(
            f"URL {url_num}) Iframe has no src"
        )

        return None, None

    # Correct relative iframe URLs.
    iframe_src = urljoin(
        page_url,
        iframe_src,
    )

    log.info(
        f"URL {url_num}) Player iframe: "
        f"{iframe_src}"
    )

    # -----------------------------------------------------------------------
    # Fetch iframe/player
    # -----------------------------------------------------------------------

    iframe_data = await request_page(
        iframe_src,
        url_num,
        referer=event_url,
    )

    if not iframe_data:
        log.error(
            f"URL {url_num}) Failed to fetch player iframe"
        )

        return None, None

    # -----------------------------------------------------------------------
    # Extract decoder variables
    # -----------------------------------------------------------------------

    variables = extract_decode_variables(
        iframe_data.text
    )

    if not variables:
        log.warning(
            f"URL {url_num}) Failed to gather "
            f"decoding variables"
        )

        return None, None

    dd, dk, dri = variables

    log.info(
        f"URL {url_num}) Found decoding variables "
        f"(_dd/_dk/_dri)"
    )

    # -----------------------------------------------------------------------
    # Decode source
    # -----------------------------------------------------------------------

    source = decrypt(
        dd,
        dk,
        dri,
    )

    if not source:
        log.warning(
            f"URL {url_num}) Decoding method failed"
        )

        return None, None

    source = normalize_url(
        source,
        iframe_src,
    )

    if not source:
        log.warning(
            f"URL {url_num}) Decoded source is not "
            f"a valid HTTP/HTTPS URL"
        )

        return None, None

    log.info(
        f"URL {url_num}) Captured stream source: "
        f"{source}"
    )

    # IMPORTANT:
    # Return the EVENT PAGE as the referer.
    #
    # Desired:
    # https://unxer123.gozo.zip/games/juventus-vs-milan/
    #
    # NOT:
    # https://fingersoon.st/...
    return source, event_url


# ---------------------------------------------------------------------------
# Event discovery
# ---------------------------------------------------------------------------

async def get_events(
    cached_keys: KeysView[str],
) -> list[Event]:

    events: list[Event] = []

    html_data = await request_page(
        BASE_URL,
        0,
    )

    if not html_data:
        log.error(
            f"Failed to fetch OZOG homepage: {BASE_URL}"
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
# Scraper
# ---------------------------------------------------------------------------

async def scrape() -> None:

    cached_urls = CACHE_FILE.load()

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

        event_origin = origin_from_url(
            event_url
        )

        entry = {
            "source": source,
            "logo": logo,
            "refer": (
                referer
                or event_url
            ),
            "origin": event_origin,
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
                f"Saved event: {key}"
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
            DEFAULT_REFERER
            or data.get(
                "link",
                "",
            ),
        )

        origin = data.get(
            "origin",
            DEFAULT_ORIGIN
            or origin_from_url(
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
            DEFAULT_REFERER
            or data.get(
                "link",
                "",
            ),
        )

        origin = data.get(
            "origin",
            DEFAULT_ORIGIN
            or origin_from_url(
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

        # Referer and Origin remain plain text.
        # Only User-Agent is URL encoded.
        content += (
            f"{source}"
            f"|referer={referer}"
            f"|origin={origin}"
            f"|user-agent={encoded_user_agent}\n"
        )

    return content


# ---------------------------------------------------------------------------
# Write playlist files
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

    vlc_file = os.path.join(
        output_dir,
        "ozog_vlc.m3u8",
    )

    tivimate_file = os.path.join(
        output_dir,
        "ozog_tivimate.m3u8",
    )

    vlc_content = generate_vlc_m3u8()

    tivimate_content = generate_tivimate_m3u8()

    with open(
        vlc_file,
        "w",
        encoding="utf-8",
        newline="\n",
    ) as file:
        file.write(
            vlc_content
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
        "Starting OZOG scraper"
    )

    await scrape()

    if urls:

        write_output_files()

        log.info(
            f"Successfully processed "
            f"{len(urls)} events"
        )

    else:

        log.warning(
            "No events found to write "
            "to output files"
        )


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
