import asyncio
import re
import adblock
from typing import Any
from urllib.parse import urljoin, quote

from utils import Cache, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "XYZ"

CACHE_FILE = Cache(TAG, exp=28_800)

API_FILE = Cache(f"{TAG}-api", exp=28_800)

BASE_URL = "https://xyzstreams.st/"

SPORTS = [
    "MLB",
    "WNBA",
    "NBA",
    "NHL",
    "NFL": "nflembed",
]

SPORT_URLS = {sport: urljoin(BASE_URL, sport.lower()) for sport in SPORTS}

API_URLS = [
    urljoin("https://site.api.espn.com/apis/site/v2/sports/", f"{sport}/scoreboard")
    for sport in [
        "baseball/mlb",
        "basketball/nba",
        "basketball/wnba",
        "football/nfl",
        "hockey/nhl",
    ]
]


async def refresh_api_cache(now: Time) -> list[dict[str, Any]]:
    tasks = [
        network.request(
            url,
            params={"dates": f"{now:%Y%m%d}"},
            headers={"User-Agent": "curl/8.20.0"},
            log=log,
        )
        for url in API_URLS
    ]

    results = await asyncio.gather(*tasks)

    api_data = []

    for resp in (r for r in results if r):
        data = resp.json()

        league = data["leagues"][0]["abbreviation"].upper()

        for event in data.get("events", []):
            event["league"] = league

            api_data.append(event)

    if not api_data:
        return [{"timestamp": now.timestamp()}]

    api_data[-1]["timestamp"] = now.timestamp()

    return api_data


async def get_sports_map() -> dict[str, dict[str, dict[str, str]]]:
    sports_map = {}

    tasks = [network.request(url, log=log) for url in SPORT_URLS.values()]

    results = await asyncio.gather(*tasks)

    if not (texts := [(html.text, html.url) for html in results if html]):
        return sports_map

    replaces = {
        "MLB": {
            "CWS": "CHW",
            "OAK": "ATH",
            "AZ": "ARI",
            "WAS": "WSH",
        },
        "WNBA": {
            "GSV": "GS",
            "LVA": "LV",
            "LAS": "LA",
            "NYL": "NY",
            "PHO": "PHX",
            "PDX": "POR",
            "WAS": "WSH",
        },
    }

    ptrn = re.compile(r"M3U8_CHANNELS_MAP\s*=\s*\{(.*?)\};", re.S)

    for text, url in texts:
        sport = next((k for k, v in SPORT_URLS.items() if v == url), "Live Event")

        if not (match := ptrn.search(text)):
            sports_map[sport] = {}

        else:
            pairs: list[tuple[str, str]] = re.findall(
                r"'([^']+)'\s*:\s*'([^']+)'",
                match[1],
            )

            sports_map[sport] = dict(pairs)

    for sport, abbrs in replaces.items():
        for old, new in abbrs.items():
            if old in sports_map.get(sport, {}):
                sports_map[sport][new] = sports_map[sport].pop(old)

    return sports_map


async def get_events() -> dict[str, dict[str, str | float]]:
    now = Time.clean(Time.now())

    events = {}

    if not (api_data := API_FILE.load(per_entry=False, index=-1)):
        log.info("Refreshing API cache")

        api_data = await refresh_api_cache(now)

        API_FILE.write(api_data)

    if not (sports_map := await get_sports_map()):
        return events

    for game_info in api_data:
        if not all(
            values := [
                game_info.get(x)
                for x in (
                    "league",
                    "name",
                    "shortName",
                )
            ]
        ):
            continue

        sport, name, short_name = values

        for abbr in (i.strip() for i in short_name.split("@")):
            key = f"[{sport}] {name} | {abbr} Feed ({TAG})"

            tvg_id, logo = leagues.get_tvg_info(sport, name)

            events[key] = {
                "source": sports_map.get(sport, {}).get(abbr),
                "logo": logo,
                "refer": BASE_URL,
                "timestamp": now.timestamp(),
                "tvg-id": tvg_id or "Live.Event.us",
                "sport": sport,
                "name": name,
                "abbr": abbr,
            }

    return events


