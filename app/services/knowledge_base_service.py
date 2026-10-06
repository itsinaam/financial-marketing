from __future__ import annotations

import json
import logging
import re
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader
from sqlalchemy.orm import Session

from app.models.companies import Company
from app.models.knowledge_base import KnowledgeBaseItem, KnowledgeBaseSource
from app.services.storage_service import delete_library_asset, upload_library_asset

logger = logging.getLogger("KnowledgeBaseService")

MAX_CRAWL_PAGES = 30
MAX_CRAWL_CHARS = 200000
SKIPPED_FILE_SUFFIXES = {
    ".avif", ".css", ".gif", ".ico", ".jpeg", ".jpg", ".js", ".png",
    ".svg", ".webp", ".woff", ".woff2", ".zip", ".mp4", ".mp3", ".pdf",
}


def normalize_url(raw_url: str) -> str:
    value = (raw_url or "").strip()
    if not value:
        raise ValueError("Website URL is required")
    if not value.startswith(("http://", "https://")):
        value = "https://" + value
    return value


def _extract_text_from_html(html_text: str) -> str:
    soup = BeautifulSoup(html_text, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()

    text = soup.get_text("\n", strip=True)
    text = unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return text.strip()


def _normalized_crawl_url(raw_url: str, base_url: str) -> str | None:
    absolute_url = urljoin(base_url, raw_url.strip())
    absolute_url, _ = urldefrag(absolute_url)
    parsed = urlsplit(absolute_url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    normalized_path = parsed.path or "/"
    if normalized_path != "/":
        normalized_path = normalized_path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), normalized_path, "", ""))


def _same_site(candidate_url: str, root_url: str) -> bool:
    candidate_host = (urlsplit(candidate_url).hostname or "").lower().removeprefix("www.")
    root_host = (urlsplit(root_url).hostname or "").lower().removeprefix("www.")
    return bool(candidate_host and candidate_host == root_host)


def website_urls_match(first_url: str | None, second_url: str | None) -> bool:
    if not first_url or not second_url:
        return False
    try:
        first = normalize_url(first_url)
        second = normalize_url(second_url)
        first_parts = urlsplit(_normalized_crawl_url(first, first) or first)
        second_parts = urlsplit(_normalized_crawl_url(second, second) or second)
        first_host = (first_parts.hostname or "").lower().removeprefix("www.")
        second_host = (second_parts.hostname or "").lower().removeprefix("www.")
        return (
            first_parts.scheme == second_parts.scheme
            and first_host == second_host
            and first_parts.path == second_parts.path
        )
    except ValueError:
        return first_url.strip().rstrip("/").lower() == second_url.strip().rstrip("/").lower()


def _sitemap_urls(root_url: str, headers: dict[str, str]) -> list[str]:
    sitemap_queue = [urljoin(root_url, "/sitemap.xml")]
    seen_sitemaps: set[str] = set()
    page_urls: list[str] = []

    while sitemap_queue and len(seen_sitemaps) < 3 and len(page_urls) < 500:
        sitemap_url = sitemap_queue.pop(0)
        if sitemap_url in seen_sitemaps or not _same_site(sitemap_url, root_url):
            continue
        seen_sitemaps.add(sitemap_url)
        try:
            response = requests.get(sitemap_url, headers=headers, timeout=15)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            for location in soup.find_all("loc"):
                url = _normalized_crawl_url(location.get_text(strip=True), root_url)
                if not url or not _same_site(url, root_url):
                    continue
                if urlsplit(url).path.lower().endswith(".xml"):
                    sitemap_queue.append(url)
                else:
                    page_urls.append(url)
                    if len(page_urls) >= 500:
                        break
        except requests.RequestException:
            continue

    return list(dict.fromkeys(page_urls))


def scrape_website_content(url: str) -> dict[str, Any]:
    root_url = _normalized_crawl_url(normalize_url(url), normalize_url(url))
    if not root_url:
        raise ValueError("A valid HTTP or HTTPS website URL is required.")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    sitemap_pages = _sitemap_urls(root_url, headers)
    pending_urls = list(dict.fromkeys([root_url, *sitemap_pages]))
    seen_urls: set[str] = set()
    pages: list[tuple[str, str, str]] = []

    while pending_urls and len(pages) < MAX_CRAWL_PAGES:
        page_url = pending_urls.pop(0)
        if page_url in seen_urls or not _same_site(page_url, root_url):
            continue
        if Path(urlsplit(page_url).path).suffix.lower() in SKIPPED_FILE_SUFFIXES:
            continue
        seen_urls.add(page_url)

        try:
            response = requests.get(page_url, headers=headers, timeout=20)
            response.raise_for_status()
        except requests.RequestException:
            continue

        response_url = _normalized_crawl_url(getattr(response, "url", page_url), page_url)
        if not response_url or not _same_site(response_url, root_url):
            continue
        content_type = (response.headers.get("Content-Type") or "").lower()
        if "html" not in content_type and "xml" not in content_type:
            continue

        html_text = response.text
        soup = BeautifulSoup(html_text, "html.parser")
        page_title = soup.title.get_text(" ", strip=True) if soup.title else response_url
        extracted = _extract_text_from_html(html_text)
        if extracted:
            pages.append((response_url, page_title, extracted))

        for anchor in soup.find_all("a", href=True):
            candidate = _normalized_crawl_url(anchor["href"], response_url)
            if (
                candidate
                and _same_site(candidate, root_url)
                and candidate not in seen_urls
                and candidate not in pending_urls
            ):
                pending_urls.append(candidate)

    if not pages:
        raise ValueError("No readable website pages were found.")

    content_parts = [f"PAGE: {title}\nURL: {page_url}\n{text}" for page_url, title, text in pages]
    content = "\n\n---\n\n".join(content_parts)[:MAX_CRAWL_CHARS]
    return {
        "title": pages[0][1] or Path(root_url).name,
        "content": content,
        "source_url": root_url,
        "pages_scraped": len(pages),
        "page_urls": [page_url for page_url, _, _ in pages],
    }


