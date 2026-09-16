from typing import Dict, Any, Optional
import urllib.parse
import requests
from fastapi import HTTPException, status

FACEBOOK_AUTH_URL = "https://www.facebook.com/v21.0/dialog/oauth"
FACEBOOK_GRAPH_URL = "https://graph.facebook.com/v21.0"

# Scopes required to list Pages and publish to them
DEFAULT_FB_SCOPES = (
    "pages_show_list,"
    "pages_read_engagement,"
    "pages_manage_posts,"
    "pages_manage_metadata"
)


class FacebookService:
    @staticmethod
    def get_authorization_url(
        client_id: str,
        redirect_uri: str,
        state: str = "default_state",
        scope: Optional[str] = None,
    ) -> str:
        """
        Generate Facebook OAuth 2.0 Authorization URL (Facebook Login for Business).
        """
        target_scope = scope or DEFAULT_FB_SCOPES
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": target_scope,
            "state": state,
        }
        return f"{FACEBOOK_AUTH_URL}?{urllib.parse.urlencode(params)}"

    @staticmethod
    def exchange_authorization_code(
        client_id: str,
        client_secret: str,
        code: str,
        redirect_uri: str,
    ) -> Dict[str, Any]:
        """
        Exchange authorization code for a User Access Token, upgrade it to a
        Long-Lived Token, then resolve the first managed Facebook Page and its
        permanent Page Access Token (used for posting).
        """
        clean_redirect_uri = redirect_uri.split("?")[0]

        try:
            res = requests.get(
                f"{FACEBOOK_GRAPH_URL}/oauth/access_token",
                params={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": clean_redirect_uri,
                    "code": code,
                },
                timeout=20,
            )
        except requests.RequestException as e:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to connect to Facebook API: {str(e)}",
            )

        data = res.json()
        short_token = data.get("access_token")
        if not short_token:
            err = data.get("error", {}).get("message", "Unknown Meta error")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Facebook token exchange failed: {err}",
            )

        # Upgrade to a Long-Lived User Token (~60 days)
        long_token = short_token
        try:
            ll_res = requests.get(
                f"{FACEBOOK_GRAPH_URL}/oauth/access_token",
                params={
                    "grant_type": "fb_exchange_token",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "fb_exchange_token": short_token,
                },
                timeout=20,
            )
            if ll_res.status_code == 200:
                long_token = ll_res.json().get("access_token", short_token)
        except requests.RequestException:
            pass

        page_id, page_name, page_token = FacebookService.get_page_details(long_token)
        if not page_id or not page_token:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "No manageable Facebook Page found on this account. "
                    "Make sure the user is an admin of at least one Facebook Page."
                ),
            )

        return {
            "access_token": page_token,
            "page_id": page_id,
            "page_name": page_name,
        }

    @staticmethod
    def get_page_details(user_access_token: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """
        Query Graph API for the first Facebook Page the user manages, returning
        (page_id, page_name, page_access_token).
        """
        try:
            r = requests.get(
                f"{FACEBOOK_GRAPH_URL}/me/accounts",
                params={"fields": "name,access_token", "access_token": user_access_token},
                timeout=15,
            )
            if r.status_code == 200:
                data = r.json().get("data", [])
                if data:
                    page = data[0]
                    return str(page.get("id")), page.get("name"), page.get("access_token")
        except requests.RequestException:
            pass
        return None, None, None

    @staticmethod
    def create_post(
        page_access_token: str,
        page_id: str,
        message: str,
        image_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Publish a post to a Facebook Page.
        - With image_url: creates a photo post (image + caption).
        - Without image_url: creates a text-only feed post.
        """
        if not page_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Facebook Page ID is missing. Please reconnect your Facebook account.",
            )

        if image_url:
            endpoint = f"{FACEBOOK_GRAPH_URL}/{page_id}/photos"
            payload = {
                "url": image_url,
                "caption": message,
                "access_token": page_access_token,
            }
        else:
            endpoint = f"{FACEBOOK_GRAPH_URL}/{page_id}/feed"
            payload = {
                "message": message,
                "access_token": page_access_token,
            }

        try:
            res = requests.post(endpoint, data=payload, timeout=30)
            data = res.json()
        except requests.RequestException as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Network error while publishing Facebook post: {str(e)}",
            )

        post_id = data.get("post_id") or data.get("id")
        if not post_id:
            err = data.get("error", {}).get("message", "Unknown Meta error")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Facebook post failed: {err}",
            )

        return {"success": True, "post_id": post_id, "target": "Facebook Page"}
