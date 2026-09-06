import ast
import base64
import re
import os
from collections.abc import KeysView
from functools import partial
from urllib.parse import urljoin, quote

from selectolax.parser import HTMLParser

from utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "OZOG"

CACHE_FILE = Cache(TAG, exp=28_800)

BASE_URL = "https://gozo.st/"

REFERER = "https://unxer123.gozo.zip/games/juventus-vs-milan/"
ORIGIN = "https://unxer123.gozo.zip/games/juventus-vs-milan/"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"


def rot13(c: str) -> str:
    code = ord(c)
    base = 90 if c <= "Z" else 122
    return chr(code + 13 if base >= (code + 13) else code - 13)


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
    except Exception as e:
        log.error(f"Decryption error: {e}")
        return


def extract_decryption_vars(html: str) -> tuple[str | None, int | None, list[int] | None]:
    """Extract _dd, _dk, _dri variables from HTML"""
    dd_ptrn = re.compile(r'const\s+_dd\s*=\s*"([^"]+)"', re.I)
    dk_ptrn = re.compile(r'const\s+_dk\s*=\s*(\d+)', re.I)
    dri_ptrn = re.compile(r'const\s+_dri\s*=\s*\[([^\]]+)\]', re.I)
    
    dd_match = dd_ptrn.search(html)
    dk_match = dk_ptrn.search(html)
    dri_match = dri_ptrn.search(html)
    
    if not (dd_match and dk_match and dri_match):
        return None, None, None
    
    dd = dd_match[1]
    dk = int(dk_match[1])
    dri = ast.literal_eval(f"[{dri_match[1]}]")
    
    return dd, dk, dri


def extract_stream_url(html: str) -> str | None:
    """Extract and decrypt the stream URL from the HTML"""
    # Look for the direct URL format first
    direct_pattern = re.compile(r'if\s*\(\s*_M\s*===\s*[\'"]direct[\'"]\s*\)\s*\{[^}]*return\s+_decrypt\(_dd,\s*_dk,\s*_dri\)', re.S)
    if direct_pattern.search(html):
        dd, dk, dri = extract_decryption_vars(html)
        if dd and dk and dri:
            try:
                return decrypt(dd, dk, dri)
            except Exception as e:
                log.error(f"Failed to decrypt stream URL: {e}")
    
    # Look for encrypted URL pattern in the player
    dd_ptrn = re.compile(r'const\s+_dd\s*=\s*"([^"]+)"', re.I)
    dk_ptrn = re.compile(r'const\s+_dk\s*=\s*(\d+)', re.I)
    dri_ptrn = re.compile(r'const\s+_dri\s*=\s*\[([^\]]+)\]', re.I)
    
    dd_match = dd_ptrn.search(html)
    dk_match = dk_ptrn.search(html)
    dri_match = dri_ptrn.search(html)
    
    if dd_match and dk_match and dri_match:
        dd = dd_match[1]
        dk = int(dk_match[1])
        dri = ast.literal_eval(f"[{dri_match[1]}]")
        try:
            return decrypt(dd, dk, dri)
        except Exception as e:
            log.error(f"Failed to decrypt stream URL: {e}")
    
    return None


async def process_event(url: str, url_num: int) -> tuple[str | None, str | None]:
    nones = None, None

    if not (html_data := await network.request(url, log=log)):
        return nones

    html_content = html_data.text if hasattr(html_data, 'text') else html_data.content
    
    # Try to extract stream URL from the HTML
    stream_url = extract_stream_url(html_content)
    
    if stream_url:
        log.info(f"URL {url_num}) Captured M3U8 from embedded player")
        return stream_url, url

    # Fallback: look for iframe
    soup = HTMLParser(html_content)

    iframe = soup.css_first("iframe")

    if not iframe or not (iframe_src := iframe.attributes.get("src")):
        log.warning(f"URL {url_num}) No iframe or stream URL found.")
        return nones

    if not (
        iframe_src_data := await network.request(
            iframe_src,
            headers={"Referer": url},
            log=log,
        )
    ):
        return nones

    # Try to extract from iframe content
    stream_url = extract_stream_url(iframe_src_data.text if hasattr(iframe_src_data, 'text') else iframe_src_data.content)
    
    if stream_url:
        log.info(f"URL {url_num}) Captured M3U8 from iframe")
        return stream_url, iframe_src

    # Try old method (for backward compatibility)
    dd_ptrn = re.compile(r'_dd\s?=\s?"(.*)";', re.I)
    dk_ptrn = re.compile(r"_dk\s?=\s?(\d*);", re.I)
    dri_ptrn = re.compile(r"_dri\s?=\s?(.*);", re.I)

    if not (
        (dd_mtch := dd_ptrn.search(iframe_src_data.text if hasattr(iframe_src_data, 'text') else iframe_src_data.content))
        and (dk_mtch := dk_ptrn.search(iframe_src_data.text if hasattr(iframe_src_data, 'text') else iframe_src_data.content))
        and (dri_mtch := dri_ptrn.search(iframe_src_data.text if hasattr(iframe_src_data, 'text') else iframe_src_data.content))
    ):
        log.warning(f"URL {url_num}) Failed to gather decoding variables")
        return nones

    dd, dk = dd_mtch[1], int(dk_mtch[1])
    dri: list[int] = ast.literal_eval(dri_mtch[1])

    if not (m3u_src := decrypt(dd, dk, dri)):
        log.warning(f"URL {url_num}) Decoding method failed")
        return nones

    log.info(f"URL {url_num}) Captured M3U8")

    return m3u_src, iframe_src