def store_scraped_site(db: Session, company: Company, url: str, title: str | None = None) -> KnowledgeBaseItem:
    for existing in (
        db.query(KnowledgeBaseItem)
        .filter(
            KnowledgeBaseItem.company_id == company.id,
            KnowledgeBaseItem.source_type == KnowledgeBaseSource.WEBSITE,
        )
        .all()
    ):
        try:
            stored_url = json.loads(existing.metadata_json or "{}").get("url")
        except (TypeError, json.JSONDecodeError):
            stored_url = None
        if stored_url and website_urls_match(stored_url, url):
            return existing

    scraped = scrape_website_content(url)
    content_text = scraped["content"]
    safe_name = re.sub(r"[^a-zA-Z0-9_-]+", "-", (title or scraped.get("title") or "website"))
    file_name = f"{safe_name[:60] or 'website'}-{company.id}.txt"

    try:
        public_url = upload_library_asset(
            file_content=content_text.encode("utf-8"),
            filename=file_name,
            content_type="text/plain; charset=utf-8",
        )
    except Exception:
        public_url = None

    item = KnowledgeBaseItem(
        company_id=company.id,
        source_type=KnowledgeBaseSource.WEBSITE,
        source_name="website",
        source_url=public_url or scraped["source_url"],
        title=(title or scraped.get("title") or "Website knowledge"),
        file_name=file_name if public_url else None,
        file_size=len(content_text.encode("utf-8")) if public_url else None,
        content=content_text,
        metadata_json=json.dumps({
            "url": scraped["source_url"],
            "download_url": public_url,
            "pages_scraped": scraped.get("pages_scraped", 1),
            "page_urls": scraped.get("page_urls", [scraped["source_url"]]),
        }),
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def extract_text_from_uploaded_file(filename: str, payload: bytes) -> str:
    suffix = Path(filename or "").suffix.lower()

    if suffix == ".pdf":
        extracted_text = ""
        try:
            from io import BytesIO

            reader = PdfReader(BytesIO(payload))
            pages = [page.extract_text() or "" for page in reader.pages]
            extracted_text = "\n\n".join(pages).strip()
        except Exception as exc:
            logger.warning("Could not extract selectable text from uploaded PDF: %s", exc)
        if extracted_text.strip():
            return extracted_text

        try:
            from google.genai import types
            from app.services.image_embed_service import get_genai_client

            response = get_genai_client().models.generate_content(
                model="gemini-3.6-flash",
                contents=[
                    "Transcribe all readable text from this scanned PDF. Preserve headings and line breaks. Return only the extracted text.",
                    types.Part.from_bytes(data=payload, mime_type="application/pdf"),
                ],
            )
            return (response.text or "").strip()
        except Exception as exc:
            raise ValueError("No readable PDF text could be extracted; scanned PDF OCR failed.") from exc

    if suffix in {".txt", ".md", ".csv", ".json", ".html", ".htm", ".xml", ".yaml", ".yml", ".ini"}:
        if b"\x00" in payload:
            even_nulls = payload[::2].count(0)
            odd_nulls = payload[1::2].count(0)
            utf16_encoding = "utf-16-be" if even_nulls > odd_nulls else "utf-16-le"
            encodings = ("utf-16", utf16_encoding, "utf-8-sig", "cp1252", "latin-1")
        else:
            encodings = ("utf-8-sig", "utf-16", "cp1252", "latin-1")
        for encoding in encodings:
            try:
                return payload.decode(encoding).strip("\x00")
            except UnicodeDecodeError:
                continue

    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        return payload.decode("latin-1", errors="ignore")


def knowledge_base_context(db: Session, company_id: int, max_chars: int = 8000) -> str:
    from sqlalchemy import case

    items = db.query(KnowledgeBaseItem).filter(
        KnowledgeBaseItem.company_id == company_id
    ).order_by(
        case((KnowledgeBaseItem.source_type == KnowledgeBaseSource.UPLOAD, 0), else_=1),
        KnowledgeBaseItem.created_at.desc(),
    ).all()
    if not items:
        return ""

    blocks = []
    remaining = max_chars
    for item in items:
        if remaining <= 0:
            break
        clean_text = re.sub(r"\s+", " ", (item.content or "")).strip()
        if not clean_text:
            continue
        summary = clean_text[: min(1500, remaining)]
        if summary:
            blocks.append(f"- {item.title or item.file_name or item.source_name or 'Knowledge'}: {summary}")
            remaining -= len(summary) + 4

    if not blocks:
        return ""
    return "KNOWLEDGE BASE REFERENCES:\n" + "\n".join(blocks)


def delete_knowledge_base_asset(item: KnowledgeBaseItem) -> None:
    """Remove the stored object for a knowledge-base item, if it has one."""
    download_url = item.source_url
    try:
        metadata = json.loads(item.metadata_json or "{}")
        download_url = metadata.get("download_url") or download_url
    except (TypeError, json.JSONDecodeError):
        pass
    if download_url:
        delete_library_asset(download_url)
