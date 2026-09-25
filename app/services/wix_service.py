import json
from typing import Any, Dict, List, Optional
from html.parser import HTMLParser
import requests
from fastapi import HTTPException, status

# Wix REST API via a Site API key (same model as the WordPress/Ghost
# integrations). The key is sent raw in the `Authorization` header (no
# "Bearer" prefix) and every site-level call also requires the
# `wix-site-id` header, otherwise Wix returns 403.
# https://dev.wix.com/docs/rest/api-reference/auth/rest-api-authentication
WIX_API_BASE = "https://www.wixapis.com"

DOMAINS_ACTION = {"GET": ["site-read"], "POST": ["blog-write"], "READ": ["blog-read"], "WRITE": ["blog-write"]}


class _RicosHTMLParser(HTMLParser):
    """
    Minimal HTML -> Wix Ricos NodeTree converter. Wix has no official Python
    converter, so only the tag subset the blog generator emits is handled:
    <p>, <h2>, <h3>, <ul>, <li>, <strong>, <em>. Anything else is dropped.
    """

    INLINE_TAGS = {"strong", "em"}
    BLOCK_TAGS = {"p", "h2", "h3", "ul", "li"}

    def __init__(self) -> None:
        super().__init__()
        self.nodes: List[Dict[str, Any]] = []
        self._counter = 0
        self._block_stack: List[Dict[str, Any]] = []
        self._active_bold = False
        self._active_italic = False

    def _new_id(self) -> str:
        self._counter += 1
        return f"_id_{self._counter}"

    def _current_block(self) -> Optional[Dict[str, Any]]:
        return self._block_stack[-1] if self._block_stack else None

    def _push_block(self, node: Dict[str, Any], nested: bool = False) -> None:
        if nested:
            parent = self._current_block()
            if parent is not None:
                parent["nodes"].append(node)
        else:
            self.nodes.append(node)
        self._block_stack.append(node)

    def _pop_block(self) -> None:
        if self._block_stack:
            self._block_stack.pop()

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in ("p", "h2", "h3"):
            node: Dict[str, Any] = {
                "type": "PARAGRAPH" if tag == "p" else "HEADING",
                "id": self._new_id(),
                "nodes": [],
            }
            if tag in ("h2", "h3"):
                node["headingData"] = {"level": 2 if tag == "h2" else 3}
            self._push_block(node)
        elif tag == "ul":
            self._push_block({"type": "BULLETED_LIST", "id": self._new_id(), "nodes": []})
        elif tag == "li":
            self._push_block(
                {"type": "LIST_ITEM", "id": self._new_id(), "nodes": [], "textData": {}},
                nested=True,
            )
        elif tag == "strong":
            self._active_bold = True
        elif tag == "em":
            self._active_italic = True

    def handle_endtag(self, tag: str) -> None:
        if tag in self.BLOCK_TAGS:
            self._pop_block()
        elif tag == "strong":
            self._active_bold = False
        elif tag == "em":
            self._active_italic = False

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if not text:
            return
        block = self._current_block()
        if block is None:
            return
        decoration: Dict[str, Any] = {}
        if self._active_bold:
            decoration["fontWeight"] = "700"
        if self._active_italic:
            decoration["fontStyle"] = "italic"
        block["nodes"].append(
            {"type": "TEXT", "id": self._new_id(), "text": text, "decoration": decoration}
        )

    def to_rich_content(self) -> Dict[str, Any]:
        # Remove empty paragraphs/lists to keep the payload clean.
        base_nodes = [n for n in self.nodes if n.get("nodes")]
        return {"nodes": base_nodes, "metadata": {}}


