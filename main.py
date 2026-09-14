from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.core.config import settings
from app.core.database import Base, engine, SessionLocal
from app.db.init_db import init_db
import os
from fastapi.staticfiles import StaticFiles

# Ensure uploads directory exists (use /tmp on Vercel serverless)
UPLOAD_DIR = "/tmp/uploads" if os.environ.get("VERCEL") else "uploads"
try:
    os.makedirs(UPLOAD_DIR, exist_ok=True)
except Exception:
    pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application Lifespan Event Handler.
    Creates database tables and seeds default Super Admin on startup.
    """
    try:
        # 1. Ensure DB tables exist
        Base.metadata.create_all(bind=engine)
        
        # 2. Seed default Super Admin user
        db = SessionLocal()
        try:
            init_db(db)
        finally:
            db.close()
    except Exception as exc:
        print(f"Startup DB init notice: {exc}")
        
    yield



app = FastAPI(
    title=settings.APP_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    lifespan=lifespan,
)

# Mount public uploads for image hosting (used by Instagram API)
if os.path.exists(UPLOAD_DIR):
    app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

# Set CORS middleware allowing Vite (5173), Next.js (3000), and all local ports
origins = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "https://financial-marketing.vercel.app",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:[0-9]+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register API v1 router
app.include_router(api_router, prefix=settings.API_V1_STR)

@app.get("/", tags=["Health Check"])
def root():
    return {
        "message": f"Welcome to {settings.APP_NAME}",
        "docs": "/docs",
        "api_v1": settings.API_V1_STR
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)



