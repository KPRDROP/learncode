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

BASE_URL = "ttps://ovostream.net/"
#"https://gozo.st/"

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
    code = ord(c)
    base = 90 if c <= "Z" else 122
    return chr(code + 13 if base >= (code + 13) else code - 13)


# ---------------------------------------------------------------------------
# OZOG decrypt
# ---------------------------------------------------------------------------

def decrypt(enc: str, xor_key: int, num_list: list[int]) -> str | None:
    hex_enc_list = []
    rot13_list = []

    try:
        chars = list(enc)

        unshuffled = [""] * len(chars)

        for i, num in enumerate(num_list):
            unshuffled[num] = chars[i]

        xor_enc = "".join(unshuffled)

        hex_enc_list.extend(
            chr(int(xor_enc[i : i + 2], 16) ^ xor_key)
            for i in range(0, len(xor_enc), 2)
        )

        hex_enc = "".join(hex_enc_list)

        rot13_list.extend(
            chr(int(hex_enc[i : i + 2], 16)) for i in range(0, len(hex_enc), 2)
        )

        rot13_str = "".join(rot13_list)

        reversed_str = "".join(
            [rot13(c) if "a" <= c <= "z" or "A" <= c <= "Z" else c for c in rot13_str]
        )[::-1]

        return base64.b64decode(reversed_str.encode("utf-8")).decode("utf-8")
    except Exception:
        return


# ---------------------------------------------------------------------------
# Process event
# ---------------------------------------------------------------------------

async def process_event(url: str, url_num: int) -> tuple[str | None, str | None]:
    nones = None, None

    if not (html_data := await network.request(url, url_num, log=log)):
        return nones

    soup = HTMLParser(html_data.content)

    iframe = soup.css_first("iframe")

    if not iframe or not (iframe_src := iframe.attributes.get("src")):
        log.warning(f"URL {url_num}) No iframe element found.")
        return nones

    if not (
        iframe_src_data := await network.request(
            iframe_src,
            url_num,
            headers={"Referer": url},
            log=log,
        )
    ):
        return nones

    # Regex patterns
    dd_ptrn = re.compile(r'_dd\s?=\s?"(.*)"(;|,)', re.I)
    dk_ptrn = re.compile(r"_dk\s?=\s?(\d*)(;|,)", re.I)
    dri_ptrn = re.compile(r"_dri\s?=\s?(\[.*\]);fu", re.I)

    if not (
        (dd_mtch := dd_ptrn.search(iframe_src_data.text))
        and (dk_mtch := dk_ptrn.search(iframe_src_data.text))
        and (dri_mtch := dri_ptrn.search(iframe_src_data.text))
    ):
        log.warning(f"URL {url_num}) Failed to gather decoding variables")
        return nones

    dd, dk = dd_mtch[1], int(dk_mtch[1])
    dri: list[int] = ast.literal_eval(dri_mtch[1])

    if not (m3u_src := decrypt(dd, dk, dri)):
        log.warning(f"URL {url_num}) Decoding method failed")
        return nones

    log.info(f"URL {url_num}) Captured M3U8")

    # Return the stream URL with the event URL as referer
    return m3u_src, url


# ---------------------------------------------------------------------------
# Event discovery
# ---------------------------------------------------------------------------

async def get_events(cached_keys: KeysView[str]) -> list[Event]:
    events: list[Event] = []

    if not (html_data := await network.request(BASE_URL, log=log)):
        return events

    soup = HTMLParser(html_data.content)

    for card in soup.css(".card-inner"):

        if not all(
            values := [
                card.css_first(x)
                for x in (
                    ".sport-tag",
                    ".teams",
                    "a.watch-btn",
                )
            ]
        ):
            continue

        sport_elem, teams_elem, watch_btn_elem = values

        sport = sport_elem.text(strip=True).capitalize()

        sport = "Live Event" if sport == "Sports" else sport

        if not (teams := teams_elem.css(".team-name")):
            continue

        event_name = " vs ".join(team.text(strip=True) for team in teams)

        if f"[{sport}] {event_name} ({TAG})" in cached_keys:
            continue

        elif not (href := watch_btn_elem.attributes.get("href")):
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

    if events := await get_events(cached_urls.keys()):
        log.info(f"Processing {len(events)} new URL(s)")

        now = Time.rn()

        for i, ev in enumerate(events, start=1):
            handler = partial(
                process_event,
                url=ev.link,
                url_num=i,
            )

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

    else:
        log.info("No new events found")

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
