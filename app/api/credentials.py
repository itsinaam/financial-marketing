from datetime import datetime, timezone
from typing import Any, Optional, List
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status, File, UploadFile, Form
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt, JWTError
from pydantic import ValidationError
from sqlalchemy.orm import Session
from app.core import deps
from app.core.config import settings
from app.models.companies import Company, Role
from app.models.credentials import Credentials
from app.schemas.credentials import (
    SaveCredentialsRequest,
    PostResponse,
    CredentialsResponse,
    PlatformStatusResponse,
    OAuthConnectResponse,
)
from app.schemas.token import TokenPayload
from app.services.linkedin_service import LinkedInService, DEFAULT_MEMBER_SCOPES
from app.services.instagram_service import InstagramService
from app.services.facebook_service import FacebookService
from app.services.twitter_service import TwitterService
from app.services.storage_service import upload_library_asset

router = APIRouter()
optional_bearer = HTTPBearer(auto_error=False)

CORE_PLATFORMS = ["linkedin", "instagram", "facebook", "x"]


def get_request_base_url(request: Request) -> str:
    """
    Get the public base URL used by OAuth callbacks.
    """
    if settings.PUBLIC_BASE_URL:
        return settings.PUBLIC_BASE_URL.rstrip("/")

    base = str(request.base_url).rstrip("/")
    proto = request.headers.get("x-forwarded-proto")
    if proto and base.startswith("http://"):
        base = "https://" + base[len("http://"):]
    return base


def get_current_callback_url(request: Request) -> str:
    """
    Get the public callback URL without query parameters.
    """
    if settings.PUBLIC_BASE_URL:
        return (
            f"{settings.PUBLIC_BASE_URL.rstrip('/')}{settings.API_V1_STR}"
            "/credentials/instagram/callback"
        )

    url = str(request.url).split("?")[0].rstrip("/")
    proto = request.headers.get("x-forwarded-proto")
    if proto and url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    return url


def resolve_company(db: Session, auth: Optional[HTTPAuthorizationCredentials], company_id: Optional[int]) -> Company:
    """
    Resolve company either from Bearer JWT token or from explicit company_id in the request.
    """
    if auth and auth.credentials:
        try:
            payload = jwt.decode(
                auth.credentials, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
            )
            token_data = TokenPayload(**payload)
            if token_data.sub:
                user = db.query(Company).filter(Company.email == token_data.sub).first()
                if user:
                    # Allow Super Admin to query a specific company_id
                    if company_id is not None and (user.role == Role.SUPERADMIN or user.is_superuser):
                        target = db.query(Company).filter(Company.id == company_id).first()
                        if target:
                            return target
                        raise HTTPException(
                            status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"Company with id {company_id} not found",
                        )
                    return user
        except (JWTError, ValidationError):
            pass

    if company_id is not None:
        user = db.query(Company).filter(Company.id == company_id).first()
        if user:
            return user
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Company with id {company_id} not found",
        )

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required: Provide Bearer token in Authorization header or company_id in request.",
    )


@router.get("", response_model=List[PlatformStatusResponse], include_in_schema=False)
def get_linked_platforms(
    company_id: Optional[int] = Query(
        None,
        description="Optional company ID override (Admin / Testing). Defaults to current logged-in company.",
    ),
    db: Session = Depends(deps.get_db),
    auth: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
) -> Any:
    """
    Get listing of social media platforms (LinkedIn, Instagram, Facebook, X, etc.)
    and whether they are linked (is_connected: true/false) for the current company.
    """
    company = resolve_company(db, auth, company_id)

    # Fetch all credentials saved for this company
    user_credentials = (
        db.query(Credentials)
        .filter(Credentials.company_id == company.id)
        .all()
    )

    # Map existing credentials by normalized platform key
    connected_map = {}
    for cred in user_credentials:
        p_key = (cred.platform or "").strip().lower()
        if p_key in ("twitter", "x"):
            p_key = "x"
        connected_map[p_key] = bool(cred.access_token and cred.access_token.strip())

    results: List[PlatformStatusResponse] = []
    processed = set()

    # 1. Standard core platforms
    for p in CORE_PLATFORMS:
        processed.add(p)
        results.append(
            PlatformStatusResponse(
                platform=p,
                is_connected=connected_map.get(p, False),
            )
        )

    # 2. Any extra custom platforms saved in DB for this company
    for p_key, is_conn in connected_map.items():
        if p_key not in processed:
            results.append(
                PlatformStatusResponse(
                    platform=p_key,
                    is_connected=is_conn,
                )
            )

    return results