def generate_m3u8_files(events_data: dict[str, dict[str, str | float]]) -> None:
    """Generate VLC and TiviMate M3U8 files from events data."""
    
    vlc_filename = f"{TAG.lower()}_vlc.m3u8"
    tivimate_filename = f"{TAG.lower()}_tivimate.m3u8"
    
    vlc_content = ['#EXTM3U']
    tivimate_content = ['#EXTM3U']
    
    channel_counter = 1
    
    for event_name, event_info in events_data.items():
        source_url = event_info.get("source")
        
        # Skip if no source URL
        if not source_url:
            continue
            
        sport = event_info.get("sport", "Live Events")
        tvg_id = event_info.get("tvg-id", "Live.Event.us")
        logo = event_info.get("logo", "")
        name = event_info.get("name", "")
        abbr = event_info.get("abbr", "")
        
        # VLC format (original)
        vlc_entry = f'#EXTINF:-1 tvg-chno="{channel_counter}" tvg-id="{tvg_id}" tvg-name="{event_name}" tvg-logo="{logo}" group-title="{sport}",{event_name}'
        vlc_content.append(vlc_entry)
        vlc_content.append(f'#EXTVLCOPT:http-referrer={BASE_URL}')
        vlc_content.append(f'#EXTVLCOPT:http-origin={BASE_URL}')
        vlc_content.append('#EXTVLCOPT:http-user-agent=Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36')
        vlc_content.append(source_url)
        
        # TiviMate format (pipe-separated with encoded user agent)
        user_agent = "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
        encoded_user_agent = quote(user_agent, safe='')
        
        tivimate_name = f"[{sport}] {name} - {abbr} ({TAG})"
        tivimate_entry = f'#EXTINF:-1 tvg-chno="{channel_counter}" tvg-id="{tvg_id}" tvg-name="{tivimate_name}" tvg-logo="{logo}" group-title="{sport}",{tivimate_name}'
        tivimate_content.append(tivimate_entry)
        tivimate_content.append(f'{source_url}|referer={BASE_URL}|origin={BASE_URL}|user-agent={encoded_user_agent}')
        
        channel_counter += 1
    
    # Write VLC file
    try:
        with open(vlc_filename, 'w', encoding='utf-8') as f:
            f.write('\n'.join(vlc_content))
        log.info(f"Generated VLC M3U8 file: {vlc_filename}")
    except Exception as e:
        log.error(f"Error writing VLC M3U8 file: {e}")
    
    # Write TiviMate file
    try:
        with open(tivimate_filename, 'w', encoding='utf-8') as f:
            f.write('\n'.join(tivimate_content))
        log.info(f"Generated TiviMate M3U8 file: {tivimate_filename}")
    except Exception as e:
        log.error(f"Error writing TiviMate M3U8 file: {e}")


async def scrape() -> None:
    if cached_urls := CACHE_FILE.load():
        urls.update({k: v for k, v in cached_urls.items() if v["source"]})

        log.info(f"Loaded {len(urls)} event(s) from cache")

        # Generate M3U8 files from cached data
        generate_m3u8_files(urls)
        
        return

    log.info(f'Scraping from "{BASE_URL}"')

    urls.update(await get_events())

    (
        log.info(f"Collected and cached {new_urls} event(s)")
        if (new_urls := len(urls))
        else log.info("No events found")
    )

    CACHE_FILE.write(urls)
    
    # Generate M3U8 files
    generate_m3u8_files(urls)


async def main() -> None:
    """Main entry point for the script."""
    try:
        log.info("Starting XYZ updater...")
        await scrape()
        log.info("Updater completed successfully")
    except Exception as e:
        log.error(f"Updater failed: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())
