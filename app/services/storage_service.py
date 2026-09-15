import os
import uuid
from pathlib import Path
from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
BUCKET_NAME = os.getenv("SUPABASE_BUCKET", "products-images")

supabase: Client | None = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception:
        supabase = None


def get_supabase_client() -> Client:
    """Return an active Supabase client or initialize one."""
    global supabase
    if supabase is not None:
        return supabase
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    if not url or not key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_KEY environment variables are not set.")
    supabase = create_client(url, key)
    return supabase


def upload_library_asset(
    file_content: bytes, filename: str = "media", content_type: str = "application/octet-stream"
) -> str:
    """
    Upload a library asset to Supabase storage and return the public URL.

    Args:
        file_content: The binary content of the uploaded file
        filename: Original filename (will be made unique)
        content_type: MIME type of the uploaded file

    Returns:
        Public URL of the uploaded image

    Raises:
        Exception: If upload fails
    """
    try:
        client = get_supabase_client()

        # Generate a unique filename to avoid collisions
        file_extension = Path(filename).suffix
        unique_filename = f"{uuid.uuid4()}{file_extension}"

        # Upload to Supabase storage
        client.storage.from_(BUCKET_NAME).upload(
            path=unique_filename,
            file=file_content,
            file_options={
                "content-type": content_type
            },
        )

        # Get public URL
        public_url = client.storage.from_(BUCKET_NAME).get_public_url(unique_filename)
        return public_url

    except Exception as e:
        raise Exception(f"Failed to upload library asset: {str(e)}")