@router.get(
    "/instagram/connect",
    response_model=OAuthConnectResponse,
    summary="Create the Instagram OAuth URL for a one-click account connection",
)
def connect_instagram(
    request: Request,
    company_id: Optional[int] = Query(
        None,
        description="Optional company ID override for an authenticated super admin or testing.",
    ),
    db: Session = Depends(deps.get_db),
    auth: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
) -> OAuthConnectResponse:
    """
    Prepare Instagram OAuth without asking the user for an Instagram password.

    The browser is redirected to Instagram/Meta, where the user logs in and
    grants permissions. Instagram never sends that password to this API.
    """
    company = resolve_company(db, auth, company_id)
    redirect_uri = (
        f"{get_request_base_url(request)}"
        f"{settings.API_V1_STR}/credentials/instagram/callback"
    )
    credential = (
        db.query(Credentials)
        .filter(
            Credentials.company_id == company.id,
            Credentials.platform == "instagram",
        )
        .first()
    )
    client_id = credential.client_id if credential else settings.INSTAGRAM_CLIENT_ID
    client_secret = credential.client_secret if credential else settings.INSTAGRAM_CLIENT_SECRET
    if not client_id or not client_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Instagram OAuth is not configured. Set INSTAGRAM_CLIENT_ID and "
                "INSTAGRAM_CLIENT_SECRET in the server environment."
            ),
        )

    if credential:
        credential.client_id = client_id
        credential.client_secret = client_secret
    else:
        credential = Credentials(
            company_id=company.id,
            platform="instagram",
            client_id=client_id,
            client_secret=client_secret,
        )
        db.add(credential)
    db.commit()

    return OAuthConnectResponse(
        company_id=company.id,
        platform="instagram",
        authorization_url=InstagramService.get_authorization_url(
            client_id=client_id,
            redirect_uri=redirect_uri,
            state=str(company.id),
        ),
        redirect_uri=redirect_uri,
        message="Open authorization_url in the browser to log in to Instagram and grant access.",
    )


