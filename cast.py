import ast
import asyncio
import re
from functools import partial
from pathlib import Path
from urllib.parse import urljoin, quote

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "CAST"

CACHE_FILE = Cache(TAG, exp=12_600)

# Output files
OUT_VLC = Path("cast_vlc.m3u8")
OUT_TIVI = Path("cast_tivimate.m3u8")

# User agent for outputs
UA_RAW = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
UA_ENC = quote(UA_RAW)

BASE_URLS = {
    "MLB": {"base": "https://mlbwebcast.com", "api": "stream/check_stream.php"},
    "NFL": {"base": "https://nflwebcast.com", "api": "live/check_stream.php"},
}


def fix_event(s: str) -> str:
    return " vs ".join(s.split("@"))


def clean_channel_name(name: str) -> str:
    """Clean channel/event name by removing emojis and cleaning special chars."""
    # Remove emojis
    emoji_pattern = re.compile(
        "["
        "\U0001F600-\U0001F64F"  # emoticons
        "\U0001F300-\U0001F5FF"  # symbols & pictographs
        "\U0001F680-\U0001F6FF"  # transport & map symbols
        "\U0001F1E0-\U0001F1FF"  # flags (iOS)
        "\U00002702-\U000027B0"
        "\U000024C2-\U0001F251"
        "\U0001F900-\U0001F9FF"  # supplemental symbols
        "\U0001FA70-\U0001FAFF"  # additional symbols
        "\U00002300-\U000023FF"  # misc symbols
        "\U00002600-\U000027FF"  # misc symbols
        "\U00002900-\U000029FF"  # arrows
        "\U00002B00-\U00002BFF"  # arrows
        "]+",
        flags=re.UNICODE,
    )
    name = emoji_pattern.sub("", name).strip()
    
    # Remove any remaining non-ASCII characters
    name = re.sub(r'[^\x00-\x7F]+', '', name).strip()
    
    # Clean up multiple spaces
    name = re.sub(r'\s+', ' ', name).strip()
    
    return name


async def process_event(
    url: str,
    url_num: int,
    sport: str,
) -> str | None:

    if not (event_data := await network.request(url, url_num, log=log)):
        return

    soup = HTMLParser(event_data.content)

    if not (iframe := soup.css_first('iframe[name="srcFrame"]')):
        log.warning(f"URL {url_num}) No iframe element found.")
        return

    if not (iframe_src := iframe.attributes.get("src")):
        log.warning(f"URL {url_num}) No iframe source found.")
        return

    elif iframe_src.lower() == "about:blank":
        iframe_src = iframe.attributes.get("data-litespeed-src")

    if not (
        iframe_src_data := await network.request(
            iframe_src,
            url_num,
            headers={"Referer": url},
            log=log,
        )
    ):
        return

    pattern = re.compile(r'var\s+\w*=\[([^"]*)\];', re.I)

    if not (match := pattern.search(iframe_src_data.text)):
        log.warning(f"URL {url_num}) No Clappr source found.")
        return

    try:
        ev_id, ev_ts, ev_pt = ast.literal_eval(match[1])
    except ValueError:
        log.warning(f"URL {url_num}) Failed to parse event info.")
        return

    params: dict[str, int | str] = dict(zip(["id", "ts", "pt"], [ev_id, ev_ts, ev_pt]))

    if not (
        api_data := await network.request(
            urljoin(BASE_URLS[sport]["base"], BASE_URLS[sport]["api"]),
            url_num,
            headers={"Referer": iframe_src},
            params=params,
            log=log,
        )
    ):
        return

    elif (data := api_data.json()).get("error"):
        log.warning(f"URL {url_num}) Failed to make php request.")
        return

    elif not (m3u8 := data.get("url")):
        log.warning(f"URL {url_num}) No M3U8 found.")

    log.info(f"URL {url_num}) Captured M3U8")

    return m3u8


async def get_events() -> list[Event]:
    tasks = [
        network.request(url, log=log) for url in (i["base"] for i in BASE_URLS.values())
    ]

    results = await asyncio.gather(*tasks)

    events: list[Event] = []

    if not (
        soups := [(HTMLParser(html.content), html.url) for html in results if html]
    ):
        return events

    for soup, url in soups:
        sport = next(
            (k for k, v in BASE_URLS.items() if v["base"] == url),
            "Live Event",
        )

        for row in soup.css("tr.singele_match_date"):
            if not (vs_node := row.css_first("td.teamvs a")):
                continue

            event_name = vs_node.text(strip=True)

            for span in vs_node.css("span.mtdate"):
                date = span.text(strip=True)

                event_name = event_name.replace(date, "").strip()

            if not (href := vs_node.attributes.get("href")):
                continue

            events.append(
                Event(
                    sport=sport,
                    name=fix_event(event_name),
                    link=href,
                )
            )

    return events


