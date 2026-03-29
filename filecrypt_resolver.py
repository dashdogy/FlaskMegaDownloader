from __future__ import annotations

import http.cookiejar
import re
import urllib.request
from dataclasses import dataclass
from html import unescape
from urllib.parse import urljoin, urlsplit


FILECRYPT_HOSTS = {"filecrypt.cc", "www.filecrypt.cc"}
MEGA_HOSTS = {"mega.nz", "mega.co.nz", "www.mega.nz", "www.mega.co.nz"}
REQUEST_TIMEOUT_SECONDS = 20
FILECRYPT_CONTAINER_RE = re.compile(r"^/Container/[^/?#]+\.html$", re.IGNORECASE)
FILECRYPT_DLC_RE = re.compile(r"^/DLC/[^/?#]+\.dlc$", re.IGNORECASE)
MEGA_MIRROR_RE = re.compile(
    r'<a[^>]+href="(?P<href>[^"]*/Container/[^"]+)"[^>]*>\s*(?P<label>mega\.nz|mega\.co\.nz)\s*</a>',
    re.IGNORECASE,
)
DOWNLOAD_ID_RE = re.compile(
    r"<button\b[^>]*>",
    re.IGNORECASE,
)
BUTTON_DOWNLOAD_CLASS_RE = re.compile(r'class="download"', re.IGNORECASE)
BUTTON_DOWNLOAD_ID_RE = re.compile(r'data-[^=]+="([A-F0-9]{10})"', re.IGNORECASE)
TABLE_ROW_RE = re.compile(r"<tr\b[^>]*>(?P<row>.*?)</tr>", re.IGNORECASE | re.DOTALL)
TABLE_CELL_RE = re.compile(r"<td\b[^>]*>(?P<cell>.*?)</td>", re.IGNORECASE | re.DOTALL)
TITLE_ATTR_RE = re.compile(r'<td[^>]*title="(?P<title>[^"]+)"', re.IGNORECASE)
GO_REDIRECT_RE = re.compile(r"top\.location\.href='(?P<url>[^']+)'", re.IGNORECASE)
MEGA_DIRECT_URL_RE = re.compile(
    r"https://(?:www\.)?mega\.(?:nz|co\.nz)/(?:file|folder)/[^\s\"'<>]+",
    re.IGNORECASE,
)
SIZE_TEXT_RE = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>[KMGTP]?i?B)", re.IGNORECASE)
TRAILING_MEGA_LABEL_RE = re.compile(r"\s*mega\.(?:co\.)?nz\s*$", re.IGNORECASE)


class FilecryptResolutionError(ValueError):
    pass


@dataclass(slots=True)
class FilecryptResolutionSummary:
    containers_resolved: int = 0
    mega_links_resolved: int = 0


@dataclass(slots=True)
class ResolvedMegaLink:
    url: str
    display_name: str | None = None
    bytes_total: int | None = None


class _FilecryptSession:
    def __init__(self) -> None:
        cookie_jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))

    def get_text(self, url: str, *, referer: str | None = None) -> tuple[str, str]:
        request = urllib.request.Request(url, headers=self._headers(referer, accept="text/html,application/xhtml+xml"))
        with self._opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return response.read().decode("utf-8", errors="replace"), response.geturl()

    def get_final_url(self, url: str, *, referer: str | None = None) -> tuple[str, str]:
        request = urllib.request.Request(url, headers=self._headers(referer, accept="text/html,application/xhtml+xml"))
        with self._opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8", errors="replace")
            return response.geturl(), body

    def _headers(self, referer: str | None, *, accept: str) -> dict[str, str]:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            ),
            "Accept": accept,
            "Accept-Language": "en-US,en;q=0.9",
        }
        if referer:
            headers["Referer"] = referer
        return headers