@router.post("", response_model=CredentialsResponse, summary="Save platform credentials (Client ID, Secret, Platform) into Database")
def save_credentials(
    payload: SaveCredentialsRequest,
    request: Request,
    db: Session = Depends(deps.get_db),
    auth: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
) -> Any:
    """
    Takes client_id, client_secret, platform (e.g. 'linkedin' or 'instagram'), and saves or updates
    the record in the database for the authenticated company.
    """
    company = resolve_company(db, auth, payload.company_id)

    path = request.url.path.rstrip("/")
    if path.endswith("/instagram"):
        platform_name = "instagram"
    elif path.endswith("/linkedin"):
        platform_name = "linkedin"
    elif path.endswith("/facebook"):
        platform_name = "facebook"
    elif path.endswith("/x") or path.endswith("/twitter"):
        platform_name = "x"
    else:
        platform_name = payload.platform.strip().lower()
        if platform_name == "twitter":
            platform_name = "x"

    base_url = get_request_base_url(request)
    access_token = None
    response_msg = "Credentials saved successfully."
    auth_url = None

    if platform_name == "linkedin":
        effective_redirect_uri = f"{base_url}{settings.API_V1_STR}/credentials/linkedin/callback"
        verification = LinkedInService.verify_credentials(
            client_id=payload.client_id,
            client_secret=payload.client_secret,
        )
        if verification.get("access_token"):
            access_token = verification["access_token"]
            response_msg = "Credentials saved and access token generated successfully."
        else:
            auth_url = LinkedInService.get_authorization_url(
                client_id=payload.client_id,
                redirect_uri=effective_redirect_uri,
                state=str(company.id),
            )
            response_msg = (
                f"Credentials saved in database! Make sure '{effective_redirect_uri}' is added to "
                f"Authorized redirect URLs in your LinkedIn Developer App (Auth tab), then open authorization_url in browser."
            )

    elif platform_name == "instagram":
        effective_redirect_uri = f"{base_url}{settings.API_V1_STR}/credentials/instagram/callback"
        auth_url = InstagramService.get_authorization_url(
            client_id=payload.client_id,
            redirect_uri=effective_redirect_uri,
            state=str(company.id),
        )
        response_msg = (
            f"Instagram credentials saved in database! Make sure '{effective_redirect_uri}' is added to "
            f"Valid OAuth Redirect URIs in your Meta Developer App, then open authorization_url in browser to connect your Instagram account."
        )

    elif platform_name == "facebook":
        effective_redirect_uri = f"{base_url}{settings.API_V1_STR}/credentials/facebook/callback"
        auth_url = FacebookService.get_authorization_url(
            client_id=payload.client_id,
            redirect_uri=effective_redirect_uri,
            state=str(company.id),
        )
        response_msg = (
            f"Facebook credentials saved in database! Make sure '{effective_redirect_uri}' is added to "
            f"Valid OAuth Redirect URIs in your Meta Developer App, then open authorization_url in browser to connect your Facebook Page."
        )

    elif platform_name == "x":
        effective_redirect_uri = f"{base_url}{settings.API_V1_STR}/credentials/x/callback"
        auth_url = TwitterService.get_authorization_url(
            client_id=payload.client_id,
            redirect_uri=effective_redirect_uri,
            company_id=str(company.id),
        )
        response_msg = (
            f"X (Twitter) credentials saved in database! Make sure '{effective_redirect_uri}' is added to "
            f"the callback URI in your X Developer App (OAuth 2.0), then open authorization_url in browser to connect your account."
        )

    # Check if record already exists for this company and platform
    credential = (
        db.query(Credentials)
        .filter(
            Credentials.company_id == company.id,
            Credentials.platform == platform_name,
        )
        .first()
    )

    if credential:
        credential.client_id = payload.client_id
        credential.client_secret = payload.client_secret
        if access_token:
            credential.access_token = access_token
    else:
        credential = Credentials(
            company_id=company.id,
            platform=platform_name,
            client_id=payload.client_id,
            client_secret=payload.client_secret,
            access_token=access_token,
        )
        db.add(credential)

    db.commit()
    db.refresh(credential)

    return CredentialsResponse(
        id=credential.id,
        company_id=credential.company_id,
        platform=credential.platform,
        client_id=credential.client_id,
        access_token=credential.access_token,
        organization_id=credential.organization_id,
        message=response_msg,
        authorization_url=auth_url if not credential.access_token else None,
        created_at=credential.created_at,
        updated_at=credential.updated_at,
    )


