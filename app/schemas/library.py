from typing import List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field

MediaTypeLiteral = Literal["photo", "video", "article"]


class LibraryBase(BaseModel):
    name: str = Field(..., description="Display name for the asset", examples=["Product launch video"])
    type: str = Field(..., description="Library category selected by the user", examples=["Robotics"])
    media_type: MediaTypeLiteral = Field(..., description="Asset kind: photo, video, or article")


class LibraryCreate(LibraryBase):
    pass


class LibraryUpdate(BaseModel):
    name: Optional[str] = Field(None, description="Display name for the asset")
    type: Optional[str] = Field(None, description="Library category selected by the user")
    media_type: Optional[MediaTypeLiteral] = Field(None, description="Asset kind: photo, video, or article")


class LibraryItemResponse(BaseModel):
    id: str
    name: str
    type: str
    media_type: str
    media_url: Optional[str] = None
    size: Optional[int] = None
    size_kb: float = 0.0
    has_embedding: bool = False
    embedding: Optional[List[float]] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class LibraryListResponse(BaseModel):
    items: List[LibraryItemResponse]
    total_assets: int
    total_photo: int
    total_article: int
    total_video: int
    total_storage_bytes: int
    total_storage_kb: float
    total_storage_mb: float


class LibraryDeleteResponse(BaseModel):
    message: str
    id: str