async def get_events(cached_keys: KeysView[str]) -> list[Event]:
    events: list[Event] = []

    if not (html_data := await network.request(BASE_URL, log=log)):
        return events

    soup = HTMLParser(html_data.text if hasattr(html_data, 'text') else html_data.content)

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


async def scrape() -> None:
    cached_urls = CACHE_FILE.load()

    valid_urls = {k: v for k, v in cached_urls.items() if v.get("source")}

    valid_count = cached_count = len(valid_urls)

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

            source, iframe = await network.safe_process(
                handler,
                url_num=i,
                timeout_return=(None, None),
                semaphore=network.HTTP_S,
                log=log,
            )

            key = f"[{ev.sport}] {ev.name} ({TAG})"

            tvg_id, logo = leagues.get_tvg_info(ev.sport, ev.name)
            
            # Use the event's URL as referer if available
            referer = iframe or ev.link or REFERER

            entry = {
                "source": source,
                "logo": logo,
                "refer": referer,
                "timestamp": now.timestamp(),
                "tvg-id": tvg_id or "Live.Event.us",
                "link": ev.link,
            }

            cached_urls[key] = entry

            if source:
                valid_count += 1
                urls[key] = entry
                log.info(f"Added event: {key}")

        log.info(f"Collected and cached {valid_count - cached_count} new event(s)")

    else:
        log.info("No new events found")

    CACHE_FILE.write(cached_urls)


def generate_vlc_m3u8() -> str:
    """Generate VLC format M3U8 file content"""
    content = "#EXTM3U\n"
    
    for idx, (title, data) in enumerate(urls.items(), 1):
        tvg_id = data.get("tvg-id", "Live.Event.us")
        logo = data.get("logo", "")
        source = data.get("source", "")
        referer = data.get("refer", REFERER)
        
        if not source:
            continue
            
        content += f'#EXTINF:-1 tvg-chno="{idx}" tvg-id="{tvg_id}" tvg-name="{title}" tvg-logo="{logo}" group-title="Live Events",{title}\n'
        content += f'#EXTVLCOPT:http-referrer={referer}\n'
        content += f'#EXTVLCOPT:http-origin={referer}\n'
        content += f'#EXTVLCOPT:http-user-agent={USER_AGENT}\n'
        content += f'{source}\n'
    
    return content


def generate_tivimate_m3u8() -> str:
    """Generate TiviMate format M3U8 file content with pipe-separated headers"""
    content = "#EXTM3U\n"
    
    encoded_user_agent = quote(USER_AGENT)
    
    for idx, (title, data) in enumerate(urls.items(), 1):
        tvg_id = data.get("tvg-id", "Live.Event.us")
        logo = data.get("logo", "")
        source = data.get("source", "")
        referer = data.get("refer", REFERER)
        
        if not source:
            continue
            
        content += f'#EXTINF:-1 tvg-chno="{idx}" tvg-id="{tvg_id}" tvg-name="{title}" tvg-logo="{logo}" group-title="Live Events",{title}\n'
        content += f'{source}|referer={referer}|origin={referer}|user-agent={encoded_user_agent}\n'
    
    return content


def write_output_files() -> None:
    """Generate and write both output M3U8 files"""
    output_dir = os.getenv("OUTPUT_DIR", ".")
    os.makedirs(output_dir, exist_ok=True)
    
    # Generate VLC format
    vlc_content = generate_vlc_m3u8()
    vlc_file = os.path.join(output_dir, "ozog_vlc.m3u8")
    with open(vlc_file, "w", encoding="utf-8") as f:
        f.write(vlc_content)
    log.info(f"Generated VLC playlist: {vlc_file}")
    
    # Generate TiviMate format
    tivimate_content = generate_tivimate_m3u8()
    tivimate_file = os.path.join(output_dir, "ozog_tivimate.m3u8")
    with open(tivimate_file, "w", encoding="utf-8") as f:
        f.write(tivimate_content)
    log.info(f"Generated TiviMate playlist: {tivimate_file}")


async def main() -> None:
    """Main entry point for the script"""
    log.info("Starting OZOG scraper")
    
    # First, scrape or load cached events
    await scrape()
    
    # Then write the output files
    if urls:
        write_output_files()
        log.info(f"Successfully processed {len(urls)} events")
    else:
        log.warning("No events found to write to output files")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
