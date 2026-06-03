#!/usr/bin/env python3
"""
Sportsonline Stream Extractor
Direct connection without proxy support
Improved header extraction and Tivimate format support
"""

import asyncio
import base64
import logging
import re
import json
from urllib.parse import urlparse, urljoin, quote
from typing import Dict, Any, List, Optional, Tuple
import aiohttp
from aiohttp import ClientSession, ClientTimeout, TCPConnector

# Try to import brotli for decompression support
try:
    import brotli
    BROTLI_AVAILABLE = True
except ImportError:
    BROTLI_AVAILABLE = False
    print("Warning: brotli not installed. Some compressed responses may fail.")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class ExtractorError(Exception):
    """Custom exception for extraction errors."""
    pass


def unpack(p, a, c, k, e=None, d=None):
    """
    Unpacker for P.A.C.K.E.R. packed javascript.
    This is a Python port of the common Javascript unpacker.
    """
    while c > 0:
        c -= 1
        if k[c]:
            p = re.sub("\\b" + _int2base(c, a) + "\\b", k[c], p)
    return p


def _int2base(x, base):
    if x < 0:
        sign = -1
    elif x == 0:
        return "0"
    else:
        sign = 1

    x *= sign
    digits = []

    while x:
        digits.append("0123456789abcdefghijklmnopqrstuvwxyz"[x % base])
        x = int(x / base)

    if sign < 0:
        digits.append("-")

    digits.reverse()
    return "".join(digits)