@router.post("/post", response_model=PostResponse, summary="Publish a text or image post to LinkedIn or Instagram using stored credentials")
def create_social_post(
    request: Request,
    caption: str = Form(..., description="Caption / text of the post"),
    hashtags: Optional[str] = Form(None, description="Optional hashtags (e.g. '#tech #ai #marketing')"),
    image: Optional[UploadFile] = File(None, description="Optional image file to upload from PC (JPG, PNG, WebP, etc.)"),
    image_url: Optional[str] = Form(None, description="Optional direct public image URL (required for Instagram if no file uploaded)"),
    platform: str = Form(default="linkedin", description="Target platform (e.g. linkedin, instagram)"),
    company_id: Optional[int] = Form(None, description="Optional company ID override (defaults to current logged in user)"),
    db: Session = Depends(deps.get_db),
    auth: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
) -> Any:
    """
    Publishes a social media post to the specified platform:
    - Automatically picks company_id from current logged in user (Bearer token)
    - Takes caption and hashtags, combines them into the post content
    - Supports optional image file upload directly from PC or direct public image_url
    """
    company = resolve_company(db, auth, company_id)
    platform_name = platform.strip().lower()
    if platform_name == "twitter":
        platform_name = "x"

    credential = (
        db.query(Credentials)
        .filter(
            Credentials.company_id == company.id,
            Credentials.platform == platform_name,
        )
        .first()
    )

    if not credential:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No credentials found for platform '{platform_name}'. Please save client_id and client_secret first using POST /api/credentials/.",
        )

    # ------------------ LINKEDIN POSTING ------------------
    if platform_name == "linkedin":
        target_org_id = credential.organization_id

        # If access_token is missing, try auto-generating via 2-legged or return authorization link
        if not credential.access_token:
            verif = LinkedInService.verify_credentials(credential.client_id, credential.client_secret)
            if verif.get("access_token"):
                credential.access_token = verif["access_token"]
                db.commit()

        if not credential.access_token:
            base_url = get_request_base_url(request)
            redirect_uri = f"{base_url}{settings.API_V1_STR}/credentials/linkedin/callback"
            auth_url = LinkedInService.get_authorization_url(
                client_id=credential.client_id,
                redirect_uri=redirect_uri,
                scope=DEFAULT_MEMBER_SCOPES,
                state=str(company.id),
            )
            return PostResponse(
                success=False,
                platform=platform_name,
                message=(
                    f"Client ID aur Secret DB mein saved hain! Post karne ke liye 1-time authorization zaroori hai. "
                    f"Make sure '{redirect_uri}' is added to Authorized redirect URLs in your LinkedIn app, then open authorization_url in browser."
                ),
                authorization_url=auth_url,
            )

        # 1. Combine caption and hashtags
        full_text = caption.strip()
        if hashtags and hashtags.strip():
            tags = hashtags.strip()
            full_text = f"{full_text}\n\n{tags}"

        # 2. Upload image if provided from PC
        image_urn = None
        if image and image.filename:
            file_bytes = image.file.read()
            if len(file_bytes) > 0:
                if target_org_id:
                    clean_org = target_org_id.replace("urn:li:organization:", "").strip()
                    owner_urn = f"urn:li:organization:{clean_org}"
                else:
                    user_info = LinkedInService.get_user_info(credential.access_token)
                    member_sub = user_info.get("sub")
                    owner_urn = f"urn:li:person:{member_sub}"

                image_urn = LinkedInService.upload_image(
                    access_token=credential.access_token,
                    owner_urn=owner_urn,
                    file_bytes=file_bytes,
                    content_type=image.content_type or "image/jpeg",
                )

        # 3. Publish post
        result = LinkedInService.create_post(
            access_token=credential.access_token,
            text=full_text,
            image_urn=image_urn,
            organization_id=target_org_id,
        )

        return PostResponse(
            success=True,
            platform=platform_name,
            post_id=result.get("post_id"),
            target=result.get("target"),
            image_attached=bool(image_urn),
            message=f"Post published successfully to {result.get('target', 'LinkedIn')}! Credentials verified & working.",
        )

    # ------------------ INSTAGRAM POSTING ------------------
    elif platform_name == "instagram":
        target_ig_id = credential.organization_id

        # If access_token is missing, return authorization_url
        if not credential.access_token:
            base_url = get_request_base_url(request)
            redirect_uri = f"{base_url}{settings.API_V1_STR}/credentials/instagram/callback"
            auth_url = InstagramService.get_authorization_url(
                client_id=credential.client_id,
                redirect_uri=redirect_uri,
                state=str(company.id),
            )
            return PostResponse(
                success=False,
                platform=platform_name,
                message=(
                    f"Instagram App ID and Secret are saved, but your Instagram account is not connected yet. "
                    f"Please ensure '{redirect_uri}' is added to Valid OAuth Redirect URIs in your Meta App, "
                    f"then connect your account by opening authorization_url in browser."
                ),
                authorization_url=auth_url,
            )

        # Attempt to auto-fetch organization_id (Instagram Account ID) if not yet resolved
        if not target_ig_id:
            fetched_id, username = InstagramService.get_instagram_account_details(credential.access_token)
            if fetched_id:
                credential.organization_id = fetched_id
                db.commit()
                target_ig_id = fetched_id
            else:
                base_url = get_request_base_url(request)
                redirect_uri = f"{base_url}{settings.API_V1_STR}/credentials/instagram/callback"
                auth_url = InstagramService.get_authorization_url(
                    client_id=credential.client_id,
                    redirect_uri=redirect_uri,
                    state=str(company.id),
                )
                return PostResponse(
                    success=False,
                    platform=platform_name,
                    message=(
                        "Instagram Account ID could not be detected automatically. "
                        "Please ensure your Instagram account is a Professional (Business/Creator) account "
                        "and re-authorize using authorization_url."
                    ),
                    authorization_url=auth_url,
                )

        # Instagram strictly requires an image/media URL
        final_image_url = None
        if image_url and image_url.strip():
            final_image_url = image_url.strip()
        elif image and image.filename:
            file_bytes = image.file.read()
            if len(file_bytes) > 0:
                final_image_url = upload_library_asset(
                    file_content=file_bytes,
                    filename=image.filename,
                    content_type=image.content_type or "image/jpeg",
                )

        if not final_image_url:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Instagram strictly requires an image! Please provide an 'image' file upload or a public 'image_url'.",
            )

        # Combine caption and hashtags
        full_text = caption.strip()
        if hashtags and hashtags.strip():
            full_text = f"{full_text}\n\n{hashtags.strip()}"

        result = InstagramService.create_post(
            access_token=credential.access_token,
            instagram_account_id=target_ig_id,
            image_url=final_image_url,
            caption=full_text,
        )

        return PostResponse(
            success=True,
            platform=platform_name,
            post_id=result.get("post_id"),
            target="Instagram",
            image_attached=True,
            message=f"Post published successfully to Instagram! Post ID: {result.get('post_id')}",
        )

    # ------------------ FACEBOOK POSTING ------------------
    elif platform_name == "facebook":
        page_id = credential.organization_id

        if not credential.access_token or not page_id:
            base_url = get_request_base_url(request)
            redirect_uri = f"{base_url}{settings.API_V1_STR}/credentials/facebook/callback"
            auth_url = FacebookService.get_authorization_url(
                client_id=credential.client_id,
                redirect_uri=redirect_uri,
                state=str(company.id),
            )
            return PostResponse(
                success=False,
                platform=platform_name,
                message=(
                    f"Facebook App ID and Secret are saved, but your Facebook Page is not connected yet. "
                    f"Please ensure '{redirect_uri}' is added to Valid OAuth Redirect URIs in your Meta App, "
                    f"then connect your account by opening authorization_url in browser."
                ),
                authorization_url=auth_url,
            )

        full_text = caption.strip()
        if hashtags and hashtags.strip():
            full_text = f"{full_text}\n\n{hashtags.strip()}"

        final_image_url = None
        if image_url and image_url.strip():
            final_image_url = image_url.strip()
        elif image and image.filename:
            file_bytes = image.file.read()
            if len(file_bytes) > 0:
                final_image_url = upload_library_asset(
                    file_content=file_bytes,
                    filename=image.filename,
                    content_type=image.content_type or "image/jpeg",
                )

        result = FacebookService.create_post(
            page_access_token=credential.access_token,
            page_id=page_id,
            message=full_text,
            image_url=final_image_url,
        )

        return PostResponse(
            success=True,
            platform=platform_name,
            post_id=result.get("post_id"),
            target="Facebook Page",
            image_attached=bool(final_image_url),
            message=f"Post published successfully to Facebook! Post ID: {result.get('post_id')}",
        )

    # ------------------ X (TWITTER) POSTING ------------------
    elif platform_name == "x":
        if not credential.access_token:
            base_url = get_request_base_url(request)
            redirect_uri = f"{base_url}{settings.API_V1_STR}/credentials/x/callback"
            auth_url = TwitterService.get_authorization_url(
                client_id=credential.client_id,
                redirect_uri=redirect_uri,
                company_id=str(company.id),
            )
            return PostResponse(
                success=False,
                platform=platform_name,
                message=(
                    f"Client ID and Secret are saved in DB! Post karne ke liye 1-time authorization zaroori hai. "
                    f"Make sure '{redirect_uri}' is added to the callback URI in your X app, then open authorization_url in browser."
                ),
                authorization_url=auth_url,
            )

        if credential.token_expires_at and credential.token_expires_at <= datetime.now(timezone.utc):
            refreshed = TwitterService.refresh_access_token(
                client_id=credential.client_id,
                client_secret=credential.client_secret,
                refresh_token=credential.refresh_token,
            )
            credential.access_token = refreshed["access_token"]
            credential.refresh_token = refreshed["refresh_token"]
            credential.token_expires_at = refreshed["expires_at"]
            db.commit()

        full_text = caption.strip()
        if hashtags and hashtags.strip():
            full_text = f"{full_text}\n\n{hashtags.strip()}"

        result = TwitterService.create_post(
            access_token=credential.access_token,
            text=full_text,
        )

        return PostResponse(
            success=True,
            platform=platform_name,
            post_id=result.get("post_id"),
            target="X (Twitter)",
            image_attached=False,
            message=f"Post published successfully to X! Post ID: {result.get('post_id')}",
        )

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Platform '{platform_name}' is not supported yet.",
    )


