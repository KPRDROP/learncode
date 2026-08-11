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

TAG = "WEBCAST"

CACHE_FILE = Cache(TAG, exp=12_600)

# Output files
OUT_VLC = Path("webcast_vlc.m3u8")
OUT_TIVI = Path("webcast_tivimate.m3u8")

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


def extract_d_array(html_content: str) -> tuple[int, int, int] | None:
    """Extract the _d array from player HTML content."""
    # Look for var _d=[id,ts,pt]
    pattern = re.compile(r'var\s+_d\s*=\s*\[([^\]]+)\];', re.I)
    match = pattern.search(html_content)
    
    if not match:
        # Try alternative pattern (var x=[id,ts,pt])
        alt_pattern = re.compile(r'var\s+\w*\s*=\s*\[([^\]]+)\];', re.I)
        match = alt_pattern.search(html_content)
    
    if not match:
        return None
    
    try:
        values = ast.literal_eval(f"[{match.group(1)}]")
        if len(values) >= 3:
            return (int(values[0]), int(values[1]), int(values[2]))
    except (ValueError, SyntaxError):
        return None
    
    return None


async def process_event(
    url: str,
    url_num: int,
    sport: str,
) -> str | None:

    log.info(f"URL {url_num}) Processing: {url}")
    
    # Fetch the event page
    if not (event_data := await network.request(url, url_num, log=log)):
        log.warning(f"URL {url_num}) Failed to fetch event page")
        return

    soup = HTMLParser(event_data.content)

    # Find the iframe
    if not (iframe := soup.css_first('iframe[name="srcFrame"]')):
        log.warning(f"URL {url_num}) No iframe element found.")
        return

    if not (iframe_src := iframe.attributes.get("src")):
        log.warning(f"URL {url_num}) No iframe source found.")
        return

    elif iframe_src.lower() == "about:blank":
        iframe_src = iframe.attributes.get("data-litespeed-src")

    if not iframe_src:
        log.warning(f"URL {url_num}) No iframe source found.")
        return

    log.info(f"URL {url_num}) Found iframe: {iframe_src}")

    # Fetch the iframe content
    if not (
        iframe_src_data := await network.request(
            iframe_src,
            url_num,
            headers={"Referer": url},
            log=log,
        )
    ):
        log.warning(f"URL {url_num}) Failed to fetch iframe content")
        return

    # Extract _d array from iframe content
    d_values = extract_d_array(iframe_src_data.text)
    
    if not d_values:
        log.warning(f"URL {url_num}) No _d array found in iframe")
        return

    ev_id, ev_ts, ev_pt = d_values
    log.info(f"URL {url_num}) Found _d array: id={ev_id}, ts={ev_ts}, pt={ev_pt}")

    params: dict[str, int | str] = dict(zip(["id", "ts", "pt"], [ev_id, ev_ts, ev_pt]))

    # Make API request
    api_url = urljoin(BASE_URLS[sport]["base"], BASE_URLS[sport]["api"])
    log.info(f"URL {url_num}) Calling API: {api_url}")
    
    if not (
        api_data := await network.request(
            api_url,
            url_num,
            headers={"Referer": iframe_src},
            params=params,
            log=log,
        )
    ):
        log.warning(f"URL {url_num}) API request failed")
        return

    data = api_data.json()
    
    if data.get("error"):
        log.warning(f"URL {url_num}) API error: {data.get('error')}")
        return

    if not (m3u8 := data.get("url")):
        log.warning(f"URL {url_num}) No M3U8 found in response")
        return

    log.info(f"URL {url_num}) Captured M3U8: {m3u8[:100]}...")
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

        # Look for game rows
        for row in soup.css("tr.singele_match_date"):
            # Skip header rows
            if row.css_first(".mdatetitle"):
                continue
                
            if not (vs_node := row.css_first("td.teamvs a")):
                continue

            event_name = vs_node.text(strip=True)

            # Remove date from event name
            for span in vs_node.css("span.mtdate"):
                date = span.text(strip=True)
                event_name = event_name.replace(date, "").strip()

            if not (href := vs_node.attributes.get("href")):
                continue

            # Fix the URL if it's relative
            if href.startswith("/"):
                href = urljoin(url, href)

            events.append(
                Event(
                    sport=sport,
                    name=fix_event(event_name),
                    link=href,
                )
            )

        # If no events found in game rows, try team logos
        if not events:
            log.info(f"No game rows found for {sport}, trying team logos...")
            for link in soup.css("li.team-logo a"):
                href = link.attributes.get("href")
                title = link.attributes.get("title", "")
                
                if href and "-live" in href.lower():
                    team_name = title.replace(" Live Stream", "").strip()
                    if not team_name:
                        # Extract from URL
                        team_name = href.rstrip("/").split("/")[-1].replace("-live", "").replace("-", " ").title()
                    
                    if href.startswith("/"):
                        href = urljoin(url, href)
                    
                    events.append(
                        Event(
                            sport=sport,
                            name=team_name,
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

    base_urls_str = " & ".join(i["base"] for i in BASE_URLS.values())
    log.info(f'Scraping from "{base_urls_str}"')

    if events := await get_events():
        log.info(f"Processing {len(events)} URL(s)")

        now = Time.clean(Time.now())
        cached_urls = {}

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
        CACHE_FILE.write(cached_urls)
    else:
        log.info("No events found")


async def main():
    """Main function to run the scraper and generate outputs."""
    log.info("Starting MLB Webcast Updater...")
    
    # Scrape or load from cache
    await scrape()
    
    # Generate output files
    write_outputs()
    
    log.info("MLB Webcast Updater completed successfully")


if __name__ == "__main__":
    asyncio.run(main())
