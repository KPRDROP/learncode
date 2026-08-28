import asyncio
import os

from collections.abc import KeysView
from dataclasses import dataclass
from functools import partial
from typing import Any
from urllib.parse import urljoin, quote

from utils import Cache, Event, Time, get_logger, leagues, network


log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "DAM"

CACHE_FILE = Cache(TAG, exp=10_800)
API_FILE = Cache(f"{TAG}-api", exp=28_800)

# BASE_URL is supplied by GitHub Actions Secrets/Variables.
BASE_URL = os.getenv("DAM_BASE_URL")

# User-Agent used in generated VLC/TiviMate playlists.
USER_AGENT = os.getenv(
    "DAM_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36",
)

# Output files
VLC_OUTPUT = "dam_vlc.m3u8"
TIVIMATE_OUTPUT = "dam_tivimate.m3u8"

GROUP_TITLE = "Live Events"


@dataclass(kw_only=True, slots=True)
class DAMIEvent(Event):
    stream_id: str
    link: str | None = None
    logo: str | None = None


async def process_event(stream_id: str, url_num: int) -> str | None:
    """
    Extract the HLS/M3U8 URL for a DAM stream.
    """

    if not BASE_URL:
        log.error("DAM_BASE_URL is not configured.")
        return None

    if not (
        event_data := await network.request(
            urljoin(BASE_URL, f"papi/extract-url/{stream_id}"),
            url_num,
            log=log,
        )
    ):
        return None

    try:
        api_data = event_data.json()
    except Exception as exc:
        log.warning(f"URL {url_num}) Invalid JSON response: {exc}")
        return None

    if not api_data.get("success"):
        log.warning(
            f"URL {url_num}) Unsuccessful Request: "
            f"{api_data.get('error')}"
        )
        return None

    if not (m3u8 := api_data.get("hlsUrl", api_data.get("sdUrl"))):
        log.warning(f"URL {url_num}) No source found.")
        return None

    log.info(f"URL {url_num}) Captured M3U8")

    return m3u8


async def get_events(cached_keys: KeysView[str]) -> list[DAMIEvent]:
    """
    Retrieve today's DAM events and return only events that
    are within the configured time window and are not cached.
    """

    now = Time.rn()

    events: list[DAMIEvent] = []

    if not (api_data := API_FILE.load(per_entry=False, ts_index=-1)):
        log.info("Refreshing API cache")

        api_data = [{"timestamp": now.timestamp()}]

        if r := await network.request(
            urljoin(BASE_URL, "papi/matches/all-today"),
            log=log,
        ):
            try:
                response_data = r.json()

                if isinstance(response_data, list):
                    api_data = response_data

                    if api_data:
                        api_data[-1]["timestamp"] = now.timestamp()

            except Exception as exc:
                log.warning(f"Unable to parse API response: {exc}")

        API_FILE.write(api_data)

    # Keep the existing ±30 minute event window.
    start_dt = now.delta(minutes=-30)
    end_dt = now.delta(minutes=30)

    for event in api_data:
        if not all(
            values := [
                event.get(x)
                for x in (
                    "title",
                    "league",
                    "date",
                    "id",
                )
            ]
        ):
            continue

        name, sport, start_ts, stream_id = values

        if not isinstance(stream_id, str):
            continue

        if stream_id.lower().startswith("dl-"):
            continue

        if stream_id.startswith("247") or (
            isinstance(sport, str) and sport.startswith("24/7")
        ):
            continue

        try:
            event_dt = Time.from_ts(int(f"{start_ts}"[:-3]))
        except (ValueError, TypeError):
            log.warning(
                f"Invalid event timestamp for {name}: {start_ts}"
            )
            continue

        key = f"[{sport}] {name} ({TAG})"

        if key in cached_keys:
            continue

        if not start_dt <= event_dt <= end_dt:
            continue

        events.append(
            DAMIEvent(
                sport=sport,
                name=name,
                logo=event.get("poster"),
                stream_id=stream_id,
                timestamp=event_dt.timestamp(),
            )
        )

    return events


