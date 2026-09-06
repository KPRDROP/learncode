```python
import ast
import asyncio
import base64
import json
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

# ================= CONFIG =================

BASE_URL = os.environ.get("WEBTV_OZOG_BASE_URL")
BASE_URL = BASE_URL.rstrip("/") + "/"

# These are only fallbacks.
# The actual event URL is used whenever possible.
REFERER = os.environ.get("WEBTV_OZOG_REFERER", BASE_URL)
ORIGIN = os.environ.get(
    "WEBTV_OZOG_ORIGIN",
    f"{urlparse(BASE_URL).scheme}://{urlparse(BASE_URL).netloc}",
)

USER_AGENT = os.environ.get(
    "WEBTV_OZOG_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/152.0.0.0 Safari/537.36",
)

# Keep the two existing output files.
OUT_VLC = "ozog_vlc.m3u8"
OUT_TIVI = "ozog_tivimate.m3u8"


# ============================================================
# HELPERS
# ============================================================

def get_origin(url: str) -> str:
    """
    Return scheme + host from a URL.

    Example:
        https://unxer123.gozo.zip/games/foo/
    becomes:
        https://unxer123.gozo.zip
    """
    try:
        parsed = urlparse(url)

        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"

    except Exception:
        pass

    return ORIGIN


def clean_url(url: str | None, base: str | None = None) -> str | None:
    """
    Normalize/resolve a URL.
    """
    if not url:
        return None

    url = str(url).strip().strip("'\"")

    if not url:
        return None

    if url.startswith("//"):
        parsed = urlparse(base or BASE_URL)
        return f"{parsed.scheme}:{url}"

    if base:
        return urljoin(base, url)

    return url


def is_m3u8(url: str | None) -> bool:
    if not url:
        return False

    value = url.lower()

    return (
        ".m3u8" in value
        or "m3u8" in value
        or "/hls/" in value
        or "playlist" in value and ("http://" in value or "https://" in value)
    )


def extract_urls(text: str) -> list[str]:
    """
    Find HTTP(S) URLs from arbitrary HTML/JavaScript.

    This is deliberately broader than only searching for .m3u8 because
    some players construct the final URL through JavaScript.
    """
    if not text:
        return []

    found: list[str] = []

    patterns = [
        r'https?://[^\'"\s<>\\]+',
        r'["\'](//[^\'"\s<>\\]+)["\']',
    ]

    for pattern in patterns:
        for match in re.findall(pattern, text, re.I):
            if isinstance(match, tuple):
                match = match[0]

            value = str(match)

            if value.startswith("//"):
                value = clean_url(value)

            if value and value not in found:
                found.append(value)

    return found


def extract_m3u8_from_text(text: str) -> str | None:
    """
    Search HTML/JavaScript for an HLS URL.

    Handles:
      https://example.com/file.m3u8
      //example.com/file.m3u8
      escaped URLs
      JSON encoded URLs
    """
    if not text:
        return None

    candidates: list[str] = []

    # Normal URLs.
    patterns = [
        r'https?://[^\'"\s<>\\]+?\.m3u8(?:\?[^\'"\s<>\\]*)?',
        r'//[^\'"\s<>\\]+?\.m3u8(?:\?[^\'"\s<>\\]*)?',
        r'https?:\\?/\\?/[^\'"\s<>]+?\.m3u8(?:\\?[^\'"\s<>]*)?',
    ]

    for pattern in patterns:
        candidates.extend(re.findall(pattern, text, re.I))

    # JSON escaped URLs.
    try:
        decoded = text.replace("\\/", "/").replace("\\u0026", "&")
        candidates.extend(
            re.findall(
                r'https?://[^\'"\s<>]+?\.m3u8(?:\?[^\'"\s<>]*)?',
                decoded,
                re.I,
            )
        )
    except Exception:
        pass

    for candidate in candidates:
        candidate = candidate.strip("'\" ,;")

        candidate = candidate.replace("\\/", "/")
        candidate = candidate.replace("\\u0026", "&")

        if candidate.startswith("//"):
            candidate = clean_url(candidate)

        if is_m3u8(candidate):
            return candidate

    return None


# ============================================================
# DECRYPTION
# ============================================================

def rot13(c: str) -> str:
    """
    Rotate only ASCII letters by 13.
    """
    if "a" <= c <= "z":
        return chr((ord(c) - ord("a") + 13) % 26 + ord("a"))

    if "A" <= c <= "Z":
        return chr((ord(c) - ord("A") + 13) % 26 + ord("A"))

    return c


def decrypt(
    enc: str,
    xor_key: int,
    num_list: list[int],
) -> str | None:
    """
    Decode the OZOG player payload.

    Original process:
      1. Unshuffle characters
      2. Convert hex
      3. XOR
      4. Convert resulting hex
      5. ROT13
      6. Reverse
      7. Base64 decode

    The function is kept compatible with the original implementation,
    but validates every stage.
    """
    try:
        if not enc or not num_list:
            return None

        chars = list(enc)

        if len(num_list) != len(chars):
            log.debug(
                "Decrypt: shuffle list length %s != encrypted length %s",
                len(num_list),
                len(chars),
            )
            return None

        # Validate permutation.
        if sorted(num_list) != list(range(len(chars))):
            log.debug("Decrypt: invalid shuffle permutation")
            return None

        unshuffled = [""] * len(chars)

        for index, position in enumerate(num_list):
            unshuffled[position] = chars[index]

        xor_enc = "".join(unshuffled)

        # Hex requires an even number of characters.
        if len(xor_enc) % 2:
            log.debug("Decrypt: XOR hex string has odd length")
            return None

        xor_bytes = bytearray()

        for i in range(0, len(xor_enc), 2):
            pair = xor_enc[i:i + 2]

            try:
                value = int(pair, 16)
            except ValueError:
                log.debug("Decrypt: invalid hexadecimal pair %s", pair)
                return None

            xor_bytes.append(value ^ xor_key)

        hex_enc = xor_bytes.decode("latin-1")

        if len(hex_enc) % 2:
            log.debug("Decrypt: second hex string has odd length")
            return None

        decoded_chars: list[str] = []

        for i in range(0, len(hex_enc), 2):
            pair = hex_enc[i:i + 2]

            try:
                decoded_chars.append(chr(int(pair, 16)))
            except ValueError:
                log.debug("Decrypt: invalid second hexadecimal pair")
                return None

        rot13_str = "".join(decoded_chars)

        transformed = "".join(
            rot13(c)
            if ("a" <= c <= "z" or "A" <= c <= "Z")
            else c
            for c in rot13_str
        )

        reversed_str = transformed[::-1]

        # Validate base64.
        try:
            raw = base64.b64decode(
                reversed_str.encode("utf-8"),
                validate=False,
            )
        except Exception as exc:
            log.debug("Decrypt: base64 decode failed: %s", exc)
            return None

        try:
            result = raw.decode("utf-8").strip()
        except UnicodeDecodeError:
            result = raw.decode("latin-1").strip()

        if not result:
            return None

        return result

    except Exception as exc:
        log.debug("Decrypt exception: %s", exc)
        return None


# ============================================================
# PLAYER PAYLOAD
# ============================================================

def extract_decryption_variables(
    text: str,
) -> tuple[str, int, list[int]] | None:
    """
    Extract _dd, _dk and _dri from the player source.

    Supports variations such as:

        var _dd = "...";
        _dd="...";

        var _dk = 123;
        _dk=123;

        var _dri = [1,2,3];
        _dri=[1, 2, 3];
    """
    if not text:
        return None

    # Don't require a specific whitespace pattern.
    dd_patterns = [
        r'\b_dd\s*=\s*["\']([^"\']+)["\']',
        r'\b_dd\s*:\s*["\']([^"\']+)["\']',
    ]

    dk_patterns = [
        r'\b_dk\s*=\s*(-?\d+)',
        r'\b_dk\s*:\s*(-?\d+)',
    ]

    dri_patterns = [
        r'\b_dri\s*=\s*(\[[^\]]+\])',
        r'\b_dri\s*:\s*(\[[^\]]+\])',
    ]

    dd_match = None
    dk_match = None
    dri_match = None

    for pattern in dd_patterns:
        dd_match = re.search(pattern, text, re.I | re.S)

        if dd_match:
            break

    for pattern in dk_patterns:
        dk_match = re.search(pattern, text, re.I | re.S)

        if dk_match:
            break

    for pattern in dri_patterns:
        dri_match = re.search(pattern, text, re.I | re.S)

        if dri_match:
            break

    if not (dd_match and dk_match and dri_match):
        return None

    try:
        dd = dd_match.group(1)
        dk = int(dk_match.group(1))
        dri = ast.literal_eval(dri_match.group(1))

        if not isinstance(dri, list):
            return None

        if not all(isinstance(x, int) for x in dri):
            return None

        return dd, dk, dri

    except Exception as exc:
        log.debug(
            "Failed parsing decryption variables: %s",
            exc,
        )

        return None


def extract_stream_from_decoded(decoded: str) -> str | None:
    """
    The decrypted payload can be either:

      - a direct URL
      - JSON containing a URL
      - a quoted/escaped URL
      - text containing the URL

    Try all of these forms.
    """
    if not decoded:
        return None

    decoded = decoded.strip()

    # Direct URL.
    if decoded.startswith(("http://", "https://", "//")):
        value = clean_url(decoded)

        if is_m3u8(value):
            return value

        # Some sites return a non-.m3u8 player endpoint.
        # Accept HTTP URLs from the decrypted payload because the
        # playlist example may legitimately use a script/player endpoint.
        if value and value.startswith(("http://", "https://")):
            return value

    # JSON.
    try:
        obj = json.loads(decoded)

        def find_url(value) -> str | None:
            if isinstance(value, str):
                candidate = clean_url(value)

                if candidate and (
                    is_m3u8(candidate)
                    or candidate.startswith(("http://", "https://"))
                ):
                    return candidate

            elif isinstance(value, dict):
                # Prefer common stream keys.
                for key in (
                    "url",
                    "source",
                    "src",
                    "stream",
                    "stream_url",
                    "streamUrl",
                    "file",
                    "hls",
                    "m3u8",
                ):
                    if key in value:
                        found = find_url(value[key])

                        if found:
                            return found

                for item in value.values():
                    found = find_url(item)

                    if found:
                        return found

            elif isinstance(value, list):
                for item in value:
                    found = find_url(item)

                    if found:
                        return found

            return None

        result = find_url(obj)

        if result:
            return result

    except Exception:
        pass

    # Search the decoded text directly.
    m3u8 = extract_m3u8_from_text(decoded)

    if m3u8:
        return m3u8

    # Generic HTTP URL fallback.
    for candidate in extract_urls(decoded):
        if candidate.startswith(("http://", "https://")):
            return candidate

    return None


# ============================================================
# REQUEST HELPERS
# ============================================================

async def request_with_retry(
    url: str,
    url_num: int,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, str | int] | None = None,
    attempts: int = 3,
):
    """
    Wrapper around network.request.

    The original code made one request and immediately failed the
    complete event. A transient 403/5xx/network failure should not
    destroy the event.
    """
    if not url:
        return None

    last_url = url

    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;"
            "q=0.9,image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    if headers:
        request_headers.update(headers)

    for attempt in range(1, attempts + 1):
        try:
            response = await network.request(
                last_url,
                url_num,
                headers=request_headers,
                params=params,
                log=log,
            )

            if response:
                return response

        except Exception as exc:
            log.debug(
                "URL %s) Request attempt %s/%s failed: %s",
                url_num,
                attempt,
                attempts,
                exc,
            )

        if attempt < attempts:
            await asyncio.sleep(1.5 * attempt)

    return None


# ============================================================
# EVENT PROCESSING
# ============================================================

async def process_event(
    url: str,
    url_num: int,
) -> tuple[str | None, str | None]:

    nones = None, None

    log.info(
        'URL %s) Processing event: "%s"',
        url_num,
        url,
    )

    event_origin = get_origin(url)

    # --------------------------------------------------------
    # STEP 1: Fetch event page
    # --------------------------------------------------------

    page_headers = {
        "Referer": REFERER,
        "Origin": ORIGIN,
    }

    html_data = await request_with_retry(
        url,
        url_num,
        headers=page_headers,
        attempts=4,
    )

    if not html_data:
        log.error(
            'URL %s) Failed to fetch "%s"',
            url_num,
            url,
        )

        return nones

    page_text = getattr(html_data, "text", "") or ""

    log.debug(
        "URL %s) Event page downloaded: %s bytes",
        url_num,
        len(page_text),
    )

    # --------------------------------------------------------
    # STEP 2: Look for direct M3U8 in event page
    # --------------------------------------------------------

    direct_m3u8 = extract_m3u8_from_text(page_text)

    if direct_m3u8:
        log.info(
            "URL %s) Captured direct M3U8 from event page",
            url_num,
        )

        return direct_m3u8, url

    # --------------------------------------------------------
    # STEP 3: Parse iframe
    # --------------------------------------------------------

    soup = HTMLParser(html_data.content)

    iframe = None

    iframe_selectors = (
        'iframe[name="srcFrame"]',
        "iframe[src]",
        "iframe[data-src]",
        "iframe[data-litespeed-src]",
        "iframe",
    )

    for selector in iframe_selectors:
        iframe = soup.css_first(selector)

        if iframe:
            break

    if not iframe:
        log.warning(
            "URL %s) No iframe element found",
            url_num,
        )

        return nones

    iframe_src = (
        iframe.attributes.get("src")
        or iframe.attributes.get("data-src")
        or iframe.attributes.get("data-litespeed-src")
    )

    if not iframe_src or iframe_src.lower() == "about:blank":
        iframe_src = (
            iframe.attributes.get("data-litespeed-src")
            or iframe.attributes.get("data-src")
        )

    iframe_src = clean_url(
        iframe_src,
        getattr(html_data, "url", None) or url,
    )

    if not iframe_src:
        log.warning(
            "URL %s) Iframe has no usable source",
            url_num,
        )

        return nones

    log.info(
        'URL %s) Iframe: "%s"',
        url_num,
        iframe_src,
    )

    # --------------------------------------------------------
    # STEP 4: Fetch iframe
    # --------------------------------------------------------

    iframe_headers = {
        "Referer": url,
        "Origin": event_origin,
        "Sec-Fetch-Dest": "iframe",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
    }

    iframe_data = await request_with_retry(
        iframe_src,
        url_num,
        headers=iframe_headers,
        attempts=4,
    )

    if not iframe_data:
        log.error(
            'URL %s) Failed to fetch iframe "%s"',
            url_num,
            iframe_src,
        )

        return nones

    iframe_text = getattr(iframe_data, "text", "") or ""

    log.debug(
        "URL %s) Iframe downloaded: %s bytes",
        url_num,
        len(iframe_text),
    )

    # --------------------------------------------------------
    # STEP 5: Search iframe for direct M3U8
    # --------------------------------------------------------

    direct_m3u8 = extract_m3u8_from_text(iframe_text)

    if direct_m3u8:
        log.info(
            "URL %s) Captured M3U8 directly from iframe",
            url_num,
        )

        return direct_m3u8, iframe_src

    # --------------------------------------------------------
    # STEP 6: Extract encrypted player variables
    # --------------------------------------------------------

    variables = extract_decryption_variables(iframe_text)

    if not variables:
        log.warning(
            "URL %s) Failed to gather decoding variables",
            url_num,
        )

        # Look for common JS files that might contain the player
        # payload as a final non-browser fallback.
        try:
            iframe_soup = HTMLParser(iframe_data.content)

            script_sources = []

            for script in iframe_soup.css("script[src]"):
                src = script.attributes.get("src")

                if src:
                    script_sources.append(
                        clean_url(src, iframe_src)
                    )

            log.debug(
                "URL %s) Found %s external script(s)",
                url_num,
                len(script_sources),
            )

        except Exception:
            pass

        return nones

    dd, dk, dri = variables

    log.debug(
        "URL %s) Decoding payload: encrypted=%s chars, key=%s, shuffle=%s",
        url_num,
        len(dd),
        dk,
        len(dri),
    )

    # --------------------------------------------------------
    # STEP 7: Decrypt
    # --------------------------------------------------------

    decoded = decrypt(dd, dk, dri)

    if not decoded:
        log.warning(
            "URL %s) Decoding method failed",
            url_num,
        )

        return nones

    log.debug(
        "URL %s) Decrypted payload length: %s",
        url_num,
        len(decoded),
    )

    # --------------------------------------------------------
    # STEP 8: Extract stream URL from decrypted payload
    # --------------------------------------------------------

    m3u_src = extract_stream_from_decoded(decoded)

    if not m3u_src:
        log.warning(
            "URL %s) Decrypted payload did not contain a stream URL",
            url_num,
        )

        return nones

    log.info(
        'URL %s) Captured stream: "%s"',
        url_num,
        m3u_src,
    )

    return m3u_src, iframe_src


# ============================================================
# EVENT DISCOVERY
# ============================================================

async def get_events(
    cached_keys: KeysView[str],
) -> list[Event]:

    events: list[Event] = []

    html_data = await request_with_retry(
        BASE_URL,
        0,
        attempts=4,
    )

    if not html_data:
        log.error(
            'Failed to fetch OZOG homepage "%s"',
            BASE_URL,
        )

        return events

    soup = HTMLParser(html_data.content)

    cards = soup.css(".card-inner")

    log.info(
        "Found %s event cards",
        len(cards),
    )

    for card in cards:

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

        sport_elem, teams_elem, watch_btn_elem = values

        sport = sport_elem.text(strip=True).capitalize()

        if sport == "Sports":
            sport = "Live Event"

        team_nodes = teams_elem.css(".team-name")

        if not team_nodes:
            continue

        event_name = " vs ".join(
            team.text(strip=True)
            for team in team_nodes
        )

        if not event_name:
            continue

        key = f"[{sport}] {event_name} ({TAG})"

        if key in cached_keys:
            continue

        href = (
            watch_btn_elem.attributes.get("href")
            or watch_btn_elem.attributes.get("data-href")
            or watch_btn_elem.attributes.get("data-url")
        )

        if not href:
            continue

        event_url = clean_url(
            href,
            getattr(html_data, "url", None) or BASE_URL,
        )

        if not event_url:
            continue

        events.append(
            Event(
                sport=sport,
                name=event_name,
                link=event_url,
            )
        )

    return events


# ============================================================
# SCRAPER
# ============================================================

async def scrape() -> None:

    cached_urls = CACHE_FILE.load() or {}

    valid_urls = {
        key: value
        for key, value in cached_urls.items()
        if isinstance(value, dict)
        and value.get("source")
    }

    cached_count = len(valid_urls)

    urls.clear()
    urls.update(valid_urls)

    log.info(
        "Loaded %s event(s) from cache",
        cached_count,
    )

    log.info(
        'Scraping from "%s"',
        BASE_URL,
    )

    events = await get_events(cached_urls.keys())

    if not events:
        log.info("No new events found")

        CACHE_FILE.write(cached_urls)

        return

    log.info(
        "Processing %s new URL(s)",
        len(events),
    )

    now = Time.rn()

    new_count = 0

    for i, ev in enumerate(events, start=1):

        handler = partial(
            process_event,
            url=ev.link,
            url_num=i,
        )

        try:
            source, iframe = await network.safe_process(
                handler,
                url_num=i,
                timeout_return=(None, None),
                semaphore=network.HTTP_S,
                log=log,
            )

        except Exception as exc:
            log.error(
                'URL %s) Processing exception for "%s": %s',
                i,
                ev.link,
                exc,
            )

            source, iframe = None, None

        key = f"[{ev.sport}] {ev.name} ({TAG})"

        tvg_id, logo = leagues.get_tvg_info(
            ev.sport,
            ev.name,
        )

        # ----------------------------------------------------
        # IMPORTANT:
        # Never overwrite a previously working cached source
        # with an empty result.
        # ----------------------------------------------------

        if not source:
            previous = cached_urls.get(key)

            if previous and previous.get("source"):
                log.warning(
                    'URL %s) Stream extraction failed; preserving cached source for "%s"',
                    i,
                    key,
                )

                # Keep old valid entry.
                cached_urls[key] = previous
                urls[key] = previous

                continue

            log.warning(
                'URL %s) No stream captured for "%s"',
                i,
                key,
            )

            # Keep event in cache with no source so it can be
            # retried later, but do not add it to output.
            cached_urls[key] = {
                "source": None,
                "logo": logo,
                "refer": iframe or ev.link,
                "timestamp": now.timestamp(),
                "tvg-id": tvg_id or "Live.Event.us",
                "link": ev.link,
            }

            continue

        # ----------------------------------------------------
        # The playlist should use the iframe URL as Referer
        # when available. Otherwise use the event URL.
        # ----------------------------------------------------

        referer = iframe or ev.link

        entry = {
            "source": source,
            "logo": logo,
            "refer": referer,
            "timestamp": now.timestamp(),
            "tvg-id": tvg_id or "Live.Event.us",
            "link": ev.link,
        }

        cached_urls[key] = entry
        urls[key] = entry

        new_count += 1

        log.info(
            'URL %s) Added "%s"',
            i,
            key,
        )

    log.info(
        "Collected and cached %s new event(s)",
        new_count,
    )

    CACHE_FILE.write(cached_urls)


# ============================================================
# VLC PLAYLIST
# ============================================================

def generate_vlc_m3u8() -> str:
    """
    Generate VLC-compatible M3U8.
    """

    content = "#EXTM3U\n"

    output_index = 1

    for title, data in urls.items():

        source = data.get("source", "")

        if not source:
            continue

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
            REFERER,
        )

        # Origin should correspond to the site that hosts the
        # event whenever possible.
        event_link = data.get("link", "")

        origin = (
            get_origin(str(event_link))
            if event_link
            else ORIGIN
        )

        content += (
            f'#EXTINF:-1 tvg-chno="{output_index}" '
            f'tvg-id="{tvg_id}" '
            f'tvg-name="{title}" '
            f'tvg-logo="{logo}" '
            f'group-title="Live Events",{title}\n'
        )

        content += (
            f"#EXTVLCOPT:http-referrer={referer}\n"
        )

        content += (
            f"#EXTVLCOPT:http-origin={origin}\n"
        )

        content += (
            f"#EXTVLCOPT:http-user-agent={USER_AGENT}\n"
        )

        content += f"{source}\n"

        output_index += 1

    return content


# ============================================================
# TIVIMATE PLAYLIST
# ============================================================

def generate_tivimate_m3u8() -> str:
    """
    Generate TiviMate playlist using pipe-separated headers.

    User-Agent is URL encoded.
    Referer and Origin remain plain text.
    """

    content = "#EXTM3U\n"

    encoded_user_agent = quote(
        USER_AGENT,
        safe="",
    )

    output_index = 1

    for title, data in urls.items():

        source = data.get("source", "")

        if not source:
            continue

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
            REFERER,
        )

        event_link = data.get("link", "")

        origin = (
            get_origin(str(event_link))
            if event_link
            else ORIGIN
        )

        content += (
            f'#EXTINF:-1 tvg-chno="{output_index}" '
            f'tvg-id="{tvg_id}" '
            f'tvg-name="{title}" '
            f'tvg-logo="{logo}" '
            f'group-title="Live Events",{title}\n'
        )

        content += (
            f"{source}"
            f"|referer={referer}"
            f"|origin={origin}"
            f"|user-agent={encoded_user_agent}\n"
        )

        output_index += 1

    return content


# ============================================================
# WRITE OUTPUT
# ============================================================

def write_output_files() -> None:
    """
    Write both playlist files.
    """

    output_dir = os.getenv(
        "OUTPUT_DIR",
        ".",
    )

    os.makedirs(
        output_dir,
        exist_ok=True,
    )

    vlc_path = os.path.join(
        output_dir,
        OUT_VLC,
    )

    tivimate_path = os.path.join(
        output_dir,
        OUT_TIVI,
    )

    vlc_content = generate_vlc_m3u8()
    tivimate_content = generate_tivimate_m3u8()

    with open(
        vlc_path,
        "w",
        encoding="utf-8",
        newline="\n",
    ) as file:
        file.write(vlc_content)

    log.info(
        "Generated VLC playlist: %s",
        vlc_path,
    )

    with open(
        tivimate_path,
        "w",
        encoding="utf-8",
        newline="\n",
    ) as file:
        file.write(tivimate_content)

    log.info(
        "Generated TiviMate playlist: %s",
        tivimate_path,
    )

    valid_count = sum(
        1
        for data in urls.values()
        if data.get("source")
    )

    log.info(
        "Playlist generation complete: %s valid event(s)",
        valid_count,
    )


# ============================================================
# MAIN
# ============================================================

async def main() -> None:

    log.info(
        "Starting OZOG updater"
    )

    await scrape()

    valid_events = [
        data
        for data in urls.values()
        if data.get("source")
    ]

    if valid_events:
        write_output_files()

        log.info(
            "Successfully processed %s events",
            len(valid_events),
        )

    else:
        # Still create the two files so the workflow has deterministic
        # output even when the upstream site temporarily fails.
        write_output_files()

        log.warning(
            "No events found to write to output files"
        )


if __name__ == "__main__":
    asyncio.run(main())
```
