import urllib.parse
from typing import Dict, Any, Optional
import requests
from fastapi import HTTPException, status

# Instagram Graph API & OAuth URLs
INSTAGRAM_AUTH_URL = "https://www.instagram.com/oauth/authorize"
INSTAGRAM_FB_AUTH_URL = "https://www.facebook.com/v21.0/dialog/oauth"
INSTAGRAM_TOKEN_URL = "https://api.instagram.com/oauth/access_token"
INSTAGRAM_LONG_TOKEN_URL = "https://graph.instagram.com/access_token"
INSTAGRAM_GRAPH_URL = "https://graph.instagram.com/v21.0"
FACEBOOK_GRAPH_URL = "https://graph.facebook.com/v21.0"

# Updated 2025/2026 Scopes for Instagram Business Login
DEFAULT_IG_SCOPES = (
    "instagram_business_basic,"
    "instagram_business_content_publish,"
    "instagram_business_manage_messages,"
    "instagram_business_manage_comments"
)

# Standard Facebook Login Scopes (if connected via Facebook Page)
DEFAULT_FB_SCOPES = (
    "instagram_basic,"
    "instagram_content_publish,"
    "pages_show_list,"
    "pages_read_engagement"
)


class InstagramService:
    @staticmethod
    def get_authorization_url(
        client_id: str,
        redirect_uri: str,
        state: str = "default_state",
        scope: Optional[str] = None,
        use_instagram_login: bool = True,
    ) -> str:
        """
        Generate Instagram OAuth 2.0 Authorization URL.
        Supports both direct Instagram Business Login dialog and Facebook OAuth dialog.
        """
        if use_instagram_login:
            target_scope = scope or DEFAULT_IG_SCOPES
            params = {
                "enable_fb_login": "0",
                "force_authentication": "1",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": target_scope,
                "state": state,
            }
            return f"{INSTAGRAM_AUTH_URL}?{urllib.parse.urlencode(params)}"
        else:
            target_scope = scope or DEFAULT_FB_SCOPES
            params = {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": target_scope,
                "state": state,
            }
            return f"{INSTAGRAM_FB_AUTH_URL}?{urllib.parse.urlencode(params)}"

    @staticmethod
    def exchange_authorization_code(
        client_id: str,
        client_secret: str,
        code: str,
        redirect_uri: str,
    ) -> Dict[str, Any]:
        """
        Exchange authorization code for an Access Token and resolve the Instagram Account ID.
        1. Exchange code for short-lived token.
        2. Convert short-lived token to 60-day Long-Lived Token.
        3. Retrieve Instagram Business Account ID and username.
        """
        short_token = None
        user_id = None

        # Clean redirect_uri (strip query params if any)
        clean_redirect_uri = redirect_uri.split("?")[0]

        # 1. Try exchange via Instagram OAuth endpoint
        ig_payload = {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
            "redirect_uri": clean_redirect_uri,
            "code": code,
        }

        try:
            res = requests.post(INSTAGRAM_TOKEN_URL, data=ig_payload, timeout=20)
            if res.status_code == 200:
                data = res.json()
                short_token = data.get("access_token")
                user_id = data.get("user_id")
        except requests.RequestException:
            pass

        # 1b. Fallback exchange via Facebook OAuth endpoint if Instagram endpoint didn't succeed
        if not short_token:
            fb_params = {
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": clean_redirect_uri,
                "code": code,
            }
            try:
                res = requests.get(f"{FACEBOOK_GRAPH_URL}/oauth/access_token", params=fb_params, timeout=20)
                if res.status_code == 200:
                    data = res.json()
                    short_token = data.get("access_token")
            except requests.RequestException as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Failed to exchange Instagram authorization code: {str(exc)}",
                )

        if not short_token:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Meta returned an error during token exchange. Please check your App ID, Secret, and Redirect URI.",
            )

        # 2. Exchange short-lived token for Long-Lived Access Token (60 days)
        long_token = short_token
        try:
            # Try Instagram long-lived exchange
            ll_res = requests.get(
                INSTAGRAM_LONG_TOKEN_URL,
                params={
                    "grant_type": "ig_exchange_token",
                    "client_secret": client_secret,
                    "access_token": short_token,
                },
                timeout=20,
            )
            if ll_res.status_code == 200:
                long_token = ll_res.json().get("access_token", short_token)
            else:
                # Fallback to Facebook long-lived exchange
                fb_ll = requests.get(
                    f"{FACEBOOK_GRAPH_URL}/oauth/access_token",
                    params={
                        "grant_type": "fb_exchange_token",
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "fb_exchange_token": short_token,
                    },
                    timeout=20,
                )
                if fb_ll.status_code == 200:
                    long_token = fb_ll.json().get("access_token", short_token)
        except Exception:
            pass

        # 3. Resolve Instagram Account ID and Username
        ig_account_id, username = InstagramService.get_instagram_account_details(long_token, fallback_user_id=user_id)

        return {
            "access_token": long_token,
            "instagram_account_id": ig_account_id,
            "username": username,
        }

    @staticmethod
    def get_instagram_account_details(access_token: str, fallback_user_id: Optional[Any] = None) -> tuple[Optional[str], Optional[str]]:
        """
        Query Graph API to get the linked Instagram Business / Creator Account ID and username.
        """
        # A. Query Instagram Graph API me endpoint
        try:
            r = requests.get(
                f"{INSTAGRAM_GRAPH_URL}/me",
                params={"fields": "user_id,username,name", "access_token": access_token},
                timeout=15,
            )
            if r.status_code == 200:
                d = r.json()
                acc_id = str(d.get("user_id") or d.get("id") or "")
                if acc_id:
                    return acc_id, d.get("username")
        except Exception:
            pass

        # B. Query Facebook accounts endpoint for linked Instagram Business Account
        try:
            r = requests.get(
                f"{FACEBOOK_GRAPH_URL}/me/accounts",
                params={
                    "fields": "name,instagram_business_account{id,username}",
                    "access_token": access_token,
                },
                timeout=15,
            )
            if r.status_code == 200:
                data = r.json().get("data", [])
                for page in data:
                    ig_biz = page.get("instagram_business_account")
                    if ig_biz and ig_biz.get("id"):
                        return str(ig_biz["id"]), ig_biz.get("username")
        except Exception:
            pass

        return str(fallback_user_id) if fallback_user_id else None, None

    @staticmethod
    def create_post(
        access_token: str,
        instagram_account_id: str,
        image_url: str,
        caption: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Publishes a photo/media post to Instagram using the 2-step Instagram Graph API flow:
        Step 1: Create a media container with image_url and caption.
        Step 2: Publish the media container.
        """
        if not instagram_account_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Instagram Account ID is missing. Please reconnect your Instagram account.",
            )

        # Step 1: Create Media Container
        container_endpoint = f"{INSTAGRAM_GRAPH_URL}/{instagram_account_id}/media"
        container_payload = {
            "image_url": image_url,
            "access_token": access_token,
        }
        if caption and caption.strip():
            container_payload["caption"] = caption.strip()

        try:
            c_res = requests.post(container_endpoint, data=container_payload, timeout=30)
            if c_res.status_code != 200:
                # Try via Facebook Graph URL if Instagram Graph endpoint returned non-200
                fb_container = f"{FACEBOOK_GRAPH_URL}/{instagram_account_id}/media"
                c_res = requests.post(fb_container, data=container_payload, timeout=30)

            c_data = c_res.json()
        except requests.RequestException as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Network error while creating Instagram media container: {str(e)}",
            )

        creation_id = c_data.get("id")
        if not creation_id:
            err = c_data.get("error", {}).get("message", "Unknown Meta error")
            if "127.0.0.1" in image_url or "localhost" in image_url:
                err += " (Note: Meta servers cannot fetch images from localhost/127.0.0.1. Please use a public image URL or test on your deployed production domain.)"
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Instagram Media Container creation failed: {err}",
            )

        # Step 2: Publish Media Container
        publish_endpoint = f"{INSTAGRAM_GRAPH_URL}/{instagram_account_id}/media_publish"
        publish_payload = {
            "creation_id": creation_id,
            "access_token": access_token,
        }

        try:
            p_res = requests.post(publish_endpoint, data=publish_payload, timeout=30)
            if p_res.status_code != 200:
                fb_publish = f"{FACEBOOK_GRAPH_URL}/{instagram_account_id}/media_publish"
                p_res = requests.post(fb_publish, data=publish_payload, timeout=30)

            p_data = p_res.json()
        except requests.RequestException as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Network error while publishing Instagram post: {str(e)}",
            )

        post_id = p_data.get("id")
        if not post_id:
            err = p_data.get("error", {}).get("message", "Unknown Meta publish error")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Instagram Publish failed: {err}",
            )

        return {
            "success": True,
            "post_id": post_id,
            "creation_id": creation_id,
            "target": "Instagram",
        }
