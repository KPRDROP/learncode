from collections import defaultdict
from collections.abc import KeysView
from typing import Any
from urllib.parse import urljoin, quote
import asyncio

from utils import Cache, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "BITESTRM"

REFERER = "https://sportsbite.org/"
ORIGIN = "https://sportsbite.org"

USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 9; G011A Build/PI) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/68.0.3440.70 Mobile Safari/537.36"
)

CACHE_FILE = Cache(TAG, exp=10_800)

API_FILE = Cache(f"{TAG}-api", exp=28_800)

BASE_URL = "https://sportsbite.org"

VLC_FILE = "bitestrm_vlc.m3u8"
TIVIMATE_FILE = "bitestrm_tivimate.m3u8"


def process_event(url: str, url_num: int) -> str:
    s = url.replace("/embed/", "/cdn/")

    log.info(f"URL {url_num}) Captured M3U8")

    return f"{s}.m3u8?cdn=1"


async def refresh_api_cache(now: Time) -> list[dict[str, Any]]:
    api_data = {"timestamp": now.timestamp()}

    if r := await network.request(
        urljoin(BASE_URL, "api/forestgump/matches"),
        log=log,
    ):
        api_data: dict[str, Any] = r.json()

        api_data["timestamp"] = now.timestamp()

    return api_data


async def get_events(cached_keys: KeysView[str]) -> dict[str, dict[str, str | float]]:
    now = Time.rn()

    events = {}

    if not (api_data := API_FILE.load(per_entry=False)):
        log.info("Refreshing API cache")

        api_data = await refresh_api_cache(now)

        API_FILE.write(api_data)

    start_ts = now.delta(hours=-24).timestamp()
    end_ts = now.delta(hours=24).timestamp()

    counter: dict[str, int] = defaultdict(int)

    i = 1

    for game_event in api_data.get("days", [{}]):
        for game_info in game_event.get("events", [{}]):
            if not all(
                values := [
                    game_info.get(x)
                    for x in (
                        "title",
                        "category",
                        "kickoffMs",
                        "streams",
                    )
                ]
            ):
                continue

            name, sport, event_ts, event_streams = values

            event_ts = int(f"{event_ts}"[:-3])

            if not start_ts <= event_ts <= end_ts:
                continue

            tvg_id, logo = leagues.get_tvg_info(sport, name)

            for stream_info in event_streams:
                counter[name] += 1

                if stream_info.get("source") != "selfhost":
                    continue

                elif not (url := stream_info.get("manifest_url")):
                    continue

                elif (
                    key := f"[{sport}] {name} {counter[name]} ({TAG})"
                ) in cached_keys:
                    continue

                events[key] = {
                    "source": process_event(url, i),
                    "logo": logo,
                    "refer": BASE_URL,
                    "timestamp": event_ts,
                    "tvg-id": tvg_id or "Live.Event.us",
                }

                i += 1

    return events


def write_vlc_file(events: dict[str, dict[str, str | float]]) -> None:
    """Write VLC-style M3U8 playlist with EXTVLCOPT headers."""
    try:
        with open(VLC_FILE, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")

            for chno, (key, info) in enumerate(events.items(), start=1):
                source = info.get("source", "")
                logo = info.get("logo", "")
                tvg_id = info.get("tvg-id", "Live.Event.us")

                # Derive display name from key: "[Sport] Name N (TAG)" -> "Name | Sport (TAG)"
                display_name = key
                if key.startswith("[") and "] " in key:
                    sport_part, rest = key[1:].split("] ", 1)
                    display_name = f"{rest.rsplit(' ', 1)[0]} | {sport_part} ({TAG})"

                f.write(
                    f'#EXTINF:-1 tvg-chno="{chno}" tvg-id="{tvg_id}" '
                    f'tvg-name="{display_name}" tvg-logo="{logo}" '
                    f'group-title="Live Events",{display_name}\n'
                )
                f.write(f"#EXTVLCOPT:http-referrer={REFERER}\n")
                f.write(f"#EXTVLCOPT:http-origin={ORIGIN}\n")
                f.write(f"#EXTVLCOPT:http-user-agent={USER_AGENT}\n")
                f.write(f"{source}\n")

        log.info(f"Wrote {len(events)} entries to {VLC_FILE}")
    except OSError as e:
        log.error(f"Failed to write {VLC_FILE}: {e}")


def write_tivimate_file(events: dict[str, dict[str, str | float]]) -> None:
    """Write TiviMate-style M3U8 playlist with pipe-delimited headers."""
    try:
        encoded_ua = quote(USER_AGENT, safe="")
        encoded_ref = quote(REFERER, safe="")
        encoded_origin = quote(ORIGIN, safe="")

        with open(TIVIMATE_FILE, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")

            for chno, (key, info) in enumerate(events.items(), start=1):
                source = info.get("source", "")
                logo = info.get("logo", "")
                tvg_id = info.get("tvg-id", "Live.Event.us")

                display_name = key
                if key.startswith("[") and "] " in key:
                    sport_part, rest = key[1:].split("] ", 1)
                    display_name = f"{rest.rsplit(' ', 1)[0]} | {sport_part} ({TAG})"

                f.write(
                    f'#EXTINF:-1 tvg-chno="{chno}" tvg-id="{tvg_id}" '
                    f'tvg-name="{display_name}" tvg-logo="{logo}" '
                    f'group-title="Live Events",{display_name}\n'
                )
                f.write(
                    f"{source}"
                    f"|referer={encoded_ref}"
                    f"|origin={encoded_origin}"
                    f"|user-agent={encoded_ua}\n"
                )

        log.info(f"Wrote {len(events)} entries to {TIVIMATE_FILE}")
    except OSError as e:
        log.error(f"Failed to write {TIVIMATE_FILE}: {e}")


async def scrape() -> None:
    cached_sources = CACHE_FILE.load()

    valid_count = len(
        valid_sources := {k: v for k, v in cached_sources.items() if v["source"]}
    )

    urls.update(valid_sources)

    log.info(f"Loaded {valid_count} event(s) from cache")

    log.info(f'Scraping from "{BASE_URL}"')

    urls.update(await get_events(cached_sources.keys()))

    (
        log.info(f"Collected and cached {new_count} new event(s)")
        if (new_count := len(urls) - valid_count)
        else log.info("No new events found")
    )

    CACHE_FILE.write(urls)

    # Generate the two output playlists
    write_vlc_file(urls)
    write_tivimate_file(urls)


async def main() -> None:
    try:
        await scrape()
    except Exception as e:
        log.error(f"Fatal error during scrape: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())
