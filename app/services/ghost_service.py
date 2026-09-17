import time
from typing import Any, Dict
import requests
from jose import jwt
from fastapi import HTTPException, status


class GhostService:
    """
    Publishes to a Ghost site via the Ghost Admin API, authenticated with a
    short-lived JWT signed using the site's Admin API Key (Settings >
    Integrations > Custom Integration in Ghost Admin), no OAuth required.
    """

    @staticmethod
    def _build_token(admin_api_key: str) -> str:
        try:
            key_id, secret = admin_api_key.split(":")
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Ghost Admin API Key must be in 'id:secret' format.",
            )

        now = int(time.time())
        return jwt.encode(
            {"iat": now, "exp": now + 300, "aud": "/admin/"},
            bytes.fromhex(secret),
            algorithm="HS256",
            headers={"kid": key_id},
        )

    @staticmethod
    def create_post(
        admin_api_url: str,
        admin_api_key: str,
        title: str,
        html_content: str,
        status_value: str = "published",
    ) -> Dict[str, Any]:
        base = admin_api_url.rstrip("/")
        endpoint = f"{base}/ghost/api/admin/posts/?source=html"
        token = GhostService._build_token(admin_api_key)

        try:
            res = requests.post(
                endpoint,
                headers={
                    "Authorization": f"Ghost {token}",
                    "Content-Type": "application/json",
                },
                json={"posts": [{"title": title, "html": html_content, "status": status_value}]},
                timeout=30,
            )
        except requests.RequestException as e:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to connect to Ghost site '{base}': {str(e)}",
            )

        if res.status_code not in (200, 201):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Ghost post failed ({res.status_code}): {res.text[:300]}",
            )

        data = res.json()
        posts = data.get("posts") or []
        post = posts[0] if posts else {}
        return {
            "success": True,
            "post_id": post.get("id"),
            "target": "Ghost",
            "link": post.get("url"),
        }
