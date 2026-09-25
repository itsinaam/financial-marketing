import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
import requests
from fastapi import HTTPException, status

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
BLOGGER_API_BASE = "https://www.googleapis.com/blogger/v3"

BLOGGER_SCOPE = "https://www.googleapis.com/auth/blogger"


class BloggerService:
    """
    Publishes to a Google Blogger blog via the Blogger v3 REST API using an
    OAuth 2.0 (offline) access token. The refresh_token lets the app keep
    publishing long after the initial consent.
    """

    @staticmethod
    def get_authorization_url(
        client_id: str,
        redirect_uri: str,
        state: str,
        scope: Optional[str] = None,
    ) -> str:
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scope or BLOGGER_SCOPE,
            "state": state,
            "access_type": "offline",
            "prompt": "consent",
        }
        return f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"

    @staticmethod
    def exchange_authorization_code(
        client_id: str,
        client_secret: str,
        code: str,
        redirect_uri: str,
    ) -> Dict[str, Any]:
        try:
            response = requests.post(
                GOOGLE_TOKEN_URL,
                data={
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30,
            )
        except requests.RequestException as e:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to connect to Google API: {str(e)}",
            )

        if response.status_code != 200:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Blogger token exchange failed: {response.text}",
            )

        token_data = response.json()
        access_token = token_data.get("access_token")
        if not access_token:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Google did not return an access token.",
            )

        expires_in = token_data.get("expires_in")
        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
            if expires_in is not None else None
        )

        return {
            "access_token": access_token,
            "refresh_token": token_data.get("refresh_token"),
            "expires_at": expires_at,
        }

    @staticmethod
    def refresh_access_token(
        client_id: str,
        client_secret: str,
        refresh_token: str,
    ) -> Dict[str, Any]:
        """Exchange a refresh_token for a new access token."""
        try:
            response = requests.post(
                GOOGLE_TOKEN_URL,
                data={
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "refresh_token",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30,
            )
        except requests.RequestException as e:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to connect to Google API: {str(e)}",
            )

        if response.status_code != 200:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Blogger token refresh failed. Reconnect the account: {response.text}",
            )

        token_data = response.json()
        expires_in = token_data.get("expires_in")
        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
            if expires_in is not None else None
        )
        return {
            "access_token": token_data["access_token"],
            "refresh_token": token_data.get("refresh_token", refresh_token),
            "expires_at": expires_at,
        }

    @staticmethod
    def list_blogs(access_token: str) -> List[Dict[str, Any]]:
        try:
            response = requests.get(
                f"{BLOGGER_API_BASE}/users/self/blogs",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=30,
            )
        except requests.RequestException as e:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to connect to Google API: {str(e)}",
            )

        if response.status_code != 200:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to list Blogger blogs: {response.text}",
            )

        items = response.json().get("items") or []
        return [
            {
                "id": blog.get("id"),
                "name": blog.get("name") or blog.get("status"),
                "url": blog.get("url"),
            }
            for blog in items
        ]

    @staticmethod
    def create_post(
        access_token: str,
        blog_id: str,
        title: str,
        content: str,
        labels: Optional[List[str]] = None,
        status_value: str = "LIVE",
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "kind": "blogger#post",
            "title": title,
            "content": content,
            "status": status_value,
        }
        if labels:
            payload["labels"] = labels

        try:
            response = requests.post(
                f"{BLOGGER_API_BASE}/blogs/{blog_id}/posts",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=30,
            )
        except requests.RequestException as e:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to connect to Google API: {str(e)}",
            )

        if response.status_code not in (200, 201):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Blogger post failed ({response.status_code}): {response.text[:300]}",
            )

        data = response.json()
        return {
            "success": True,
            "post_id": data.get("id"),
            "target": "Blogger",
            "link": data.get("url"),
        }