def write_playlists() -> None:
    """
    Generate the VLC and TiviMate M3U8 playlist files.

    dam_vlc.m3u8:
        Uses EXTVLCOPT headers.

    dam_tivimate.m3u8:
        Uses pipe-separated headers and a URL-encoded User-Agent.
    """

    log.info("Generating playlist files")

    # URL-encode the complete User-Agent for TiviMate.
    encoded_user_agent = quote(USER_AGENT, safe="")

    vlc_lines: list[str] = ["#EXTM3U"]
    tivimate_lines: list[str] = ["#EXTM3U"]

    channel_number = 1

    for key, entry in urls.items():
        source = entry.get("source")

        if not source:
            continue

        source = str(source)

        logo = entry.get("logo") or ""
        tvg_id = entry.get("tvg-id") or "Live.Event.us"
        refer = entry.get("refer") or BASE_URL

        # Convert values to strings safely.
        logo = str(logo)
        tvg_id = str(tvg_id)
        refer = str(refer)

        # ---------------------------------------------------------
        # VLC playlist
        # ---------------------------------------------------------

        vlc_lines.append(
            f'#EXTINF:-1 '
            f'tvg-chno="{channel_number}" '
            f'tvg-id="{tvg_id}" '
            f'tvg-name="{key}" '
            f'tvg-logo="{logo}" '
            f'group-title="{GROUP_TITLE}",'
            f'{key}'
        )

        vlc_lines.append(
            f"#EXTVLCOPT:http-referrer={refer}"
        )

        vlc_lines.append(
            f"#EXTVLCOPT:http-origin={refer}"
        )

        vlc_lines.append(
            f"#EXTVLCOPT:http-user-agent={USER_AGENT}"
        )

        vlc_lines.append(source)

        # ---------------------------------------------------------
        # TiviMate playlist
        # ---------------------------------------------------------

        tivimate_lines.append(
            f'#EXTINF:-1 '
            f'tvg-chno="{channel_number}" '
            f'tvg-id="{tvg_id}" '
            f'tvg-name="{key}" '
            f'tvg-logo="{logo}" '
            f'group-title="{GROUP_TITLE}",'
            f'{key}'
        )

        tivimate_lines.append(
            f"{source}"
            f"|referer={refer}"
            f"|origin={refer}"
            f"|user-agent={encoded_user_agent}"
        )

        channel_number += 1

    # Always write both files, even when there are currently no
    # valid events. This prevents stale output files from remaining.
    with open(VLC_OUTPUT, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(vlc_lines) + "\n")

    with open(
        TIVIMATE_OUTPUT,
        "w",
        encoding="utf-8",
        newline="\n",
    ) as f:
        f.write("\n".join(tivimate_lines) + "\n")

    valid_entries = channel_number - 1

    log.info(
        f"Generated {VLC_OUTPUT} with {valid_entries} event(s)"
    )

    log.info(
        f"Generated {TIVIMATE_OUTPUT} with {valid_entries} event(s)"
    )


async def scrape() -> None:
    """
    Main scraper process.
    """

    if not BASE_URL:
        raise RuntimeError(
            "DAM_BASE_URL environment variable is not configured."
        )

    cached_urls = CACHE_FILE.load()

    valid_urls = {
        k: v
        for k, v in cached_urls.items()
        if v.get("source")
    }

    valid_count = cached_count = len(valid_urls)

    urls.update(valid_urls)

    log.info(
        f"Loaded {cached_count} event(s) from cache"
    )

    log.info(
        f'Scraping from "{BASE_URL}"'
    )

    if events := await get_events(cached_urls.keys()):
        log.info(
            f"Processing {len(events)} new URL(s)"
        )

        for i, ev in enumerate(events, start=1):
            handler = partial(
                process_event,
                stream_id=ev.stream_id,
                url_num=i,
            )

            source = await network.safe_process(
                handler,
                url_num=i,
                semaphore=network.HTTP_S,
                log=log,
            )

            key = f"[{ev.sport}] {ev.name} ({TAG})"

            tvg_id, logo = leagues.get_tvg_info(
                ev.sport,
                ev.name,
            )

            entry = {
                "source": source,
                "logo": ev.logo or logo,
                "refer": urljoin(
                    BASE_URL,
                    f"embed/?id={ev.stream_id}",
                ),
                "timestamp": ev.timestamp,
                "tvg-id": tvg_id or "Live.Event.us",
            }

            cached_urls[key] = entry

            if source:
                valid_count += 1
                urls[key] = entry

        log.info(
            f"Collected and cached "
            f"{valid_count - cached_count} new event(s)"
        )

    else:
        log.info("No new events found")

    # Save scraper cache.
    CACHE_FILE.write(cached_urls)

    # Generate the two playlist files from all currently valid
    # cached events plus any newly discovered events.
    write_playlists()


async def main() -> None:
    """
    Application entry point.
    """

    log.info("Starting DAM updater")

    await scrape()

    log.info("DAM updater finished successfully")


if __name__ == "__main__":
    asyncio.run(main())
