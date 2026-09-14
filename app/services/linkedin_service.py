import os
import urllib.parse
from typing import Dict, Any, Optional
import requests
from fastapi import HTTPException, status

LINKEDIN_TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
LINKEDIN_AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
LINKEDIN_REST_POSTS_URL = "https://api.linkedin.com/rest/posts"
LINKEDIN_UGC_POSTS_URL = "https://api.linkedin.com/v2/ugcPosts"
LINKEDIN_USERINFO_URL = "https://api.linkedin.com/v2/userinfo"
LINKEDIN_API_VERSION = "202603"

# Default approved scopes for personal posting
DEFAULT_MEMBER_SCOPES = "openid profile email w_member_social"


class LinkedInService:
    @staticmethod
    def get_authorization_url(
        client_id: str,
        redirect_uri: str,
        scope: Optional[str] = None,
        state: str = "secure_random_state",
    ) -> str:
        """
        Generate LinkedIn 3-legged OAuth Authorization URL.
        """
        target_scope = scope or DEFAULT_MEMBER_SCOPES
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": target_scope,
            "state": state,
        }
        return f"{LINKEDIN_AUTH_URL}?{urllib.parse.urlencode(params)}"

    @staticmethod
    def verify_credentials(client_id: str, client_secret: str) -> dict:
        """
        Verify if Client ID and Client Secret are authentic by contacting LinkedIn's token endpoint.
        """
        payload = {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        try:
            response = requests.post(LINKEDIN_TOKEN_URL, data=payload, headers=headers, timeout=15)
            data = response.json()
        except requests.RequestException as e:
            return {
                "valid": False,
                "error": f"Failed to connect to LinkedIn API: {str(e)}",
            }

        if response.status_code == 200:
            return {
                "valid": True,
                "access_token": data.get("access_token"),
                "message": "Credentials are valid and access token was successfully generated.",
            }

        error_code = data.get("error", "")
        error_desc = data.get("error_description", "") or data.get("message", "")

        if error_code == "invalid_client":
            return {
                "valid": False,
                "error": f"Invalid Client ID or Client Secret ({error_desc or 'Client authentication failed'}).",
            }
        elif error_code in ("unauthorized_client", "access_denied"):
            return {
                "valid": True,
                "message": f"Client ID and Client Secret are authentic and valid on LinkedIn! (Note: {error_desc}. 3-legged authorization is required to generate social posting tokens).",
            }
        else:
            return {
                "valid": False,
                "error": f"LinkedIn returned ({error_code}): {error_desc}",
            }

    @staticmethod
    def exchange_authorization_code(
        client_id: str,
        client_secret: str,
        code: str,
        redirect_uri: str,
    ) -> dict:
        """
        3-legged OAuth (Authorization Code Flow).
        Exchanges authorization code obtained via Callback URL for an access token.
        """
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": client_secret,
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        try:
            response = requests.post(LINKEDIN_TOKEN_URL, data=payload, headers=headers, timeout=15)
        except requests.RequestException as e:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to connect to LinkedIn API: {str(e)}",
            )

        data = response.json()
        if response.status_code != 200:
            error_desc = data.get("error_description") or data.get("message") or data.get("error") or "Unknown error"
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"LinkedIn token exchange failed ({data.get('error', 'error')}): {error_desc}",
            )

        return data

    @staticmethod
    def get_user_info(access_token: str) -> dict:
        """
        Fetch basic profile info of the authenticated LinkedIn member (e.g. member sub/id).
        """
        headers = {"Authorization": f"Bearer {access_token}"}
        try:
            res = requests.get(LINKEDIN_USERINFO_URL, headers=headers, timeout=15)
        except requests.RequestException as e:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to connect to LinkedIn userinfo API: {str(e)}",
            )

        if res.status_code != 200:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Could not retrieve LinkedIn user info: {res.text}",
            )
        return res.json()

    @staticmethod
    def upload_image(
        access_token: str,
        owner_urn: str,
        file_bytes: bytes,
        content_type: str = "image/jpeg",
    ) -> str:
        """
        Uploads an image to LinkedIn using the modern REST Images API.
        Step 1: Initialize image upload
        Step 2: Upload image binary stream
        Returns: image URN (e.g. 'urn:li:image:...')
        """
        init_url = "https://api.linkedin.com/rest/images?action=initializeUpload"
        init_headers = {
            "Authorization": f"Bearer {access_token}",
            "LinkedIn-Version": LINKEDIN_API_VERSION,
            "X-Restli-Protocol-Version": "2.0.0",
            "Content-Type": "application/json",
        }
        init_body = {
            "initializeUploadRequest": {
                "owner": owner_urn
            }
        }
        try:
            init_res = requests.post(init_url, json=init_body, headers=init_headers, timeout=20)
        except requests.RequestException as e:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to initialize image upload with LinkedIn: {str(e)}",
            )

        if init_res.status_code not in (200, 201):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"LinkedIn image upload initialization failed: {init_res.text}",
            )

        init_data = init_res.json().get("value", {})
        upload_url = init_data.get("uploadUrl")
        image_urn = init_data.get("image")

        if not upload_url or not image_urn:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="LinkedIn did not return an image upload URL.",
            )

        put_headers = {
            "Authorization": f"Bearer {access_token}",
            "X-Restli-Protocol-Version": "2.0.0",
            "Content-Type": content_type or "image/jpeg",
        }
        try:
            put_res = requests.put(upload_url, data=file_bytes, headers=put_headers, timeout=60)
        except requests.RequestException as e:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to upload image binary to LinkedIn: {str(e)}",
            )

        if put_res.status_code not in (200, 201):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"LinkedIn image upload failed: {put_res.text}",
            )

        return image_urn

    @staticmethod
    def create_post(
        access_token: str,
        text: str,
        image_urn: Optional[str] = None,
        organization_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Publish a post to LinkedIn:
        - Supports text-only and image posts
        - If organization_id is provided, posts to Company Page (requires w_organization_social).
        - Otherwise, posts to Member's Personal Feed (requires w_member_social).
        """
        if organization_id:
            clean_org_id = organization_id.replace("urn:li:organization:", "").strip()
            author_urn = f"urn:li:organization:{clean_org_id}"
            target_desc = f"Company Page ({clean_org_id})"
        else:
            user_info = LinkedInService.get_user_info(access_token)
            member_sub = user_info.get("sub")
            if not member_sub:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Could not resolve member ID from token.",
                )
            author_urn = f"urn:li:person:{member_sub}"
            target_desc = f"Personal Profile ({user_info.get('name', member_sub)})"

        # 1. Try modern Posts REST API
        rest_headers = {
            "Authorization": f"Bearer {access_token}",
            "LinkedIn-Version": LINKEDIN_API_VERSION,
            "X-Restli-Protocol-Version": "2.0.0",
            "Content-Type": "application/json",
        }
        rest_body = {
            "author": author_urn,
            "commentary": text,
            "visibility": "PUBLIC",
            "distribution": {
                "feedDistribution": "MAIN_FEED",
                "targetEntities": [],
                "thirdPartyDistributionChannels": [],
            },
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }
        if image_urn:
            rest_body["content"] = {
                "media": {
                    "id": image_urn,
                    "title": "Post Image"
                }
            }

        try:
            response = requests.post(LINKEDIN_REST_POSTS_URL, json=rest_body, headers=rest_headers, timeout=20)
        except requests.RequestException as e:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to communicate with LinkedIn: {str(e)}",
            )

        if response.status_code in (200, 201):
            post_urn = response.headers.get("x-restli-id") or response.headers.get("location") or "published"
            return {"post_id": post_urn, "target": target_desc, "status": "success"}

        # 2. Fallback to UGC Posts API
        ugc_headers = {
            "Authorization": f"Bearer {access_token}",
            "X-Restli-Protocol-Version": "2.0.0",
            "Content-Type": "application/json",
        }
        ugc_body = {
            "author": author_urn,
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {"text": text},
                    "shareMediaCategory": "IMAGE" if image_urn else "NONE",
                }
            },
            "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
        }
        if image_urn:
            ugc_body["specificContent"]["com.linkedin.ugc.ShareContent"]["media"] = [
                {
                    "status": "READY",
                    "description": {"text": text[:50] if text else "Image"},
                    "media": image_urn,
                    "title": {"text": "Post Image"}
                }
            ]

        try:
            ugc_response = requests.post(LINKEDIN_UGC_POSTS_URL, json=ugc_body, headers=ugc_headers, timeout=20)
        except requests.RequestException as e:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to post via fallback API: {str(e)}",
            )

        if ugc_response.status_code in (200, 201):
            ugc_data = ugc_response.json()
            return {"post_id": ugc_data.get("id"), "target": target_desc, "status": "success"}

        # Extract LinkedIn error description
        error_details = response.text or ugc_response.text
        try:
            parsed = response.json()
            error_details = parsed.get("message") or parsed.get("errorDescription") or error_details
        except Exception:
            pass

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to publish post to {target_desc}: {error_details}",
        )