@router.get("/linkedin/callback", summary="LinkedIn Callback endpoint that automatically exchanges code and saves token in DB")
def linkedin_callback(
    request: Request,
    code: Optional[str] = Query(None, description="Authorization code from LinkedIn"),
    error: Optional[str] = Query(None, description="Error code if user denied authorization"),
    error_description: Optional[str] = Query(None, description="Error description"),
    state: Optional[str] = Query(None, description="State containing company_id"),
    db: Session = Depends(deps.get_db),
) -> dict:
    if error:
        return {
            "status": "error",
            "error": error,
            "error_description": error_description,
        }
    if not code:
        return {
            "status": "error",
            "error": "missing_code",
            "message": "No authorization code was provided in callback.",
        }

    current_redirect_uri = get_current_callback_url(request)

    target_company_id = None
    if state:
        try:
            target_company_id = int(state)
        except ValueError:
            pass

    credential = None
    if target_company_id:
        credential = (
            db.query(Credentials)
            .filter(
                Credentials.company_id == target_company_id,
                Credentials.platform == "linkedin",
            )
            .first()
        )

    if credential:
        try:
            token_data = LinkedInService.exchange_authorization_code(
                client_id=credential.client_id,
                client_secret=credential.client_secret,
                code=code,
                redirect_uri=current_redirect_uri,
            )
            access_token = token_data.get("access_token")
            if access_token:
                credential.access_token = access_token
                db.commit()
                return {
                    "status": "success",
                    "message": "🎉 LinkedIn account successfully connected! Access token generated and saved in database.",
                    "company_id": target_company_id,
                    "platform": "linkedin",
                    "next_step": "You can now publish posts using POST /api/credentials/post.",
                }
        except Exception as ex:
            return {
                "status": "partial_success",
                "code": code,
                "warning": f"Received authorization code, but automated token exchange failed: {str(ex)}",
            }

    return {
        "status": "success",
        "code": code,
        "state": state,
        "message": "Authorization code received successfully.",
    }


