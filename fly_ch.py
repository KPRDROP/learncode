import re
import adblock
from functools import partial
from urllib.parse import quote
from pathlib import Path

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "FLY_CH"

CACHE_FILE = Cache(TAG, exp=7_200)

API_FILE = Cache(f"{TAG}-api", exp=19_800)

BASE_URL = "https://flyembed.click"
API_URL = "https://flyembed.click/channels.json"

# User Agent for playlists
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)
UA_ENC = quote(USER_AGENT)

# Referer and origin
REFERER = "https://epiembeds.online/"
ORIGIN = "https://epiembeds.online"

OUTPUT_VLC = Path("fly_ch_vlc.m3u8")
OUTPUT_TIVIMATE = Path("fly_ch_tivimate.m3u8")


def clean_name(s: str) -> str:
    return re.sub(r"(\r|\n)", "", s).strip()


async def process_event(url: str, url_num: int) -> tuple[str | None, str | None]:
    """Process event URL to extract M3U8 stream"""
    nones = None, None

    if not (html_data := await network.request(url, url_num, log=log)):
        return nones

    soup = HTMLParser(html_data.content)

    iframe = soup.css_first("iframe")

    if not iframe or not (iframe_src := iframe.attributes.get("src")):
        log.warning(f"URL {url_num}) No iframe source found.")
        return nones

    elif not (
        iframe_src_data := await network.request(
            iframe_src,
            url_num,
            headers={"Referer": url},
            log=log,
        )
    ):
        return nones

    num_list_ptrn = re.compile(r"var\s+_(\w|\d)+=\[(.*)\],", re.S)

    index_ptrn = re.compile(r'\],(.*)(_.*="")')

    m3u_ptrn = re.compile(r'var\s+signed_url\s+=\s+"(.*)";', re.I)

    z_ptrn = re.compile(r"\%(\d+)")

    if not (z_mtch := z_ptrn.search(iframe_src_data.text)):
        log.warning(f"URL {url_num}) Unable to decipher m3u encryption.")
        return nones

    elif not (num_list_mtch := num_list_ptrn.search(iframe_src_data.text)):
        log.warning(f"URL {url_num}) Unable to decipher m3u encryption.")
        return nones

    elif not (index_mtch := index_ptrn.search(iframe_src_data.text)):
        log.warning(f"URL {url_num}) Unable to decipher m3u encryption.")
        return nones

    num_list = (int(i) for i in num_list_mtch[2].split(","))

    x, y = (int(i.split("=")[-1]) for i in index_mtch[1].split(",") if i)

    z = int(z_mtch[1])

    js = "".join(chr(((i ^ x) - y + z) % z) for i in num_list)

    if not (m3u_mtch := m3u_ptrn.search(js)):
        log.warning(f"URL {url_num}) No M3U8 source found.")
        return nones

    log.info(f"URL {url_num}) Captured M3U8")

    return m3u_mtch[1], iframe_src


async def get_events(cached_keys: set[str]) -> list[Event]:
    """Get events from the channels API"""
    now = Time.clean(Time.now())

    events: list[Event] = []

    if not (api_data := API_FILE.load(per_entry=False, index=-1)):
        log.info("Refreshing API cache")

        api_data = [{"timestamp": now.timestamp()}]

        if r := await network.request(API_URL, log=log):
            try:
                api_data = r.json()
                if not isinstance(api_data, list):
                    log.warning("API returned non-list data")
                    api_data = []
                else:
                    # Add timestamp for cache
                    api_data = [{"timestamp": now.timestamp()}] + api_data
            except Exception as e:
                log.error(f"Failed to parse API response: {e}")
                api_data = [{"timestamp": now.timestamp()}]

        API_FILE.write(api_data)

    # Skip the timestamp entry if present
    events_data = api_data
    if events_data and isinstance(events_data, list) and len(events_data) > 0:
        if "timestamp" in events_data[0]:
            events_data = events_data[1:]

    for channel in events_data:
        if not isinstance(channel, dict):
            continue

        name = channel.get("name")
        category = channel.get("category", "Live")
        url = channel.get("url")

        if not (name and url):
            continue

        # Clean name
        name = clean_name(name)

        # Determine sport/group
        sport = category.upper()
        if sport == "SPORTS":
            sport = "Live Sports"
        elif sport == "ENTERTAINMENT":
            sport = "Entertainment"
        elif sport == "NEWS":
            sport = "News"
        else:
            sport = category.capitalize()

        key = f"[{sport}] {name} ({TAG})"

        if key in cached_keys:
            continue

        events.append(
            Event(
                sport=sport,
                name=name,
                link=url,
                timestamp=now.timestamp(),
            )
        )

    log.info(f"Found {len(events)} events from API")
    return events


