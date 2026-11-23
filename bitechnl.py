from typing import Any
import asyncio

from utils import Cache, Time, get_logger, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "BITECHNL"

REFERER = "https://sportsbite.org/"
ORIGIN = "https://sportsbite.org"

USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 9; G011A Build/PI) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/68.0.3440.70 Mobile Safari/537.36"
)

BASE_URL = "https://sportsbite.org"

API_URL = "https://sportsbite.org/api/forestgump/channels"

CACHE_FILE = Cache(TAG, exp=10_800)

VLC_FILE = "bitechls_vlc.m3u8"
TIVIMATE_FILE = "bitechls_tivimate.m3u8"


def process_stream(iframe_url: str, channel_id: str) -> str:
    """
    Convert an iframe/embed URL to a playable 247 stream URL.

    Example:
      iframe_url : https://sportsbite.org/247-embed/embed/bbcone
      channel_id : bbcone  (extracted from "id": "247-bbcone")
      -> output  : https://sportsbite.org/247/bbcone
    """
    # Prefer parsing the last path segment of the iframe URL (most reliable)
    if iframe_url and "/embed/" in iframe_url:
        slug = iframe_url.rstrip("/").rsplit("/embed/", 1)[-1]
    else:
        slug = channel_id.split("-", 1)[-1] if "-" in channel_id else channel_id

    return f"{BASE_URL}/247/{slug}"


async def fetch_channels() -> list[dict[str, Any]]:
    """Fetch all channels from the API endpoint."""
    if r := await network.request(API_URL, log=log):
        data = r.json()
        # The endpoint returns a list (or possibly an object wrapping a list)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("channels", "data", "items", "results"):
                if isinstance(data.get(key), list):
                    return data[key]
        log.warning(f"Unexpected API response shape: {type(data)}")
    return []


async def get_channels() -> dict[str, dict[str, str | float]]:
    """Build the channel dictionary keyed by a unique name."""
    events: dict[str, dict[str, str | float]] = {}

    channels = await fetch_channels()

    log.info(f"API returned {len(channels)} channel(s)")

    i = 1
    for ch in channels:
        if not isinstance(ch, dict):
            continue

        # Skip channels not marked online (safe-guard; keeps only working entries)
        if str(ch.get("status", "")).lower() != "online":
            continue

        channel_id = ch.get("id")
        name = ch.get("name")
        iframe_url = ch.get("iframe_url") or ch.get("embed_url") or ch.get("manifest_url")

        if not all((channel_id, name, iframe_url)):
            continue

        logo = ch.get("logo_url") or ch.get("image") or ""

        # Build a unique key so duplicate display names are possible without collision
        key = f"{name} [{channel_id}]"

        if key in events:
            continue

        events[key] = {
            "source": process_stream(iframe_url, channel_id),
            "logo": logo,
            "refer": BASE_URL,
            "tvg-id": channel_id,
            "name": name,
        }

        log.info(f"Channel {i}) Captured {name} -> {events[key]['source']}")
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
                tvg_id = info.get("tvg-id", "")
                name = info.get("name", "")

                display_name = f"{name} ({TAG})"

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
        from urllib.parse import quote

        encoded_ua = quote(USER_AGENT, safe="")
        encoded_ref = quote(REFERER, safe="")
        encoded_origin = quote(ORIGIN, safe="")

        with open(TIVIMATE_FILE, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")

            for chno, (key, info) in enumerate(events.items(), start=1):
                source = info.get("source", "")
                logo = info.get("logo", "")
                tvg_id = info.get("tvg-id", "")
                name = info.get("name", "")

                display_name = f"{name} ({TAG})"

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
    log.info(f'Scraping from "{API_URL}"')

    urls.update(await get_channels())

    if not urls:
        log.warning("No channels collected — output files will be empty")
    else:
        log.info(f"Collected {len(urls)} channel(s)")

    CACHE_FILE.write(urls)

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