@router.get("/instagram/callback", summary="Instagram Callback endpoint that automatically exchanges code and saves token in DB")
def instagram_callback(
    request: Request,
    code: Optional[str] = Query(None, description="Authorization code from Instagram/Meta"),
    error: Optional[str] = Query(None, description="Error code if user denied authorization"),
    error_reason: Optional[str] = Query(None, description="Error reason"),
    error_description: Optional[str] = Query(None, description="Error description"),
    state: Optional[str] = Query(None, description="State containing company_id"),
    db: Session = Depends(deps.get_db),
) -> dict:
    """
    Instagram OAuth Callback:
    - Receives authorization code from Meta/Instagram OAuth dialog
    - Matches credential for the company
    - Exchanges short code for short-lived token -> 60-day Long-Lived Token
    - Resolves Instagram Business Account ID and username
    - Automatically updates database record
    """
    if error:
        return {
            "status": "error",
            "error": error,
            "error_reason": error_reason,
            "error_description": error_description,
        }
    if not code:
        return {
            "status": "error",
            "error": "missing_code",
            "message": "No authorization code was provided in callback. Please initiate OAuth from authorization_url.",
        }

    # Meta sometimes appends #_ to the authorization code
    clean_code = code.split("#_")[0]
    current_redirect_uri = get_current_callback_url(request)

    target_company_id = None
    if state:
        try:
            target_company_id = int(state)
        except ValueError:
            pass

    credential = None
    if target_company_id:
        credential = (
            db.query(Credentials)
            .filter(
                Credentials.company_id == target_company_id,
                Credentials.platform == "instagram",
            )
            .first()
        )
    else:
        # Fallback to the latest saved Instagram credential
        credential = (
            db.query(Credentials)
            .filter(Credentials.platform == "instagram")
            .order_by(Credentials.updated_at.desc())
            .first()
        )
        if credential:
            target_company_id = credential.company_id

    if credential:
        try:
            token_data = InstagramService.exchange_authorization_code(
                client_id=credential.client_id,
                client_secret=credential.client_secret,
                code=clean_code,
                redirect_uri=current_redirect_uri,
            )
            access_token = token_data.get("access_token")
            ig_account_id = token_data.get("instagram_account_id")
            username = token_data.get("username")

            if access_token:
                credential.access_token = access_token
                if ig_account_id:
                    credential.organization_id = str(ig_account_id)
                db.commit()
                db.refresh(credential)

                return {
                    "status": "success",
                    "message": "🎉 Instagram account successfully connected! Long-lived access token and Instagram Account ID saved in database.",
                    "company_id": target_company_id,
                    "platform": "instagram",
                    "instagram_account_id": credential.organization_id,
                    "username": username,
                    "next_step": "You can now publish posts using POST /api/credentials/post with platform='instagram'.",
                }
        except Exception as ex:
            return {
                "status": "partial_success",
                "warning": f"Received authorization code, but automated token exchange failed: {str(ex)}",
                "company_id": target_company_id,
                "platform": "instagram",
            }

    return {
        "status": "success",
        "code": clean_code,
        "state": state,
        "message": "Authorization code received successfully, but no matching Instagram credential found in database.",
    }


