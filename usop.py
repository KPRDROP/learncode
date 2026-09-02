from urllib.parse import parse_qsl, urlsplit
from urllib.parse import quote
import os

from utils import Cache, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "USOP"

CACHE_FILE = Cache(TAG, exp=19_800)

API_URL = os.getenv("USOP_API_URL")


async def get_events() -> dict[str, dict[str, str | float]]:
    now = Time.rn()

    events = {}

    if not (api_req := await network.request(API_URL, log=log)):
        return events

    api_data = api_req.json()

    sport = "US Open"

    for game in api_data.get("itemListElement", []):
        title, stream_url = game.get("name"), game.get("contentUrl")

        if not (title and stream_url):
            continue

        splits = urlsplit(stream_url)

        params = dict(parse_qsl(splits.query))

        if not (stream_id := params.get("streamid")):
            continue

        key = f"[{sport}] {title} ({TAG})"

        tvg_id, logo = leagues.get_tvg_info(sport, title)

        events[key] = {
            "source": f"https://xyzstreams.blog/2/stream/espn/{stream_id}/stream_0.m3u8",
            "logo": game.get("thumbnailUrl") or logo,
            "refer": "https://xyzstreams.st/",
            "timestamp": now.timestamp(),
            "tvg-id": tvg_id or "Live.Event.us",
        }

    return events


async def scrape() -> None:
    if cached := CACHE_FILE.load():
        urls.update(cached)

        log.info(f"Loaded {len(urls)} event(s) from cache")

        return

    log.info(f'Scraping from "{API_URL}"')

    urls.update(await get_events())

    log.info(f"Collected and cached {len(urls)} new event(s)")

    CACHE_FILE.write(urls)


def generate_vlc_m3u8() -> str:
    """Generate VLC format M3U8 file content"""
    content = "#EXTM3U\n"
    
    for idx, (title, data) in enumerate(urls.items(), 1):
        tvg_id = data.get("tvg-id", "Live.Event.us")
        logo = data.get("logo", "")
        referer = data.get("refer", "https://xyzstreams.st/")
        source = data.get("source", "")
        
        content += f'#EXTINF:-1 tvg-chno="{idx}" tvg-id="{tvg_id}" tvg-name="{title}" tvg-logo="{logo}" group-title="Live Events",{title}\n'
        content += f'#EXTVLCOPT:http-referrer={referer}\n'
        content += f'#EXTVLCOPT:http-origin={referer}\n'
        content += '#EXTVLCOPT:http-user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36\n'
        content += f'{source}\n'
    
    return content


def generate_tivimate_m3u8() -> str:
    """Generate TiviMate format M3U8 file content with pipe-separated headers"""
    content = "#EXTM3U\n"
    
    user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
    encoded_user_agent = quote(user_agent)
    
    for idx, (title, data) in enumerate(urls.items(), 1):
        tvg_id = data.get("tvg-id", "Live.Event.us")
        logo = data.get("logo", "")
        referer = data.get("refer", "https://xyzstreams.st/")
        source = data.get("source", "")
        
        content += f'#EXTINF:-1 tvg-chno="{idx}" tvg-id="{tvg_id}" tvg-name="{title}" tvg-logo="{logo}" group-title="Live Events",{title}\n'
        content += f'{source}|referer={referer}|referer={referer}|user-agent={encoded_user_agent}\n'
    
    return content


def write_output_files() -> None:
    """Generate and write both output M3U8 files"""
    output_dir = os.getenv("OUTPUT_DIR", ".")
    os.makedirs(output_dir, exist_ok=True)
    
    # Generate VLC format
    vlc_content = generate_vlc_m3u8()
    vlc_file = os.path.join(output_dir, "usop_vlc.m3u8")
    with open(vlc_file, "w", encoding="utf-8") as f:
        f.write(vlc_content)
    log.info(f"Generated VLC playlist: {vlc_file}")
    
    # Generate TiviMate format
    tivimate_content = generate_tivimate_m3u8()
    tivimate_file = os.path.join(output_dir, "usop_tivimate.m3u8")
    with open(tivimate_file, "w", encoding="utf-8") as f:
        f.write(tivimate_content)
    log.info(f"Generated TiviMate playlist: {tivimate_file}")


async def main() -> None:
    """Main entry point for the script"""
    log.info("Starting US Open scraper")
    
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
