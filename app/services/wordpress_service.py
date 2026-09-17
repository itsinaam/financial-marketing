from typing import Any, Dict, Optional
import requests
from fastapi import HTTPException, status


class WordPressService:
    """
    Publishes to a self-hosted or WordPress.com site via the WordPress REST API,
    authenticated with an Application Password (Users > Profile > Application
    Passwords in wp-admin). No OAuth dance required.
    """

    @staticmethod
    def create_post(
        site_url: str,
        username: str,
        app_password: str,
        title: str,
        content: str,
        status_value: str = "publish",
    ) -> Dict[str, Any]:
        base = site_url.rstrip("/")
        endpoint = f"{base}/wp-json/wp/v2/posts"

        try:
            res = requests.post(
                endpoint,
                auth=(username, app_password),
                json={"title": title, "content": content, "status": status_value},
                timeout=30,
            )
        except requests.RequestException as e:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to connect to WordPress site '{base}': {str(e)}",
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