class WixService:
    """
    Publishes to a Wix blog via the Wix REST API (v3 blog endpoints) using a
    Wix Site API key plus the site id. The API key is sent raw in the
    `Authorization` header and every call also includes the `wix-site-id`
    header, which lets Wix resolve the target blog instance. This mirrors the
    WordPress / Ghost API-key integrations and avoids the whole OAuth
    app-instance flow, which the Blog REST API does not support (it returns
    "UNAUTHENTICATED: No blog instanceId found" for app tokens).
    The member id is optional for draft creation but is resolved when the post
    author is required; if it can't be resolved the call proceeds without it.
    """

    @staticmethod
    def _request(
        method: str,
        url: str,
        api_key: str,
        site_id: Optional[str] = None,
        **kwargs: Any,
    ) -> requests.Response:
        headers = {
            "Authorization": api_key,
            "Content-Type": "application/json",
        }
        if site_id:
            headers["wix-site-id"] = site_id
        try:
            return requests.request(method, url, headers=headers, timeout=30, **kwargs)
        except requests.RequestException as e:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to connect to Wix API: {str(e)}",
            )

    @staticmethod
    def test_connection(api_key: str, site_id: str) -> Dict[str, Any]:
        """
        Validate an API key/site pair against the Wix REST API. Listing the
        site's draft posts is a cheap site-level call that also confirms the
        site actually has a Wix Blog instance installed (the classic
        "No blog instanceId found" failure surfaces here instead of at publish
        time).
        """
        res = WixService._request(
            "GET",
            f"{WIX_API_BASE}/blog/v3/draft-posts?paging.limit=1",
            api_key,
            site_id=site_id,
        )
        if res.status_code not in (200, 201):
            detail = res.text[:300]
            hint = ""
            if "No blog instanceId found" in detail.lower() or "blog" in detail.lower():
                hint = (
                    " The Wix Blog app must be installed (and have at least one post) on the site "
                    "before connecting. Add the Blog app in your Wix editor, create a test post, then reconnect."
                )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Wix connection failed ({res.status_code}): {detail}. "
                    "Check that the API key has Wix Blog permissions, is scoped to this site, "
                    f"and was created by the site owner. Verify the Site ID{hint}"
                ),
            )
        return {"success": True, "site_id": site_id}

    @staticmethod
    def get_member_id(api_key: str, site_id: str) -> Optional[str]:
        """
        Best-effort resolution of a member id to use as the post author. The
        blog v3 API can create drafts without an explicit member id (it
        defaults to the site owner), so this is optional.
        """
        try:
            res = WixService._request(
                "GET",
                f"{WIX_API_BASE}/blog/v3/draft-posts?paging.limit=1",
                api_key,
                site_id=site_id,
            )
            if res.ok:
                data = res.json() or {}
                draft_post = (data.get("draftPosts") or [{}])[0]
                if draft_post.get("memberId"):
                    return draft_post["memberId"]
        except Exception:
            pass

        try:
            me_res = WixService._request(
                "GET",
                f"{WIX_API_BASE}/members/v1/members/me",
                api_key,
                site_id=site_id,
            )
            if me_res.ok:
                member = (me_res.json() or {}).get("member") or {}
                if member.get("id"):
                    return member["id"]
        except Exception:
            pass

        return None

    @staticmethod
    def create_draft(
        api_key: str,
        site_id: str,
        title: str,
        content: Dict[str, Any],
        member_id: Optional[str] = None,
    ) -> str:
        """Create a draft post and return its draft post id."""
        draft_post: Dict[str, Any] = {
            "title": title,
            "originalRichContent": json.dumps(content),
        }
        if member_id:
            draft_post["memberId"] = member_id
        res = WixService._request(
            "POST",
            f"{WIX_API_BASE}/blog/v3/draft-posts",
            api_key,
            site_id=site_id,
            json={"draftPost": draft_post},
        )
        if res.status_code not in (200, 201):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Wix draft post failed ({res.status_code}): {res.text[:300]}",
            )
        data = res.json() or {}
        draft_post_out = data.get("draftPost") or {}
        draft_id = draft_post_out.get("id")
        if not draft_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Wix draft post created without an id.",
            )
        return draft_id

    @staticmethod
    def publish_draft(api_key: str, site_id: str, draft_id: str) -> Dict[str, Any]:
        """Publish a previously created draft post."""
        res = WixService._request(
            "POST",
            f"{WIX_API_BASE}/blog/v3/draft-posts/{draft_id}/publish",
            api_key,
            site_id=site_id,
            json={},
        )
        if res.status_code not in (200, 201):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Wix publish failed ({res.status_code}): {res.text[:300]}",
            )
        data = res.json() or {}
        post = data.get("draftPost") or data.get("post") or {}
        return {
            "success": True,
            "draft_post_id": draft_id,
            "target": "Wix",
            "link": post.get("url"),
        }

    @staticmethod
    def create_post(
        api_key: str,
        site_id: str,
        title: str,
        html_content: str,
        member_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a draft post from HTML, then publish it. Returns the post link."""
        parser = _RicosHTMLParser()
        try:
            parser.feed(html_content)
            parser.close()
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Could not convert blog HTML to Wix Ricos format: {str(exc)}",
            )
        rich_content = parser.to_rich_content()

        if not member_id:
            member_id = WixService.get_member_id(api_key, site_id)

        draft_id = WixService.create_draft(
            api_key=api_key,
            site_id=site_id,
            title=title,
            content=rich_content,
            member_id=member_id,
        )
        return WixService.publish_draft(
            api_key=api_key,
            site_id=site_id,
            draft_id=draft_id,
        )
