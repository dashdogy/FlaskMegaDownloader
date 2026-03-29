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
GO_REDIRECT_RE = re.compile(r"top\.location\.href='(?P<url>[^']+)'", re.IGNORECASE)
MEGA_DIRECT_URL_RE = re.compile(
    r"https://(?:www\.)?mega\.(?:nz|co\.nz)/(?:file|folder)/[^\s\"'<>]+",
    re.IGNORECASE,
)


class FilecryptResolutionError(ValueError):
    pass


@dataclass(slots=True)
class FilecryptResolutionSummary:
    containers_resolved: int = 0
    mega_links_resolved: int = 0


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
    summary = FilecryptResolutionSummary()
    expanded_urls: list[str] = []
    seen: set[str] = set()

    for raw_url in urls:
        if not is_filecrypt_url(raw_url):
            if raw_url not in seen:
                seen.add(raw_url)
                expanded_urls.append(raw_url)
            continue

        resolved_urls = resolve_filecrypt_url(raw_url, session_factory=session_factory)
        summary.containers_resolved += 1
        summary.mega_links_resolved += len(resolved_urls)
        for resolved_url in resolved_urls:
            if resolved_url in seen:
                continue
            seen.add(resolved_url)
            expanded_urls.append(resolved_url)

    return expanded_urls, summary


def resolve_filecrypt_url(
    url: str,
    *,
    session_factory: type[_FilecryptSession] = _FilecryptSession,
) -> list[str]:
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
    if not download_ids:
        mega_mirror_url = _select_mega_mirror_url(container_html, container_url)
        if mega_mirror_url and _normalize_url(mega_mirror_url) != _normalize_url(container_url):
            container_html, container_url = session.get_text(mega_mirror_url, referer=container_url)
            download_ids = _extract_download_ids(container_html)
    if not download_ids:
        raise FilecryptResolutionError("Filecrypt did not expose any downloadable entries for the public MEGA mirror.")

    resolved_urls: list[str] = []
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
        resolved_urls.append(mega_url)

    if not resolved_urls:
        raise FilecryptResolutionError("Filecrypt resolved, but no MEGA links were found in the public container.")
    return resolved_urls


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
