from typing import Any, Dict, List, Optional
import requests
from fastapi import HTTPException, status


class WordPressService:
    """
    Publishes to a self-hosted or WordPress.com site via the WordPress REST API,
    authenticated with an Application Password (Users > Profile > Application
    Passwords in wp-admin). No OAuth dance required.
    """

    @staticmethod
    def _request(
        method: str,
        url: str,
        username: str,
        app_password: str,
        **kwargs: Any,
    ) -> requests.Response:
        try:
            return requests.request(
                method,
                url,
                auth=(username, app_password),
                timeout=30,
                **kwargs,
            )
        except requests.RequestException as e:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to connect to the WordPress site: {str(e)}",
            )

    @staticmethod
    def _base_url(site_url: str) -> str:
        return site_url.rstrip("/")

    @staticmethod
    def test_connection(site_url: str, username: str, app_password: str) -> Dict[str, Any]:
        """
        Validate WordPress credentials by hitting /wp-json/wp/v2/users/me.
        Returns the connected user's WordPress display name.
        """
        base = WordPressService._base_url(site_url)
        res = WordPressService._request(
            "GET",
            f"{base}/wp-json/wp/v2/users/me",
            username,
            app_password,
        )
        if not res.ok:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"WordPress connection failed ({res.status_code}): {res.text[:300]}",
            )
        data = res.json()
        return {
            "success": True,
            "user": data.get("name") or data.get("slug") or username,
        }

    @staticmethod
    def _resolve_tags(
        base: str,
        username: str,
        app_password: str,
        tags: List[str],
    ) -> List[int]:
        """Look up existing WordPress tags by name, creating any that are missing."""
        resolved: List[int] = []
        seen = set()

        for raw_tag in tags:
            tag = raw_tag.strip().lstrip("#").strip()
            if not tag or tag.lower() in seen:
                continue
            seen.add(tag.lower())

            # 1. Look for an existing tag
            search_res = WordPressService._request(
                "GET",
                f"{base}/wp-json/wp/v2/tags",
                username,
                app_password,
                params={"search": tag, "per_page": "100"},
            )
            if search_res.ok:
                matched = False
                for item in search_res.json():
                    if (item.get("name") or "").lower() == tag.lower():
                        resolved.append(item["id"])
                        matched = True
                        break
                if matched:
                    continue

            # 2. Create it if it doesn't exist
            create_res = WordPressService._request(
                "POST",
                f"{base}/wp-json/wp/v2/tags",
                username,
                app_password,
                json={"name": tag},
            )
            if create_res.ok:
                created = create_res.json()
                resolved.append(created["id"])

        return resolved

    @staticmethod
    def create_post(
        site_url: str,
        username: str,
        app_password: str,
        title: str,
        content: str,
        tags: Optional[List[str]] = None,
        status_value: str = "publish",
    ) -> Dict[str, Any]:
        base = WordPressService._base_url(site_url)
        endpoint = f"{base}/wp-json/wp/v2/posts"

        payload: Dict[str, Any] = {
            "title": title,
            "content": content,
            "status": status_value,
        }
        if tags:
            payload["tags"] = WordPressService._resolve_tags(
                base, username, app_password, tags
            )

        res = WordPressService._request(
            "POST",
            endpoint,
            username,
            app_password,
            json=payload,
        )

        if res.status_code not in (200, 201):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"WordPress post failed ({res.status_code}): {res.text[:300]}",
            )

        data = res.json()
        return {
            "success": True,
            "post_id": data.get("id"),
            "target": "WordPress",
            "link": data.get("link"),
        }