@router.get("/facebook/callback", summary="Facebook Callback endpoint that automatically exchanges code and saves the Page token in DB")
def facebook_callback(
    request: Request,
    code: Optional[str] = Query(None, description="Authorization code from Facebook"),
    error: Optional[str] = Query(None, description="Error code if user denied authorization"),
    error_reason: Optional[str] = Query(None, description="Error reason"),
    error_description: Optional[str] = Query(None, description="Error description"),
    state: Optional[str] = Query(None, description="State containing company_id"),
    db: Session = Depends(deps.get_db),
) -> dict:
    """
    Facebook OAuth Callback:
    - Receives authorization code from the Meta/Facebook OAuth dialog
    - Exchanges the code for a User token, upgrades it to a Long-Lived token
    - Resolves the first managed Facebook Page and its permanent Page Access Token
    - Saves the Page token + Page ID to the database
    """
    if error:
        return {
            "status": "error",
            "error": error,
            "error_reason": error_reason,
            "error_description": error_description,
        }
    if not code:
        return {
            "status": "error",
            "error": "missing_code",
            "message": "No authorization code was provided in callback. Please initiate OAuth from authorization_url.",
        }

    current_redirect_uri = get_current_callback_url(request)

    target_company_id = None
    if state:
        try:
            target_company_id = int(state)
        except ValueError:
            pass

    credential = None
    if target_company_id:
        credential = (
            db.query(Credentials)
            .filter(
                Credentials.company_id == target_company_id,
                Credentials.platform == "facebook",
            )
            .first()
        )

    if credential:
        try:
            token_data = FacebookService.exchange_authorization_code(
                client_id=credential.client_id,
                client_secret=credential.client_secret,
                code=code,
                redirect_uri=current_redirect_uri,
            )
            credential.access_token = token_data.get("access_token")
            credential.organization_id = token_data.get("page_id")
            db.commit()
            db.refresh(credential)

            return {
                "status": "success",
                "message": "🎉 Facebook Page successfully connected! Page access token saved in database.",
                "company_id": target_company_id,
                "platform": "facebook",
                "page_id": credential.organization_id,
                "page_name": token_data.get("page_name"),
                "next_step": "You can now publish posts using POST /api/credentials/post with platform='facebook'.",
            }
        except Exception as ex:
            return {
                "status": "partial_success",
                "code": code,
                "warning": f"Received authorization code, but automated token exchange failed: {str(ex)}",
                "company_id": target_company_id,
                "platform": "facebook",
            }

    return {
        "status": "success",
        "code": code,
        "state": state,
        "message": "Authorization code received successfully, but no matching Facebook credential found in database.",
    }


