import asyncio
import re
import os
import json
from collections.abc import KeysView
from functools import partial
from typing import Dict
from urllib.parse import urlsplit

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "FLY"

CACHE_FILE = Cache(TAG, exp=7_200)

API_FILE = Cache(f"{TAG}-api", exp=19_800)

# Secret variables
FLY_API_URL = os.getenv("FLY_API_URL")
VLC_USER_AGENT = os.getenv("VLC_USER_AGENT")
TIVIMATE_USER_AGENT = os.getenv("TIVIMATE_USER_AGENT")


# ---------- helpers ----------

def clean_name(s: str) -> str:
    return re.sub(r"(\r|\n)", "", s).strip()


def clean_m3u(s: str) -> str:
    return re.sub(r"\.live\n", ".pro", s)


def clean_display_name(name: str) -> str:
    if not name:
        return ""
    cleaned = re.sub(r",\s*", " ", name)
    return re.sub(r"\s+", " ", cleaned).strip()


def normalize_vs(name: str) -> str:
    return re.sub(r"\bVS\b", "vs", name, flags=re.I)


def encode_user_agent(ua: str) -> str:
    for a, b in ((" ", "%20"), ("(", "%28"), (")", "%29"),
                 (";", "%3B"), (",", "%2C")):
        ua = ua.replace(a, b)
    return ua


def origin_of(url: str) -> str:
    if not url:
        return ""
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else ""


# ---------- event processing ----------

async def process_event(url: str, url_num: int) -> tuple[str | None, str | None]:
    """Process event URL to extract M3U8 stream from encrypted iframe HTML."""
    nones = None, None

    if not (html_data := await network.request(url, url_num, log=log)):
        return nones

    soup = HTMLParser(html_data.content)
    iframe = soup.css_first("iframe")

    if not iframe or not (iframe_src := iframe.attributes.get("src")):
        log.warning(f"URL {url_num}) No iframe source found.")
        return nones

    if not (iframe_src_data := await network.request(
        iframe_src, url_num, headers={"Referer": url}, log=log
    )):
        return nones

    num_list_ptrn = re.compile(r"var\s+_(\w+)=\[([^\]]*)\],", re.S)
    index_ptrn = re.compile(r"(_[a-z]+\d+)=(\d+)")
    m3u_ptrn = re.compile(r'(var\s?signed_)?url\s?=\s?"(.*)";', re.I)

    if not (num_list_mtch := num_list_ptrn.findall(iframe_src_data.text)):
        log.warning(f"URL {url_num}) Unable to decipher m3u encryption.")
        return nones

    if not (index_mtch := index_ptrn.findall(iframe_src_data.text)):
        log.warning(f"URL {url_num}) Unable to decipher m3u encryption.")
        return nones

    num_list = (int(n.strip()) for n in num_list_mtch[-1][-1].split(","))

    if len(index_mtch) > 2:
        index_mtch.pop()

    x, y = (int(i[-1].strip()) for i in index_mtch)

    js = "".join(chr(((i ^ x) - y + 256) & 255) for i in num_list)

    if not (m3u_mtch := m3u_ptrn.search(js)):
        log.warning(f"URL {url_num}) No M3U8 source found.")
        return nones

    log.info(f"URL {url_num}) Captured M3U8")
    return json.loads(f'"{m3u_mtch[2]}"'), iframe_src


# ---------- event discovery ----------

async def get_events(cached_keys: KeysView[str]) -> list[Event]:
    now = Time.rn()
    events: list[Event] = []

    if not (api_data := API_FILE.load(per_entry=False, ts_index=-1)):
        log.info("Refreshing API cache")
        api_data = [{"timestamp": now.timestamp()}]

        if r := await network.request(FLY_API_URL, log=log):
            api_data: list[dict[str, str]] = r.json()
            api_data[-1]["timestamp"] = now.timestamp()

        API_FILE.write(api_data)

    # ===== EXPANDED WINDOW =====
    # -6h: catch events that already started (still live)
    # +48h: catch everything through tomorrow (API posts all events for today+tomorrow)
    start_dt = now.delta(hours=-6)
    end_dt = now.delta(hours=48)

    for event_group in api_data:
        if not all(
            values := [
                event_group.get(k)
                for k in ("Sport", "Team1", "Team2",
                          "MatchDate", "MatchStartTime", "IframeURL")
            ]
        ):
            continue

        sport, away, home, date, time, link = values

        try:
            event_dt = Time.from_str(f"{date} {time}", tz_name="UTC")
        except Exception as e:
            log.debug(f"Failed to parse date for {away} vs {home}: {e}")
            continue

        if not start_dt <= event_dt <= end_dt:
            continue

        sport = clean_name(sport)
        name = normalize_vs(clean_name(f"{away} vs {home}"))

        if f"[{sport}] {name} ({TAG})" in cached_keys:
            continue

        events.append(
            Event(
                sport=sport,
                name=name,
                link=link,
                timestamp=now.timestamp(),
            )
        )

    return events


# ---------- scraper ----------