def write_outputs():
    """Write the VLC and TiviMate playlist files."""
    if not urls:
        log.warning("No URLs to write to output files")
        return
    
    vlc_lines = ["#EXTM3U"]
    tivi_lines = ["#EXTM3U"]
    channel_counter = 1
    
    for key, data in urls.items():
        if not data.get("source"):
            continue
        
        # Extract sport from key
        sport_match = re.search(r'\[([^\]]+)\]', key)
        sport = sport_match.group(1) if sport_match else "Live Event"
        
        # Clean channel name
        clean_name = clean_channel_name(key)
        
        # Get logo and tvg-id
        logo = data.get("logo", "")
        tvg_id = data.get("tvg-id", "Live.Event.us")
        referer = data.get("refer", "")
        
        # Build EXTINF line
        extinf = (
            f'#EXTINF:-1 tvg-chno="{channel_counter}" '
            f'tvg-id="{tvg_id}" '
            f'tvg-name="{clean_name}" '
            f'tvg-logo="{logo}" '
            f'group-title="{sport}",{clean_name}'
        )
        
        # VLC format
        vlc_lines.append(extinf)
        vlc_lines.append(f"#EXTVLCOPT:http-referrer={referer}")
        vlc_lines.append(f"#EXTVLCOPT:http-origin={referer}")
        vlc_lines.append(f"#EXTVLCOPT:http-user-agent={UA_RAW}")
        vlc_lines.append(data["source"])
        
        # TiviMate format
        tivi_lines.append(extinf)
        url_with_params = (
            f"{data['source']}"
            f"|referer={referer}/"
            f"|origin={referer}/"
            f"|user-agent={UA_ENC}"
        )
        tivi_lines.append(url_with_params)
        
        channel_counter += 1
    
    # Write VLC output
    OUT_VLC.write_text("\n".join(vlc_lines) + "\n", encoding="utf-8")
    log.info(f"Written {OUT_VLC} with {channel_counter - 1} channels")
    
    # Write TiviMate output
    OUT_TIVI.write_text("\n".join(tivi_lines) + "\n", encoding="utf-8")
    log.info(f"Written {OUT_TIVI} with {channel_counter - 1} channels")


async def scrape() -> None:
    if cached_urls := CACHE_FILE.load():
        urls.update({k: v for k, v in cached_urls.items() if v["source"]})

        log.info(f"Loaded {len(urls)} event(s) from cache")

        return

    # Fixed f-string syntax error
    base_urls_str = " & ".join(i["base"] for i in BASE_URLS.values())
    log.info(f'Scraping from "{base_urls_str}"')

    if events := await get_events():
        log.info(f"Processing {len(events)} URL(s)")

        now = Time.clean(Time.now())

        for i, ev in enumerate(events, start=1):
            handler = partial(
                process_event,
                url=ev.link,
                url_num=i,
                sport=ev.sport,
            )

            source = await network.safe_process(
                handler,
                url_num=i,
                semaphore=network.HTTP_S,
                log=log,
            )

            key = f"[{ev.sport}] {ev.name} ({TAG})"

            tvg_id, logo = leagues.get_tvg_info(ev.sport, ev.name)

            entry = {
                "source": source,
                "logo": logo,
                "refer": BASE_URLS[ev.sport]["base"],
                "timestamp": now.timestamp(),
                "tvg-id": tvg_id or "Live.Event.us",
                "link": ev.link,
            }

            cached_urls[key] = entry

            if source:
                urls[key] = entry

        log.info(f"Collected and cached {len(urls)} event(s)")

    else:
        log.info("No events found")

    CACHE_FILE.write(cached_urls)


async def main():
    """Run the updater and generate outputs."""
    log.info("Starting CAST updaterr...")
    
    # Scrape or load from cache
    await scrape()
    
    # Generate output files
    write_outputs()
    
    log.info("CAST updater completed successfully")


if __name__ == "__main__":
    asyncio.run(main())