@router.get("/x/callback", summary="X (Twitter) Callback endpoint that automatically exchanges code and saves token in DB")
def x_callback(
    request: Request,
    code: Optional[str] = Query(None, description="Authorization code from X"),
    error: Optional[str] = Query(None, description="Error code if user denied authorization"),
    state: Optional[str] = Query(None, description="State containing company_id and PKCE code_verifier"),
    db: Session = Depends(deps.get_db),
) -> dict:
    """
    X (Twitter) OAuth 2.0 (PKCE) Callback:
    - Recovers company_id and code_verifier from the opaque state param
    - Exchanges the authorization code for an access/refresh token pair
    - Saves the tokens (and their expiry) to the database
    """
    if error:
        return {"status": "error", "error": error}
    if not code:
        return {
            "status": "error",
            "error": "missing_code",
            "message": "No authorization code was provided in callback. Please initiate OAuth from authorization_url.",
        }

    target_company_id, code_verifier = TwitterService.parse_state(state or "")
    if not target_company_id or not code_verifier:
        return {
            "status": "error",
            "error": "invalid_state",
            "message": "Missing or invalid state param. Please initiate OAuth from authorization_url again.",
        }

    current_redirect_uri = get_current_callback_url(request)

    credential = (
        db.query(Credentials)
        .filter(
            Credentials.company_id == int(target_company_id),
            Credentials.platform == "x",
        )
        .first()
    )

    if credential:
        try:
            token_data = TwitterService.exchange_authorization_code(
                client_id=credential.client_id,
                client_secret=credential.client_secret,
                code=code,
                redirect_uri=current_redirect_uri,
                code_verifier=code_verifier,
            )
            credential.access_token = token_data.get("access_token")
            credential.refresh_token = token_data.get("refresh_token")
            credential.token_expires_at = token_data.get("expires_at")
            db.commit()
            db.refresh(credential)

            return {
                "status": "success",
                "message": "🎉 X (Twitter) account successfully connected! Access token saved in database.",
                "company_id": int(target_company_id),
                "platform": "x",
                "username": token_data.get("username"),
                "next_step": "You can now publish posts using POST /api/credentials/post with platform='x'.",
            }
        except Exception as ex:
            return {
                "status": "partial_success",
                "code": code,
                "warning": f"Received authorization code, but automated token exchange failed: {str(ex)}",
                "company_id": int(target_company_id),
                "platform": "x",
            }

    return {
        "status": "success",
        "code": code,
        "state": state,
        "message": "Authorization code received successfully, but no matching X credential found in database.",
    }