async def scrape() -> None:
    cached_urls = CACHE_FILE.load()

    valid_urls = {k: v for k, v in cached_urls.items() if v["source"]}
    valid_count = cached_count = len(valid_urls)

    urls.update(valid_urls)

    log.info(f"Loaded {cached_count} event(s) from cache")
    log.info('Scraping from "https://flyembed.xyz"')

    if events := await get_events(cached_urls.keys()):
        log.info(f"Processing {len(events)} new URL(s)")

        for i, ev in enumerate(events, start=1):
            handler = partial(process_event, url=ev.link, url_num=i)

            source, iframe = await network.safe_process(
                handler,
                url_num=i,
                timeout_return=(None, None),
                semaphore=network.HTTP_S,
                log=log,
            )

            key = f"[{ev.sport}] {ev.name} ({TAG})"
            tvg_id, logo = leagues.get_tvg_info(ev.sport, ev.name)

            entry = {
                "source": source,
                "logo": logo,
                "refer": iframe or "",
                "timestamp": ev.timestamp,
                "tvg-id": tvg_id or "Live.Event.us",
                "link": ev.link,
                "sport": ev.sport,
                "name": ev.name,
            }

            cached_urls[key] = entry

            if source:
                valid_count += 1
                entry["source"] = clean_m3u(source)
                urls[key] = entry

        log.info(f"Collected and cached {valid_count - cached_count} new event(s)")
    else:
        log.info("No new events found")

    CACHE_FILE.write(cached_urls)
    await generate_m3u8_files(urls)


# ---------- m3u8 output ----------

async def generate_m3u8_files(channels_data: Dict[str, Dict[str, str | float]],
                              output_dir: str = ".") -> None:
    os.makedirs(output_dir, exist_ok=True)

    vlc_path = os.path.join(output_dir, "fly_vlc.m3u8")
    tivimate_path = os.path.join(output_dir, "fly_tivimate.m3u8")

    valid_channels = {k: v for k, v in channels_data.items() if v.get("source")}

    if not valid_channels:
        log.warning("No valid channels found to generate M3U8 files")
        for p in (vlc_path, tivimate_path):
            with open(p, "w", encoding="utf-8") as f:
                f.write("#EXTM3U\n")
            os.chmod(p, 0o644)
        return

    with open(vlc_path, "w", encoding="utf-8") as vlc_file:
        vlc_file.write("#EXTM3U\n")
        for chno, (key, channel) in enumerate(valid_channels.items(), 1):
            vlc_file.write(format_vlc_channel(key, channel, chno) + "\n")

    with open(tivimate_path, "w", encoding="utf-8") as tivimate_file:
        tivimate_file.write("#EXTM3U\n")
        for chno, (key, channel) in enumerate(valid_channels.items(), 1):
            tivimate_file.write(format_tivimate_channel(key, channel, chno) + "\n")

    os.chmod(vlc_path, 0o644)
    os.chmod(tivimate_path, 0o644)

    log.info(f"Generated {vlc_path} with {len(valid_channels)} channel(s)")
    log.info(f"Generated {tivimate_path} with {len(valid_channels)} channel(s)")


def format_vlc_channel(key: str, channel: Dict[str, str | float], chno: int) -> str:
    sport = channel.get("sport", "Live Event")
    name = channel.get("name", key)
    refer = channel.get("refer", "") or ""
    origin = origin_of(refer)

    display_name = clean_display_name(key.replace(f" ({TAG})", ""))
    tvg_name = f"[{sport}] {name} ({TAG})"

    extinf = (f'#EXTINF:-1 tvg-chno="{chno}" '
              f'tvg-id="{channel.get("tvg-id", "Live.Event.us")}" '
              f'tvg-name="{tvg_name}" '
              f'tvg-logo="{channel.get("logo", "")}" '
              f'group-title="{sport}",'
              f'{display_name}')

    options = [f"#EXTVLCOPT:http-referrer={refer}"]
    if origin:
        options.append(f"#EXTVLCOPT:http-origin={origin}")
    options.append(f"#EXTVLCOPT:http-user-agent={VLC_USER_AGENT}")

    url = channel.get("source", "")
    return f"{extinf}\n" + "\n".join(options) + f"\n{url}"


def format_tivimate_channel(key: str, channel: Dict[str, str | float], chno: int) -> str:
    sport = channel.get("sport", "Live Event")
    name = channel.get("name", key)
    refer = channel.get("refer", "") or ""

    display_name = clean_display_name(key.replace(f" ({TAG})", f" ({TAG})"))
    tvg_name = f"[{sport}] {name} ({TAG})"

    extinf = (f'#EXTINF:-1 tvg-chno="{chno}" '
              f'tvg-id="{channel.get("tvg-id", "Live.Event.us")}" '
              f'tvg-name="{tvg_name}" '
              f'tvg-logo="{channel.get("logo", "")}" '
              f'group-title="{sport}",'
              f'{display_name}')

    encoded_ua = encode_user_agent(TIVIMATE_USER_AGENT)

    params = [f"referer={refer}"]
    if origin := origin_of(refer):
        params.append(f"origin={origin}")
    params.append(f"user-agent={encoded_ua}")

    url = channel.get("source", "")
    return f"{extinf}\n{url}|{'|'.join(params)}"


async def main() -> None:
    log.info(f"Starting {TAG} updater")
    await scrape()
    log.info(f"{TAG} updater completed")


if __name__ == "__main__":
    asyncio.run(main())
