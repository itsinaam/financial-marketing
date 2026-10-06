from app.models.knowledge_base import KnowledgeBaseSource
from app.schemas.companies import SignupRequest
from app.schemas.knowledge_base import KnowledgeBaseItemResponse
from app.schemas.auth import WebsiteScrapeStatusResponse
from app.services import knowledge_base_service


def test_signup_accepts_optional_website():
    payload = SignupRequest(
        full_name="Ayesha Khan",
        email="ayesha@example.com",
        password="StrongPass123!",
        confirm_password="StrongPass123!",
        website="https://example.com",
    )

    assert payload.website == "https://example.com"


def test_signup_allows_missing_website():
    payload = SignupRequest(
        full_name="Ayesha Khan",
        email="ayesha@example.com",
        password="StrongPass123!",
        confirm_password="StrongPass123!",
    )

    assert payload.website is None


def test_knowledge_item_has_download_url():
    payload = KnowledgeBaseItemResponse(
        id=1,
        company_id=2,
        source_type=KnowledgeBaseSource.UPLOAD,
        source_name="upload",
        source_url="https://example.com/download/test.pdf",
        title="Test upload",
        file_name="test.pdf",
        content="hello",
    )

    assert payload.source_url == "https://example.com/download/test.pdf"
    assert payload.download_url is None
    assert payload.content == "hello"


def test_store_scraped_site_uploads_file_and_sets_download_url(monkeypatch):
    class DummySession:
        def add(self, item):
            self.item = item

        def commit(self):
            pass

        def refresh(self, item):
            return None

    monkeypatch.setattr(
        knowledge_base_service,
        "scrape_website_content",
        lambda url: {
            "title": "Techfy",
            "content": "hello scraped text",
            "source_url": "https://techfy.io/",
        },
    )
    monkeypatch.setattr(
        knowledge_base_service,
        "upload_library_asset",
        lambda file_content, filename, content_type: "https://cdn.example.com/techfy.txt",
    )

    company = type("Company", (), {"id": 7})()
    db = DummySession()

    item = knowledge_base_service.store_scraped_site(db, company, "https://techfy.io/", "Techfy")

    assert item.source_url == "https://cdn.example.com/techfy.txt"
    assert item.content == "hello scraped text"
    assert "techfy.txt" in item.metadata_json


def test_website_scrape_status_response_has_progress_fields():
    payload = WebsiteScrapeStatusResponse(
        status="running",
        website="https://example.com",
        download_url="https://cdn.example.com/site.txt",
        message="Scraping website content",
    )

    assert payload.status == "running"
    assert payload.website == "https://example.com"
    assert payload.download_url == "https://cdn.example.com/site.txt"


def test_scrape_website_content_crawls_internal_pages(monkeypatch):
    class Response:
        def __init__(self, url, html):
            self.url = url
            self.text = html
            self.headers = {"Content-Type": "text/html; charset=utf-8"}

        def raise_for_status(self):
            pass

    pages = {
        "https://example.com/sitemap.xml": "<urlset></urlset>",
        "https://example.com/": '<title>Home</title><a href="/about">About</a><p>Home page content</p>',
        "https://example.com/about": '<title>About</title><p>About page content</p>',
    }

    def fake_get(url, **kwargs):
        return Response(url, pages[url])

    monkeypatch.setattr(knowledge_base_service.requests, "get", fake_get)

    result = knowledge_base_service.scrape_website_content("https://example.com/")

    assert "Home page content" in result["content"]
    assert "About page content" in result["content"]
    assert result["pages_scraped"] == 2
