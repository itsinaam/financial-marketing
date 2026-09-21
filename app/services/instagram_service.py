import time
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

# Scopes for Instagram Business Publishing via Meta Graph API
DEFAULT_FB_SCOPES = (
    "instagram_basic,"
    "instagram_content_publish,"
    "pages_show_list,"
    "pages_read_engagement,"
    "pages_manage_posts"
)

# Alternative Direct Instagram Login scopes
DEFAULT_IG_SCOPES = (
    "instagram_business_basic,"
    "instagram_business_content_publish"
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
        Defaults to Instagram Login so users authenticate on Instagram directly.
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
        1. Exchange code for token (supports both Facebook Graph and Instagram OAuth).
        2. Convert short-lived token to 60-day Long-Lived Token / Page Access Token.
        3. Retrieve Instagram Business Account ID and username.
        """
        short_token = None
        user_id = None
        exchange_errors = []

        clean_redirect_uri = redirect_uri.split("?")[0]

        # 1. Try Facebook OAuth endpoint first (Standard for Instagram Graph API)
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
            else:
                exchange_errors.append(res.text[:500])
        except requests.RequestException:
            exchange_errors.append("Facebook token exchange request failed")

        # 1b. Fallback to direct Instagram endpoint
        if not short_token:
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
                else:
                    exchange_errors.append(res.text[:500])
            except requests.RequestException:
                exchange_errors.append("Instagram token exchange request failed")

        if not short_token:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Meta returned an error during token exchange. Verify that the OAuth URL client_id, "
                    "INSTAGRAM_CLIENT_ID, INSTAGRAM_CLIENT_SECRET, and redirect_uri belong to the same Meta app. "
                    f"Provider response: {' | '.join(exchange_errors)}"
                ),
            )

        # 2. Upgrade to Long-Lived Token (60 days)
        long_token = short_token
        try:
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
            else:
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
        except Exception:
            pass

        # 3. Resolve Instagram Account ID, Username, and Page Token
        ig_account_id, username, page_token = InstagramService.get_instagram_account_details(long_token, fallback_user_id=user_id)

        # Prefer Page Access Token if available (permanent & has posting permissions)
        final_token = page_token or long_token

        return {
            "access_token": final_token,
            "instagram_account_id": ig_account_id,
            "username": username,
        }

    @staticmethod
    def get_instagram_account_details(access_token: str, fallback_user_id: Optional[Any] = None) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """
        Query Graph API to get the linked Instagram Business / Creator Account ID, username, and page access token.
        """
        # A. Query Facebook accounts endpoint for linked Instagram Business Account
        try:
            r = requests.get(
                f"{FACEBOOK_GRAPH_URL}/me/accounts",
                params={
                    "fields": "name,access_token,instagram_business_account{id,username}",
                    "access_token": access_token,
                },
                timeout=15,
            )
            if r.status_code == 200:
                data = r.json().get("data", [])
                for page in data:
                    ig_biz = page.get("instagram_business_account")
                    if ig_biz and ig_biz.get("id"):
                        return str(ig_biz["id"]), ig_biz.get("username"), page.get("access_token")
        except Exception:
            pass

        # B. Query Instagram Graph API me endpoint
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
                    return acc_id, d.get("username"), None
        except Exception:
            pass

        return (str(fallback_user_id) if fallback_user_id else None), None, None

    @staticmethod
    def create_media_container(
        access_token: str,
        instagram_account_id: str,
        image_url: str,
        caption: Optional[str] = None,
    ) -> str:
        """
        Create Instagram media container using the Graph API.
        """
        if not instagram_account_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Instagram Account ID is missing. Please reconnect your Instagram account.",
            )

        url = f"{INSTAGRAM_GRAPH_URL}/{instagram_account_id}/media"
        payload = {
            "image_url": image_url,
            "access_token": access_token,
        }
        if caption and caption.strip():
            payload["caption"] = caption.strip()

        try:
            response = requests.post(url, data=payload, timeout=30)
        except requests.RequestException as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Network error while creating Instagram media container: {str(exc)}",
            ) from exc

        if response.status_code not in (200, 201):
            error_detail = response.text
            if "127.0.0.1" in image_url or "localhost" in image_url:
                error_detail += " (Meta cannot fetch images from localhost/127.0.0.1; use a public URL)"
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Instagram media container creation failed: {error_detail}",
            )

        data = response.json()
        creation_id = data.get("id")
        if not creation_id:
            err = data.get("error", {}).get("message", "Unknown Meta error")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Instagram media container creation failed: {err}",
            )

        return str(creation_id)

    @staticmethod
    def publish_media(
        access_token: str,
        instagram_account_id: str,
        creation_id: str,
    ) -> Dict[str, Any]:
        """
        Publish an Instagram media container.
        """
        url = f"{INSTAGRAM_GRAPH_URL}/{instagram_account_id}/media_publish"
        payload = {
            "creation_id": creation_id,
            "access_token": access_token,
        }

        try:
            response = requests.post(url, data=payload, timeout=30)
        except requests.RequestException as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Network error while publishing Instagram post: {str(exc)}",
            ) from exc

        if response.status_code not in (200, 201):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Instagram publish failed: {response.text}",
            )

        data = response.json()
        post_id = data.get("id")
        if not post_id:
            err = data.get("error", {}).get("message", "Unknown Meta publish error")
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

    @staticmethod
    def create_post(
        access_token: str,
        instagram_account_id: str,
        image_url: str,
        caption: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Create a media container, wait briefly for Meta to process it, then publish.
        """
        creation_id = InstagramService.create_media_container(
            access_token=access_token,
            instagram_account_id=instagram_account_id,
            image_url=image_url,
            caption=caption,
        )

        time.sleep(5)

        return InstagramService.publish_media(
            access_token=access_token,
            instagram_account_id=instagram_account_id,
            creation_id=creation_id,
        )