def generate_vlc_playlist(data: dict[str, dict]) -> int:
    """Generate VLC-compatible playlist"""
    lines = ["#EXTM3U"]
    lines.append(f"# Playlist generated by {TAG} - {Time.clean(Time.now()).strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")
    count = 0
    chno = 1

    for name, entry in sorted(data.items()):
        url = entry.get("source")
        if not url:
            continue

        referer = entry.get("refer", REFERER)
        tvg_id = entry.get("tvg-id", "Live.Event.us")
        tvg_logo = entry.get("logo", "https://i.gyazo.com/4a5e9fa2525808ee4b65002b56d3450e.png")
        group_title = entry.get("sport", "Live Events")
        
        lines.append(f'#EXTINF:-1 tvg-chno="{chno}" tvg-id="{tvg_id}" tvg-logo="{tvg_logo}" group-title="{group_title}",{name}')
        lines.append(f"#EXTVLCOPT:http-referrer={referer}")
        lines.append(f"#EXTVLCOPT:http-origin={referer}")
        lines.append(f"#EXTVLCOPT:http-user-agent={USER_AGENT}")
        lines.append(url)
        lines.append("")
        count += 1
        chno += 1

    with open(OUTPUT_VLC, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    log.info(f"Generated {OUTPUT_VLC} with {count} events")
    return count


def generate_tivimate_playlist(data: dict[str, dict]) -> int:
    """Generate TiviMate-compatible playlist with pipe format"""
    lines = ["#EXTM3U"]
    lines.append(f"# Playlist generated by {TAG} - {Time.clean(Time.now()).strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")
    count = 0
    chno = 1

    for name, entry in sorted(data.items()):
        url = entry.get("source")
        if not url:
            continue

        referer = entry.get("refer", REFERER)
        tvg_id = entry.get("tvg-id", "Live.Event.us")
        tvg_logo = entry.get("logo", "https://i.gyazo.com/4a5e9fa2525808ee4b65002b56d3450e.png")
        group_title = entry.get("sport", "Live Events")
        
        lines.append(f'#EXTINF:-1 tvg-chno="{chno}" tvg-id="{tvg_id}" tvg-logo="{tvg_logo}" group-title="{group_title}",{name}')
        lines.append(f"{url}|referer={referer}|origin={referer}|user-agent={UA_ENC}")
        lines.append("")
        count += 1
        chno += 1

    with open(OUTPUT_TIVIMATE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    log.info(f"Generated {OUTPUT_TIVIMATE} with {count} events")
    return count


async def scrape() -> None:
    """Main scrape function"""
    cached_urls = CACHE_FILE.load() or {}

    valid_urls = {k: v for k, v in cached_urls.items() if v.get("source")}
    valid_count = cached_count = len(valid_urls)

    urls.update(valid_urls)

    log.info(f"Loaded {cached_count} event(s) from cache")

    log.info(f'Scraping from "{API_URL}"')

    events = await get_events(set(cached_urls.keys()))
    
    if events:
        log.info(f"Processing {len(events)} new URL(s)")

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

            if not source:
                log.warning(f"Event {i}) No stream found for: {ev.name}")
                continue

            key = f"[{ev.sport}] {ev.name} ({TAG})"

            tvg_id, logo = leagues.get_tvg_info(ev.sport, ev.name)

            entry = {
                "source": source,
                "logo": logo,
                "refer": iframe or REFERER,
                "timestamp": ev.timestamp,
                "tvg-id": tvg_id or "Live.Event.us",
                "link": ev.link,
                "sport": ev.sport,
            }

            cached_urls[key] = entry
            urls[key] = entry
            valid_count += 1
            log.info(f"Event {i}) ✓ Captured: {ev.name}")

        log.info(f"Collected and cached {valid_count - cached_count} new event(s)")

    else:
        log.info("No new events found")

    # Save cache
    CACHE_FILE.write(cached_urls)
    
    # Generate playlists only with valid URLs
    valid_events = {k: v for k, v in urls.items() if v.get("source")}
    
    if valid_events:
        vlc_count = generate_vlc_playlist(valid_events)
        tivimate_count = generate_tivimate_playlist(valid_events)
        log.info(f"Final playlist size: {len(valid_events)} events")
        log.info(f"Total written: {vlc_count + tivimate_count}")
    else:
        log.warning("No valid events to generate playlists")
        with open(OUTPUT_VLC, "w") as f:
            f.write("#EXTM3U\n# No events available\n")
        with open(OUTPUT_TIVIMATE, "w") as f:
            f.write("#EXTM3U\n# No events available\n")


async def main():
    """Main entry point"""
    log.info(f"Starting {TAG} scraper")
    await scrape()
    log.info(f"{TAG} scraper completed")


def run():
    """Run the scraper"""
    import asyncio
    asyncio.run(main())


if __name__ == "__main__":
    run()
