import base64
import hashlib
import secrets
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional
import requests
from fastapi import HTTPException, status

X_AUTH_URL = "https://x.com/i/oauth2/authorize"
X_TOKEN_URL = "https://api.x.com/2/oauth2/token"
X_API_BASE = "https://api.x.com/2"

DEFAULT_X_SCOPES = "tweet.read tweet.write users.read offline.access"

STATE_DELIMITER = "::"


class TwitterService:
    @staticmethod
    def _code_challenge(code_verifier: str) -> str:
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    @staticmethod
    def get_authorization_url(
        client_id: str,
        redirect_uri: str,
        company_id: str,
        scope: Optional[str] = None,
    ) -> str:
        """
        Generate X (Twitter) OAuth 2.0 Authorization URL using PKCE.
        The code_verifier is embedded into the opaque `state` param (alongside the
        company_id) so it can be recovered at the callback without extra storage.
        """
        code_verifier = secrets.token_urlsafe(64)
        state = f"{company_id}{STATE_DELIMITER}{code_verifier}"

        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scope or DEFAULT_X_SCOPES,
            "state": state,
            "code_challenge": TwitterService._code_challenge(code_verifier),
            "code_challenge_method": "S256",
        }
        return f"{X_AUTH_URL}?{urllib.parse.urlencode(params)}"

    @staticmethod
    def parse_state(state: str) -> tuple[Optional[str], Optional[str]]:
        """Recover (company_id, code_verifier) from the opaque state param."""
        if not state or STATE_DELIMITER not in state:
            return None, None
        company_id, code_verifier = state.split(STATE_DELIMITER, 1)
        return company_id, code_verifier

    @staticmethod
    def exchange_authorization_code(
        client_id: str,
        client_secret: str,
        code: str,
        redirect_uri: str,
        code_verifier: str,
    ) -> Dict[str, Any]:
        """
        Exchange authorization code + PKCE verifier for an access/refresh token pair.
        """
        try:
            response = requests.post(
                X_TOKEN_URL,
                data={
                    "code": code,
                    "grant_type": "authorization_code",
                    "redirect_uri": redirect_uri,
                    "code_verifier": code_verifier,
                },
                auth=(client_id, client_secret),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30,
            )
        except requests.RequestException as e:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to connect to X API: {str(e)}",
            )

        if response.status_code != 200:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"X token exchange failed: {response.text}",
            )

        token_data = response.json()
        access_token = token_data.get("access_token")
        if not access_token:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="X did not return an access token.",
            )

        username = None
        try:
            user_res = requests.get(
                f"{X_API_BASE}/users/me",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=15,
            )
            if user_res.status_code == 200:
                username = user_res.json().get("data", {}).get("username")
        except requests.RequestException:
            pass

        expires_in = token_data.get("expires_in")
        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
            if expires_in is not None else None
        )

        return {
            "access_token": access_token,
            "refresh_token": token_data.get("refresh_token"),
            "expires_at": expires_at,
            "username": username,
        }

    @staticmethod
    def refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> Dict[str, Any]:
        """Exchange a refresh_token for a new access/refresh token pair."""
        try:
            response = requests.post(
                X_TOKEN_URL,
                data={"grant_type": "refresh_token", "refresh_token": refresh_token},
                auth=(client_id, client_secret),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30,
            )
        except requests.RequestException as e:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to connect to X API: {str(e)}",
            )

        if response.status_code != 200:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"X token refresh failed. Reconnect the account: {response.text}",
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
    def create_post(access_token: str, text: str) -> Dict[str, Any]:
        """Publish a text tweet. (Media upload requires X API v1.1 endpoints and elevated access.)"""
        try:
            response = requests.post(
                f"{X_API_BASE}/tweets",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json={"text": text},
                timeout=30,
            )
        except requests.RequestException as e:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to connect to X API: {str(e)}",
            )

        if response.status_code not in (200, 201):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"X post failed: {response.text}",
            )

        tweet_data = response.json().get("data", {})
        return {
            "success": True,
            "post_id": tweet_data.get("id"),
            "target": "X (Twitter)",
        }