def is_filecrypt_url(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.scheme in {"http", "https"} and parsed.netloc.lower() in FILECRYPT_HOSTS


def expand_submission_urls(
    urls: list[str],
    *,
    session_factory: type[_FilecryptSession] = _FilecryptSession,
) -> tuple[list[str], FilecryptResolutionSummary]:
    expanded_urls, summary, _ = expand_submission_urls_with_metadata(urls, session_factory=session_factory)
    return expanded_urls, summary


def expand_submission_urls_with_metadata(
    urls: list[str],
    *,
    session_factory: type[_FilecryptSession] = _FilecryptSession,
) -> tuple[list[str], FilecryptResolutionSummary, dict[str, dict[str, int | str | None]]]:
    summary = FilecryptResolutionSummary()
    expanded_urls: list[str] = []
    seen: set[str] = set()
    metadata_by_url: dict[str, dict[str, int | str | None]] = {}

    for raw_url in urls:
        if not is_filecrypt_url(raw_url):
            if raw_url not in seen:
                seen.add(raw_url)
                expanded_urls.append(raw_url)
            continue

        resolved_links = resolve_filecrypt_links(raw_url, session_factory=session_factory)
        summary.containers_resolved += 1
        summary.mega_links_resolved += len(resolved_links)
        for resolved_link in resolved_links:
            if resolved_link.url not in metadata_by_url:
                metadata_by_url[resolved_link.url] = {
                    "display_name": resolved_link.display_name,
                    "bytes_total": resolved_link.bytes_total,
                }
            else:
                existing = metadata_by_url[resolved_link.url]
                if not existing.get("display_name") and resolved_link.display_name:
                    existing["display_name"] = resolved_link.display_name
                if existing.get("bytes_total") is None and resolved_link.bytes_total is not None:
                    existing["bytes_total"] = resolved_link.bytes_total
            if resolved_link.url in seen:
                continue
            seen.add(resolved_link.url)
            expanded_urls.append(resolved_link.url)

    return expanded_urls, summary, metadata_by_url


def resolve_filecrypt_url(
    url: str,
    *,
    session_factory: type[_FilecryptSession] = _FilecryptSession,
) -> list[str]:
    return [item.url for item in resolve_filecrypt_links(url, session_factory=session_factory)]


def resolve_filecrypt_links(
    url: str,
    *,
    session_factory: type[_FilecryptSession] = _FilecryptSession,
) -> list[ResolvedMegaLink]:
    parsed = urlsplit(url)
    if parsed.netloc.lower() not in FILECRYPT_HOSTS:
        raise FilecryptResolutionError("Unsupported Filecrypt host. Paste a public filecrypt.cc container URL.")
    if FILECRYPT_DLC_RE.match(parsed.path):
        raise FilecryptResolutionError(
            "Direct Filecrypt DLC URLs are not supported by this local resolver yet. Paste the Filecrypt container URL instead."
        )
    if not FILECRYPT_CONTAINER_RE.match(parsed.path):
        raise FilecryptResolutionError(
            "Unsupported Filecrypt URL. Paste a public Filecrypt container URL such as /Container/... ."
        )

    session = session_factory()
    container_html, container_url = session.get_text(url)
    download_ids = _extract_download_ids(container_html)
    row_metadata = _extract_row_metadata(container_html)
    mega_mirror_url = _select_mega_mirror_url(container_html, container_url)
    should_fetch_mirror = not download_ids or _has_missing_row_metadata(download_ids, row_metadata)
    if should_fetch_mirror and mega_mirror_url and _normalize_url(mega_mirror_url) != _normalize_url(container_url):
        mirror_html, mirror_url = session.get_text(mega_mirror_url, referer=container_url)
        mirror_download_ids = _extract_download_ids(mirror_html)
        mirror_row_metadata = _extract_row_metadata(mirror_html)
        if mirror_download_ids:
            download_ids = mirror_download_ids
        row_metadata = _merge_row_metadata(row_metadata, mirror_row_metadata)
        container_html, container_url = mirror_html, mirror_url
    if not download_ids:
        raise FilecryptResolutionError("Filecrypt did not expose any downloadable entries for the public MEGA mirror.")

    resolved_links: list[ResolvedMegaLink] = []
    seen: set[str] = set()
    for download_id in download_ids:
        link_url = urljoin(container_url, f"/Link/{download_id}.html")
        link_html, link_page_url = session.get_text(link_url, referer=container_url)
        go_url = _extract_go_url(link_html, link_page_url)
        final_url, final_body = session.get_final_url(go_url, referer=link_page_url)
        mega_url = _extract_final_mega_url(final_url, final_body)
        if mega_url in seen:
            continue
        seen.add(mega_url)
        entry_metadata = row_metadata.get(download_id, {})
        resolved_links.append(
            ResolvedMegaLink(
                url=mega_url,
                display_name=entry_metadata.get("display_name"),
                bytes_total=entry_metadata.get("bytes_total"),
            )
        )

    if not resolved_links:
        raise FilecryptResolutionError("Filecrypt resolved, but no MEGA links were found in the public container.")
    return resolved_links


def _normalize_url(url: str) -> str:
    parsed = urlsplit(url)
    query = "&".join(part for part in parsed.query.split("&") if part)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}?{query}" if query else f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _select_mega_mirror_url(container_html: str, container_url: str) -> str | None:
    for match in MEGA_MIRROR_RE.finditer(container_html):
        label = match.group("label").strip().lower()
        if label not in {"mega.nz", "mega.co.nz"}:
            continue
        return urljoin(container_url, unescape(match.group("href")))
    return None


