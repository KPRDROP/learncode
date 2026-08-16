from collections.abc import KeysView
from functools import partial
from typing import Any
from urllib.parse import urljoin, quote
from pathlib import Path
import os

from playwright.async_api import Browser, async_playwright

from utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

# ================= CONFIG =================

TAG = "PLAY"

CACHE_FILE = Cache(TAG, exp=5_400)
API_FILE = Cache(f"{TAG}-api", exp=28_800)

BASE_URL = "https://playfa.st"

REFERER = "https://exposestrat.com/"
ORIGIN = "https://exposestrat.com"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

UA_ENC = quote(USER_AGENT, safe="")

OUT_VLC = Path("play_vlc.m3u8")
OUT_TIVI = Path("play_tivimate.m3u8")


async def get_events(cached_keys: KeysView[str]) -> list[Event]:
    now = Time.clean(Time.now())

    if not (api_data := API_FILE.load(per_entry=False)):
        log.info("Refreshing API cache")

        api_data = {"timestamp": now.timestamp()}

        if r := await network.request(
            urljoin(BASE_URL, "ch.php"),
            params={"id": 1, "schedule": 1},
            log=log,
        ):
            api_data: dict[str, list[dict[str, Any]]] = r.json()

            api_data["timestamp"] = now.timestamp()

        API_FILE.write(api_data)

    events: list[Event] = []

    lang_map = {
        "GB": "EN",
        "US": "EN",
    }

    # Expanded time window to catch more events
    start_dt = now.delta(hours=-6)
    end_dt = now.delta(hours=6)

    for info in api_data.get("matches", []):
        event_name, sport = info["matchstr"], info["league"]

        event_dt = Time.from_ts(int(f'{info["startTimestamp"]}'[:-3]))

        if not start_dt <= event_dt <= end_dt:
            continue

        if not (event_channels := info.get("channels")):
            continue

        event_urls: dict[int, str] = {
            channel["number"]: lang_map.get(channel["language"], channel["language"])
            for channel in event_channels
        }

        for event_num, lang in event_urls.items():
            event_key = f"[{sport}] {event_name} | {lang} ({TAG})"
            if event_key not in cached_keys:
                events.append(
                    Event(
                        sport=sport,
                        name=f"{event_name} | {lang}",
                        link=f"https://s1.playfa.st/ch.php?id={event_num}",
                        timestamp=now.timestamp(),
                    )
                )

    return events


async def scrape(browser: Browser) -> None:
    cached_urls = CACHE_FILE.load()

    valid_urls = {k: v for k, v in cached_urls.items() if v.get("source")}

    valid_count = cached_count = len(valid_urls)

    urls.update(valid_urls)

    log.info(f"Loaded {cached_count} event(s) from cache")

    log.info(f'Scraping from "{BASE_URL}"')

    if events := await get_events(cached_urls.keys()):
        log.info(f"Processing {len(events)} new URL(s)")

        async with network.event_context(browser) as context:
            for i, ev in enumerate(events, start=1):
                async with network.event_page(context) as page:
                    handler = partial(
                        network.process_event,
                        url=ev.link,
                        url_num=i,
                        page=page,
                        log=log,
                    )

                    source = await network.safe_process(
                        handler,
                        url_num=i,
                        semaphore=network.PW_S,
                        log=log,
                    )

                    tvg_id, logo = leagues.get_tvg_info(ev.sport, ev.name)

                    key = f"[{ev.sport}] {ev.name} ({TAG})"

                    entry = {
                        "source": source,
                        "logo": logo,
                        "refer": REFERER,
                        "timestamp": ev.timestamp,
                        "tvg-id": tvg_id or "Live.Event.us",
                        "link": ev.link,
                    }

                    cached_urls[key] = entry

                    if source:
                        valid_count += 1

                        urls[key] = entry

        log.info(f"Collected and cached {valid_count - cached_count} new event(s)")

    else:
        log.info("No new events found")

    CACHE_FILE.write(cached_urls)


def write_outputs():
    """Generate M3U8 playlists for VLC and TiviMate."""
    if not urls:
        log.warning("No URLs to write")
        return
    
    # VLC format
    with open(OUT_VLC, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for i, (name, e) in enumerate(urls.items(), 1):
            f.write(
                f'#EXTINF:-1 tvg-chno="{i}" tvg-id="{e["tvg-id"]}" '
                f'tvg-name="{name}" tvg-logo="{e["logo"]}" '
                f'group-title="Live Events",{name}\n'
            )
            f.write(f"#EXTVLCOPT:http-referrer={REFERER}\n")
            f.write(f"#EXTVLCOPT:http-origin={ORIGIN}\n")
            f.write(f"#EXTVLCOPT:http-user-agent={USER_AGENT}\n")
            f.write(f"{e['source']}\n\n")
    
    # TiviMate format
    with open(OUT_TIVI, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for i, (name, e) in enumerate(urls.items(), 1):
            f.write(
                f'#EXTINF:-1 tvg-chno="{i}" tvg-id="{e["tvg-id"]}" '
                f'tvg-name="{name}" tvg-logo="{e["logo"]}" '
                f'group-title="Live Events",{name}\n'
            )
            f.write(
                f"{e['source']}|referer={REFERER}|origin={ORIGIN}|user-agent={UA_ENC}\n\n"
            )
    
    log.info(f"M3U playlists generated: {OUT_VLC}, {OUT_TIVI}")


async def main():
    """Main entry point."""
    log.info("Starting PLAY updater...")
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        
        await scrape(browser)
        
        await browser.close()
    
    write_outputs()


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