class SportsonlineExtractor:
    """Sportsonline/Sportzonline URL extractor for M3U8 streams."""

    def __init__(self, request_headers: dict = None):
        self.request_headers = request_headers or {}
        self.base_headers = {
            "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Mobile Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }
        self.session = None
        self.mediaflow_endpoint = "hls_manifest_proxy"
        self._session_lock = asyncio.Lock()
        self.real_referer = None
        self.real_origin = None

    def update_request_headers(self, request_headers: dict | None):
        self.request_headers = request_headers or {}

    def _get_request_header(self, name: str, default: str | None = None) -> str | None:
        for header_name, header_value in self.request_headers.items():
            if header_name.lower() == name.lower():
                return header_value
        return default

    def _get_origin(self, url: str) -> str:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"

    def _build_page_headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": self._get_request_header("User-Agent", self.base_headers["User-Agent"]),
            "Accept": self._get_request_header("Accept", self.base_headers["Accept"]),
            "Accept-Language": self._get_request_header("Accept-Language", self.base_headers["Accept-Language"]),
            "Accept-Encoding": "gzip, deflate",
            "Connection": self.base_headers["Connection"],
            "Upgrade-Insecure-Requests": self.base_headers["Upgrade-Insecure-Requests"],
        }
        
        cookie = self._get_request_header("Cookie")
        if cookie:
            headers["Cookie"] = cookie
            
        referer = self._get_request_header("Referer")
        if referer:
            headers["Referer"] = referer
            
        return headers

    def _build_iframe_headers(self, page_url: str, iframe_url: str) -> dict[str, str]:
        headers = self._build_page_headers()
        headers["Referer"] = page_url
        headers["Origin"] = self._get_origin(page_url)
        headers["Sec-Fetch-Site"] = (
            "same-origin"
            if urlparse(page_url).netloc == urlparse(iframe_url).netloc
            else "cross-site"
        )
        headers["Sec-Fetch-Mode"] = "iframe"
        headers["Sec-Fetch-Dest"] = "iframe"
        return headers

    async def _get_session(self):
        if self.session is None or self.session.closed:
            if self.session and not self.session.closed:
                await self.session.close()

            timeout = ClientTimeout(total=60, connect=30, sock_read=30)
            connector = TCPConnector(
                limit=0,
                limit_per_host=0,
                ssl=False,
                enable_cleanup_closed=True
            )

            self.session = ClientSession(
                timeout=timeout,
                connector=connector,
                headers=self.base_headers,
                cookie_jar=aiohttp.CookieJar()
            )
        return self.session

    async def _make_request(self, url: str, headers: dict = None, retries: int = 3, timeout: int = 30):
        """Make HTTP requests directly without proxy."""
        final_headers = headers or self.base_headers.copy()
        
        # Ensure we don't request brotli compression
        final_headers['Accept-Encoding'] = 'gzip, deflate'

        for attempt in range(retries):
            try:
                logger.debug(f"Request attempt {attempt + 1}/{retries} for URL: {url}")
                session = await self._get_session()
                
                async with session.get(url, headers=final_headers, timeout=timeout, ssl=False) as response:
                    if response.status == 403:
                        logger.warning(f"Access forbidden (403) for {url}")
                        if attempt < retries - 1:
                            await asyncio.sleep(2 ** attempt)
                            continue
                        raise ExtractorError(f"Access forbidden: {url}")
                    
                    if response.status == 404:
                        logger.warning(f"Page not found (404) for {url}")
                        raise ExtractorError(f"Page not found: {url}")
                    
                    if response.status == 400:
                        logger.warning(f"Bad request (400) for {url}")
                        if attempt < retries - 1:
                            await asyncio.sleep(2 ** attempt)
                            continue
                        raise ExtractorError(f"Bad request: {url}")
                    
                    response.raise_for_status()
                    html = await self._handle_response_content(response)
                    if not html:
                        raise ExtractorError(f"Empty response for {url}")
                    return html, str(response.url)

            except aiohttp.ClientError as e:
                logger.warning(f"Request attempt {attempt + 1} failed for {url}: {str(e)}")
                if attempt < retries - 1:
                    await asyncio.sleep(2 ** attempt)
                else:
                    raise ExtractorError(f"All request attempts failed for {url}: {str(e)}")
            except ExtractorError:
                raise
            except Exception as e:
                logger.error(f"Unexpected error for {url}: {str(e)}")
                if attempt < retries - 1:
                    await asyncio.sleep(2 ** attempt)
                else:
                    raise ExtractorError(f"Request failed for {url}: {str(e)}")
        
        raise ExtractorError(f"Unable to complete request for {url}")

    async def _handle_response_content(self, response: aiohttp.ClientResponse) -> str:
        """Read response body, handling various encodings."""
        raw_body = await response.read()
        
        # Check if response is brotli compressed
        content_encoding = response.headers.get('Content-Encoding', '').lower()
        
        if 'br' in content_encoding and BROTLI_AVAILABLE:
            try:
                raw_body = brotli.decompress(raw_body)
                logger.debug("Successfully decompressed brotli response")
            except Exception as e:
                logger.warning(f"Failed to decompress brotli: {e}")
        
        # Try to decode with proper charset
        charset = response.charset
        if not charset:
            content_type = response.headers.get('Content-Type', '')
            if 'charset=' in content_type:
                charset = content_type.split('charset=')[-1].split(';')[0].strip()
            else:
                charset = 'utf-8'
        
        try:
            return raw_body.decode(charset, errors='replace')
        except UnicodeDecodeError:
            return raw_body.decode('utf-8', errors='replace')

    def _extract_iframe_info(self, html: str, base_url: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """Extract iframe URL, real referer and origin from the HTML."""
        # Look for player iframe
        iframe_patterns = [
            r'<!--player--><iframe[^>]+src=["\']([^"\']+)["\']',
            r'<iframe[^>]+src=["\']([^"\']+)["\'][^>]*>',
            r'player["\']?\s*:\s*["\']([^"\']+)["\']',
        ]
        
        iframe_url = None
        for pattern in iframe_patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                iframe_url = self._normalize_stream_url(match.group(1), base_url)
                break
        
        if not iframe_url:
            return None, None, None
        
        # Extract real referer and origin from the iframe URL domain
        parsed_iframe = urlparse(iframe_url)
        real_origin = f"{parsed_iframe.scheme}://{parsed_iframe.netloc}"
        real_referer = iframe_url
        
        # Also look for any meta referrer tags
        meta_ref = re.search(r'<meta[^>]+name=["\']referrer["\'][^>]+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
        if meta_ref:
            logger.debug(f"Found meta referrer: {meta_ref.group(1)}")
        
        return iframe_url, real_referer, real_origin

    def _detect_packed_blocks(self, html: str) -> list[str]:
        """Detect P.A.C.K.E.R. packed JavaScript blocks."""
        raw_matches: list[str] = []
        
        # Pattern for packed JavaScript
        packed_pattern = re.compile(
            r'eval\s*\(\s*function\s*\(\s*p\s*,\s*a\s*,\s*c\s*,\s*k\s*,\s*e\s*,\s*[dr]\s*\)\s*\{.*?\}\s*\(\s*.*?\s*\)\s*\)',
            re.IGNORECASE | re.DOTALL
        )
        
        # First look in script tags
        script_pattern = re.compile(r'<script[^>]*>(.*?)</script>', re.IGNORECASE | re.DOTALL)
        for script_body in script_pattern.findall(html):
            matches = packed_pattern.findall(script_body)
            raw_matches.extend(matches)
        
        # If not found, search entire HTML
        if not raw_matches:
            raw_matches = packed_pattern.findall(html)
        
        return raw_matches

    @staticmethod
    def _extract_m3u8_candidate(text: str) -> str | None:
        """Extract m3u8 URL from text."""
        patterns = [
            r'["\'](https?://[^"\']+\.m3u8[^"\']*)["\']',
            r'(https?://[^\s<>"\']+\.m3u8[^\s<>"\']*)',
            r'["\'](//[^"\']+\.m3u8[^"\']*)["\']',
            r'(//[^\s<>"\']+\.m3u8[^\s<>"\']*)',
            r'(?:file|src|url)\s*[:=]\s*["\']([^"\']+\.m3u8[^"\']*)["\']',
            r'stream_url["\']\s*:\s*["\']([^"\']+)["\']',
            r'var\s+src\s*=\s*["\']([^"\']+\.m3u8[^"\']*)["\']',
        ]
        
        for pattern in patterns:
            matches = re.findall(pattern, text, re.IGNORECASE)
            for match in matches:
                if '.m3u8' in str(match):
                    return match
        
        return None

    @staticmethod
    def _extract_econfig_m3u8(html: str) -> str | None:
        """Decode current dynmill player config and return its stream URL."""
        config_match = re.search(r"window\._econfig\s*=\s*['\"]([^'\"]+)['\"]", html)
        if not config_match:
            return None

        try:
            encoded_config = config_match.group(1)
            decoded_config = base64.b64decode(
                encoded_config + "=" * (-len(encoded_config) % 4)
            ).decode("latin1")

            part_order = [2, 0, 3, 1]
            part_length = -(-len(decoded_config) // 4)
            encoded_parts = []
            offset = 0

            for _ in range(4):
                part = decoded_config[offset: offset + part_length]
                offset += part_length
                encoded_parts.append(part[:3] + part[4:])

            decoded_parts = [""] * 4
            for index, part in enumerate(encoded_parts):
                decoded_parts[part_order[index]] = base64.b64decode(
                    part + "=" * (-len(part) % 4)
                ).decode("latin1")

            joined_config = "".join(decoded_parts)
            config_json = base64.b64decode(
                joined_config + "=" * (-len(joined_config) % 4)
            ).decode("utf-8")
            config = json.loads(config_json)
        except Exception as e:
            logger.debug(f"Failed to decode Sportsonline _econfig: {e}")
            return None

        return config.get("stream_url_nop2p") or config.get("stream_url")

    @staticmethod
    def _normalize_stream_url(stream_url: str, base_url: str) -> str:
        """Normalize stream URL."""
        cleaned = stream_url.strip().strip("\"'").replace("\\/", "/")
        if cleaned.startswith("//"):
            parsed_base = urlparse(base_url)
            return f"{parsed_base.scheme or 'https'}:{cleaned}"
        if not urlparse(cleaned).scheme:
            return urljoin(base_url, cleaned)
        return cleaned

    @staticmethod
    def extract_unpack(packed_js: str) -> str:
        """Unpack P.A.C.K.E.R. packed javascript."""
        try:
            # Extract the parameters
            match = re.search(r'}\((.*)\)\)\s*$', packed_js)
            if not match:
                # Try alternative pattern
                match = re.search(r'}\(([^)]+)\)\)', packed_js)
            if not match:
                raise ValueError("Cannot find packed data.")
            
            # Safely evaluate the parameters
            params_str = match.group(1)
            # Split by comma but respect parentheses
            import ast
            # Use ast.literal_eval for safety
            params = ast.literal_eval(f'[{params_str}]')
            
            if len(params) >= 4:
                p, a, c, k = params[0], params[1], params[2], params[3]
                e = params[4] if len(params) > 4 else None
                d = params[5] if len(params) > 5 else None
                
                if isinstance(k, list):
                    # Convert k list to dict format expected by unpack
                    k_dict = {i: k[i] for i in range(len(k))}
                else:
                    k_dict = {}
                    
                return unpack(p, a, c, k_dict, e, d)
            else:
                raise ValueError("Invalid packed parameters")
                
        except Exception as e:
            raise ValueError(f"Failed to unpack JS: {e}")

    async def extract(self, url: str, **kwargs) -> Dict[str, Any]:
        """Main extraction flow: fetch page, extract iframe, unpack and find m3u8."""
        try:
            self.update_request_headers(kwargs.get("request_headers"))
            
            parsed_source = urlparse(url)
            source_origin = f"{parsed_source.scheme}://{parsed_source.netloc}"
            source_referer = self._get_request_header("Referer") or f"{source_origin}/"
            user_agent = self._get_request_header("User-Agent", self.base_headers["User-Agent"])

            # Step 1: Fetch main page
            logger.debug(f"Fetching main page: {url}")
            main_headers = self._build_page_headers()
            if source_referer:
                main_headers["Referer"] = source_referer

            main_html, main_url = await self._make_request(url, headers=main_headers)

            # Extract iframe with real referer and origin
            iframe_url, real_referer, real_origin = self._extract_iframe_info(main_html, main_url)
            
            iframe_html = main_html
            if iframe_url:
                logger.debug(f"Found iframe URL: {iframe_url}")
                logger.debug(f"Real Referer: {real_referer}")
                logger.debug(f"Real Origin: {real_origin}")

                # Fetch iframe content with proper headers
                iframe_headers = self._build_iframe_headers(main_url, iframe_url)
                try:
                    iframe_html, active_iframe_url = await self._make_request(iframe_url, headers=iframe_headers)
                    iframe_url = active_iframe_url
                    logger.debug(f"Iframe HTML length: {len(iframe_html)}")
                except ExtractorError as e:
                    logger.warning(f"Failed to fetch iframe: {e}, trying main HTML")
                    iframe_html = main_html
            else:
                logger.debug("No iframe found, using main HTML")
                # Try to find player config in main HTML
                real_referer = main_url
                real_origin = self._get_origin(main_url)

            # Build playback headers with real referer and origin
            if real_referer and real_origin:
                playback_headers = {
                    "Referer": real_referer,
                    "Origin": real_origin,
                    "User-Agent": user_agent,
                }
            else:
                parsed_iframe = urlparse(iframe_url)
                playback_headers = {
                    "Referer": iframe_url,
                    "Origin": f"{parsed_iframe.scheme}://{parsed_iframe.netloc}",
                    "User-Agent": user_agent,
                }

            # Try direct m3u8 extraction first
            m3u8_url = self._extract_m3u8_candidate(iframe_html)
            if not m3u8_url:
                m3u8_url = self._extract_econfig_m3u8(iframe_html)
            
            if m3u8_url:
                m3u8_url = self._normalize_stream_url(m3u8_url, iframe_url)
                logger.info(f"Found direct m3u8 URL: {m3u8_url}")
                return {
                    "destination_url": m3u8_url,
                    "request_headers": playback_headers,
                    "mediaflow_endpoint": self.mediaflow_endpoint,
                }

            # Try packed blocks
            packed_blocks = self._detect_packed_blocks(iframe_html)
            logger.debug(f"Found {len(packed_blocks)} packed blocks")

            for i, block in enumerate(packed_blocks):
                try:
                    unpacked = self.extract_unpack(block)
                    m3u8_url = self._extract_m3u8_candidate(unpacked)
                    if m3u8_url:
                        logger.debug(f"Found m3u8 in packed block {i}")
                        break
                except Exception as e:
                    logger.debug(f"Failed to unpack block {i}: {e}")
                    continue

            if not m3u8_url:
                # Last resort: scan entire HTML for any m3u8
                all_matches = re.findall(r'https?://[^\s<>"\']+\.m3u8[^\s<>"\']*', iframe_html, re.IGNORECASE)
                if all_matches:
                    m3u8_url = all_matches[0]
                    logger.debug(f"Found m3u8 via direct scan: {m3u8_url}")

            if not m3u8_url:
                raise ExtractorError("Could not extract m3u8 URL")

            m3u8_url = self._normalize_stream_url(m3u8_url, iframe_url)
            logger.info(f"Successfully extracted m3u8 URL: {m3u8_url}")

            return {
                "destination_url": m3u8_url,
                "request_headers": playback_headers,
                "mediaflow_endpoint": self.mediaflow_endpoint,
            }

        except ExtractorError:
            raise
        except Exception as e:
            logger.exception(f"Sportsonline extraction failed for {url}")
            raise ExtractorError(f"Extraction failed: {str(e)}")

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
            self.session = None


async def fetch_prog_txt() -> List[Dict[str, Any]]:
    """Fetch and parse prog.txt to get channel information."""
    prog_url = "https://sportsonline.sc/prog.txt"
    channels = []
    target_channels = ["HD1", "HD2", "HD5", "HD6", "HD8", "HD9", "HD10", "HD11"]
    
    try:
        async with ClientSession() as session:
            async with session.get(prog_url, ssl=False) as response:
                if response.status == 200:
                    content = await response.text()
                    lines = content.split('\n')
                    
                    current_day = None
                    for line in lines:
                        line = line.strip()
                        if line in ["TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY", "MONDAY"]:
                            current_day = line
                        elif 'x' in line and '|' in line and 'https://' in line:
                            parts = line.split('|')
                            if len(parts) >= 2:
                                event_info = parts[0].strip()
                                stream_url = parts[1].strip()
                                
                                # Extract time and teams
                                event_parts = event_info.split()
                                if len(event_parts) >= 3:
                                    time = event_parts[0]
                                    teams = ' '.join(event_parts[1:])
                                    
                                    # Check if this matches our target channels
                                    for channel in target_channels:
                                        channel_lower = channel.lower()
                                        url_lower = stream_url.lower()
                                        if f'/{channel_lower}/' in url_lower or f'/{channel_lower}.' in url_lower or f'/{channel_lower}?' in url_lower:
                                            channels.append({
                                                'day': current_day,
                                                'time': time,
                                                'teams': teams,
                                                'channel': channel,
                                                'url': stream_url,
                                            })
                                            break
    except Exception as e:
        logger.error(f"Failed to fetch prog.txt: {e}")
    
    return channels


def parse_prog_txt_from_file(filepath: str) -> List[Dict[str, Any]]:
    """Parse local prog.txt file."""
    channels = []
    target_channels = ["HD1", "HD2", "HD5", "HD6", "HD8", "HD9", "HD10", "HD11"]
    
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
            lines = content.split('\n')
            
            current_day = None
            for line in lines:
                line = line.strip()
                if line in ["TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY", "MONDAY"]:
                    current_day = line
                elif 'x' in line and '|' in line and 'https://' in line:
                    parts = line.split('|')
                    if len(parts) >= 2:
                        event_info = parts[0].strip()
                        stream_url = parts[1].strip()
                        
                        event_parts = event_info.split()
                        if len(event_parts) >= 3:
                            time = event_parts[0]
                            teams = ' '.join(event_parts[1:])
                            
                            for channel in target_channels:
                                channel_lower = channel.lower()
                                url_lower = stream_url.lower()
                                if f'/{channel_lower}/' in url_lower or f'/{channel_lower}.' in url_lower:
                                    channels.append({
                                        'day': current_day,
                                        'time': time,
                                        'teams': teams,
                                        'channel': channel,
                                        'url': stream_url,
                                    })
                                    break
    except Exception as e:
        logger.error(f"Failed to parse local file {filepath}: {e}")
    
    return channels


def format_tivimate_url(stream_url: str, headers: Dict[str, str]) -> str:
    """Format URL with headers for Tivimate using pipe format."""
    # Encode User-Agent for URL
    user_agent = headers.get('User-Agent', '')
    encoded_ua = quote(user_agent, safe='')
    
    # Build headers string
    header_parts = []
    if headers.get('Referer'):
        header_parts.append(f"Referer={headers['Referer']}")
    if headers.get('Origin'):
        header_parts.append(f"Origin={headers['Origin']}")
    if encoded_ua:
        header_parts.append(f"User-Agent={encoded_ua}")
    
    if header_parts:
        return f"{stream_url}|{'|'.join(header_parts)}"
    return stream_url


async def generate_m3u8(channels: List[Dict[str, Any]], output_file: str = "onlinesp_tivimate.m3u8"):
    """Generate M3U8 file with extracted stream URLs in Tivimate format."""
    extractor = SportsonlineExtractor()
    successful = 0
    
    try:
        with open(output_file, 'w', encoding='utf-8') as f:
            # Write M3U8 header
            f.write('#EXTM3U\n')
            f.write('#EXTINF:-1 tvg-id="" tvg-name="SPORTSONLINE" tvg-logo="" group-title="Sports", SportsOnline Channels\n\n')
            
            for idx, channel in enumerate(channels, 1):
                try:
                    logger.info(f"[{idx}/{len(channels)}] Extracting: {channel['teams']} ({channel['channel']})")
                    
                    result = await extractor.extract(channel['url'])
                    stream_url = result.get('destination_url')
                    headers = result.get('request_headers', {})
                    
                    if stream_url:
                        # Format channel name
                        channel_name = f"{channel['teams']} - {channel['channel']} ({channel['day']} {channel['time']})"
                        
                        # Format URL with headers for Tivimate
                        tivimate_url = format_tivimate_url(stream_url, headers)
                        
                        # Write M3U8 entry
                        f.write(f'#EXTINF:-1 tvg-id="" tvg-name="{channel_name}" tvg-logo="" group-title="Sports", {channel_name}\n')
                        f.write(f'{tivimate_url}\n\n')
                        
                        logger.info(f"✓ Success: {channel_name[:50]}...")
                        successful += 1
                    else:
                        logger.warning(f"✗ No stream URL: {channel['teams']}")
                        
                except Exception as e:
                    logger.error(f"✗ Failed: {channel['teams']} - {str(e)[:100]}")
                    continue
                    
            logger.info(f"✅ Complete: {successful}/{len(channels)} streams extracted")
            
    finally:
        await extractor.close()


async def main():
    """Main function to run the extractor."""
    print("=" * 60)
    print("SPORTSONLINE STREAM EXTRACTOR")
    print("Enhanced with Real Header Extraction")
    print("=" * 60)
    
    if not BROTLI_AVAILABLE:
        print("⚠️  Warning: brotli not installed - install with: pip install brotli")
    
    # Fetch channels
    channels = await fetch_prog_txt()
    
    if not channels:
        logger.warning("Could not fetch prog.txt from URL, trying local file...")
        channels = parse_prog_txt_from_file("prog.txt")
    
    if not channels:
        logger.error("No channels found. Please ensure prog.txt is accessible.")
        return
    
    # Remove duplicates (same event on same channel)
    unique_channels = []
    seen = set()
    for ch in channels:
        key = f"{ch['teams']}_{ch['channel']}"
        if key not in seen:
            seen.add(key)
            unique_channels.append(ch)
    
    logger.info(f"Found {len(unique_channels)} unique channels to process")
    
    # Generate M3U8
    await generate_m3u8(unique_channels, "onlinesp_tivimate.m3u8")
    
    print("\n" + "=" * 60)
    print("✅ EXTRACTION COMPLETE!")
    print(f"📁 Output: onlinesp_tivimate.m3u8")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
