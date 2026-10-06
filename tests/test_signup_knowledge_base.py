from app.models.knowledge_base import KnowledgeBaseSource
from app.schemas.companies import SignupRequest
from app.schemas.knowledge_base import KnowledgeBaseItemResponse
from app.schemas.auth import WebsiteScrapeStatusResponse
from app.services import knowledge_base_service
from app.services import storage_service
from app.api import themes as themes_service


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
        file_size=123,
        content="hello",
    )

    assert payload.source_url == "https://example.com/download/test.pdf"
    assert payload.download_url is None
    assert payload.file_size == 123


def test_store_scraped_site_uploads_file_and_sets_download_url(monkeypatch):
    class Query:
        def filter(self, *args):
            return self

        def all(self):
            return []

    class DummySession:
        def query(self, model):
            return Query()

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


def test_extract_text_decodes_utf16_without_bom():
    text = "Campaign message from uploaded file"

    assert knowledge_base_service.extract_text_from_uploaded_file("notes.txt", text.encode("utf-16-le")) == text


def test_scanned_pdf_uses_ocr_fallback(monkeypatch):
    from app.services import image_embed_service

    class FakeModels:
        def generate_content(self, **kwargs):
            return type("Response", (), {"text": "OCR extracted planner text"})()

    monkeypatch.setattr(
        image_embed_service,
        "get_genai_client",
        lambda: type("Client", (), {"models": FakeModels()})(),
    )

    assert knowledge_base_service.extract_text_from_uploaded_file("scan.pdf", b"not a readable PDF") == "OCR extracted planner text"


def test_store_scraped_site_reuses_existing_url(monkeypatch):
    existing = type(
        "KnowledgeItem",
        (),
        {"metadata_json": '{"url":"https://example.com/"}'},
    )()

    class Query:
        def filter(self, *args):
            return self

        def all(self):
            return [existing]

    class DummySession:
        def query(self, model):
            return Query()

    monkeypatch.setattr(
        knowledge_base_service,
        "scrape_website_content",
        lambda url: (_ for _ in ()).throw(AssertionError("unchanged URL was scraped again")),
    )

    reused = knowledge_base_service.store_scraped_site(
        DummySession(), type("Company", (), {"id": 7})(), "https://example.com"
    )

    assert reused is existing


def test_knowledge_context_does_not_limit_to_five_items():
    upload = type("Item", (), {"source_type": KnowledgeBaseSource.UPLOAD, "content": "uploaded reference stays", "title": "Old upload", "file_name": "old.txt", "source_name": "upload"})()
    websites = [
        type("Item", (), {"source_type": KnowledgeBaseSource.WEBSITE, "content": f"website page {index}", "title": f"Site {index}", "file_name": None, "source_name": "website"})()
        for index in range(8)
    ]

    class Query:
        def filter(self, *args):
            return self

        def order_by(self, *args):
            return self

        def all(self):
            return [upload, *websites]

    class DummySession:
        def query(self, model):
            return Query()

    context = knowledge_base_service.knowledge_base_context(DummySession(), 4)

    assert "Old upload: uploaded reference stays" in context
    assert "Site 7: website page 7" in context


def test_blog_generation_prompt_includes_knowledge_base(monkeypatch):
    from app.services import blog_generator_service

    captured = {}

    class FakeModels:
        def generate_content(self, **kwargs):
            captured["prompt"] = kwargs["contents"]
            return type("Response", (), {"text": '{"title":"Title","content":"<p>Body</p>","hashtags":"#brand"}'})()

    monkeypatch.setattr(
        blog_generator_service,
        "get_genai_client",
        lambda: type("Client", (), {"models": FakeModels()})(),
    )

    blog_generator_service.generate_blog_content(
        "Write a blog", knowledge_context="Uploaded product guide: recycled material"
    )

    assert "Uploaded product guide: recycled material" in captured["prompt"]


def test_delete_library_asset_removes_object_from_configured_bucket(monkeypatch):
    removed = {}

    class Bucket:
        def remove(self, paths):
            removed["paths"] = paths

    class Storage:
        def from_(self, bucket):
            removed["bucket"] = bucket
            return Bucket()

    monkeypatch.setattr(storage_service, "SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setattr(storage_service, "BUCKET_NAME", "products-images")
    monkeypatch.setattr(storage_service, "get_supabase_client", lambda: type("Client", (), {"storage": Storage()})())

    deleted = storage_service.delete_library_asset(
        "https://project.supabase.co/storage/v1/object/public/products-images/folder/my%20file.pdf"
    )

    assert deleted is True
    assert removed == {"bucket": "products-images", "paths": ["folder/my file.pdf"]}


def test_themes_website_sync_does_not_rescrape_unchanged_url(monkeypatch):
    scraped = []
    monkeypatch.setattr(themes_service, "store_scraped_site", lambda *args, **kwargs: scraped.append(args[2]))
    company = type("Company", (), {"name": "Acme", "email": "a@example.com", "website": "https://example.com"})()
    profile = type("Profile", (), {"company_website": "https://example.com/", "company_name": "Acme"})()

    themes_service._sync_company_website(None, company, profile, "https://example.com")

    assert scraped == []
    assert company.website == "https://example.com/"


def test_themes_website_sync_scrapes_when_url_changes(monkeypatch):
    scraped = []
    monkeypatch.setattr(themes_service, "store_scraped_site", lambda *args, **kwargs: scraped.append(args[2]))
    company = type("Company", (), {"name": "Acme", "email": "a@example.com", "website": "https://example.com"})()
    profile = type("Profile", (), {"company_website": "https://new.example.com", "company_name": "Acme"})()

    themes_service._sync_company_website(None, company, profile, "https://example.com")

    assert scraped == ["https://new.example.com"]
    assert company.website == "https://new.example.com"


def test_themes_profile_uses_signup_website_when_profile_url_is_empty(monkeypatch):
    company = type("Company", (), {"id": 3, "website": "https://signup.example.com"})()
    profile = type("Profile", (), {"company_id": 3, "company_website": None})()
    monkeypatch.setattr(themes_service, "resolve_company", lambda db, auth, company_id: company)
    monkeypatch.setattr(themes_service, "_get_or_create", lambda db, company_id: profile)

    response = themes_service.get_brand_profile(db=None, auth=None)

    assert response.company_website == "https://signup.example.com"