def _extract_download_ids(container_html: str) -> list[str]:
    found_ids: list[str] = []
    seen: set[str] = set()
    for match in DOWNLOAD_ID_RE.finditer(container_html):
        button_tag = match.group(0)
        if not BUTTON_DOWNLOAD_CLASS_RE.search(button_tag):
            continue
        id_match = BUTTON_DOWNLOAD_ID_RE.search(button_tag)
        if not id_match:
            continue
        download_id = id_match.group(1)
        if download_id in seen:
            continue
        seen.add(download_id)
        found_ids.append(download_id)
    return found_ids


def _extract_row_metadata(container_html: str) -> dict[str, dict[str, int | str | None]]:
    metadata: dict[str, dict[str, int | str | None]] = {}
    for row_match in TABLE_ROW_RE.finditer(container_html):
        row_html = row_match.group("row")
        if not BUTTON_DOWNLOAD_CLASS_RE.search(row_html):
            continue
        id_match = BUTTON_DOWNLOAD_ID_RE.search(row_html)
        if not id_match:
            continue
        cells = TABLE_CELL_RE.findall(row_html)
        title_match = TITLE_ATTR_RE.search(row_html)
        display_name = unescape(title_match.group("title")).strip() if title_match else None
        if not display_name and len(cells) >= 2:
            display_name = _infer_display_name_from_cell(cells[1])
        size_text = _normalize_cell_text(cells[2]) if len(cells) >= 3 else ""
        download_id = id_match.group(1)
        entry = metadata.setdefault(download_id, {})
        if display_name:
            entry["display_name"] = display_name
        bytes_total = _parse_text_size_bytes(size_text)
        if bytes_total is not None:
            entry["bytes_total"] = bytes_total
        metadata[download_id] = {
            "display_name": entry.get("display_name"),
            "bytes_total": entry.get("bytes_total"),
        }
    return metadata


def _has_missing_row_metadata(
    download_ids: list[str],
    row_metadata: dict[str, dict[str, int | str | None]],
) -> bool:
    if not row_metadata:
        return True
    for download_id in download_ids:
        entry = row_metadata.get(download_id)
        if not entry or not entry.get("display_name"):
            return True
    return False


def _merge_row_metadata(
    base_metadata: dict[str, dict[str, int | str | None]],
    override_metadata: dict[str, dict[str, int | str | None]],
) -> dict[str, dict[str, int | str | None]]:
    merged: dict[str, dict[str, int | str | None]] = {
        key: dict(value) for key, value in base_metadata.items()
    }
    for key, value in override_metadata.items():
        existing = merged.setdefault(key, {})
        if value.get("display_name"):
            existing["display_name"] = value["display_name"]
        if value.get("bytes_total") is not None:
            existing["bytes_total"] = value["bytes_total"]
    return merged


def _extract_go_url(link_html: str, link_url: str) -> str:
    match = GO_REDIRECT_RE.search(link_html)
    if not match:
        raise FilecryptResolutionError(f"Could not follow Filecrypt link redirect for {link_url}.")
    return urljoin(link_url, unescape(match.group("url")))


def _extract_final_mega_url(final_url: str, response_body: str) -> str:
    if _is_mega_url(final_url):
        return final_url

    for candidate in MEGA_DIRECT_URL_RE.findall(response_body):
        if _is_mega_url(candidate):
            return candidate

    raise FilecryptResolutionError("Filecrypt redirected successfully, but did not yield a usable MEGA URL.")


def _is_mega_url(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.scheme in {"http", "https"} and parsed.netloc.lower() in MEGA_HOSTS


def _normalize_cell_text(raw_html: str) -> str:
    cleaned = re.sub(r"<[^>]+>", "", raw_html or "")
    return unescape(cleaned).strip()


def _infer_display_name_from_cell(raw_html: str) -> str | None:
    text = _normalize_cell_text(raw_html)
    if not text:
        return None
    cleaned = TRAILING_MEGA_LABEL_RE.sub("", text).strip()
    return cleaned or None


def _parse_text_size_bytes(value: str | None) -> int | None:
    if not value:
        return None
    match = SIZE_TEXT_RE.search(value)
    if not match:
        return None
    number = float(match.group("value"))
    unit = match.group("unit").lower()
    scale = {
        "b": 1,
        "kb": 1000,
        "mb": 1000**2,
        "gb": 1000**3,
        "tb": 1000**4,
        "kib": 1024,
        "mib": 1024**2,
        "gib": 1024**3,
        "tib": 1024**4,
    }
    factor = scale.get(unit)
    if factor is None:
        return None
    return int(number * factor)
