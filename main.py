import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api.router import api_router
from app.core.config import settings
from app.core.database import Base, engine, SessionLocal
from app.db.init_db import init_db
from app.core.plans import seed_plans
from app.models import companies, credentials, library, payment, post, blog, notification, brand, plan  # noqa: F401 - register models on Base
from app.services.scheduler_service import scheduled_post_checker_loop


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application Lifespan Event Handler.
    Creates database tables, seeds default Super Admin, and starts the
    scheduled-post auto-publish background loop.
    """
    scheduler_task = asyncio.create_task(scheduled_post_checker_loop())
    try:
        # 1. Ensure DB tables exist
        Base.metadata.create_all(bind=engine)

        # 1b. Add columns introduced after these tables were first created
        with engine.begin() as connection:
            connection.execute(text(
                "ALTER TABLE credentials ADD COLUMN IF NOT EXISTS refresh_token TEXT"
            ))
            connection.execute(text(
                "ALTER TABLE credentials ADD COLUMN IF NOT EXISTS token_expires_at TIMESTAMPTZ"
            ))
            connection.execute(text(
                "ALTER TABLE generated_posts ADD COLUMN IF NOT EXISTS company_id INTEGER REFERENCES companies(id)"
            ))
            connection.execute(text(
                "ALTER TABLE generated_posts ALTER COLUMN created_at SET DEFAULT now()"
            ))
            connection.execute(text(
                "ALTER TABLE generated_posts ALTER COLUMN updated_at SET DEFAULT now()"
            ))
            connection.execute(text(
                "ALTER TABLE libraryy ADD COLUMN IF NOT EXISTS company_id INTEGER REFERENCES companies(id)"
            ))
            connection.execute(text(
                "ALTER TABLE companies ADD COLUMN IF NOT EXISTS first_name VARCHAR(100)"
            ))
            connection.execute(text(
                "ALTER TABLE companies ADD COLUMN IF NOT EXISTS last_name VARCHAR(100)"
            ))
            connection.execute(text(
                "ALTER TABLE companies ADD COLUMN IF NOT EXISTS avatar_url VARCHAR(500)"
            ))
            connection.execute(text(
                "ALTER TABLE generated_posts ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ"
            ))
            connection.execute(text(
                "ALTER TABLE generated_blogs ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ"
            ))
            connection.execute(text(
                "ALTER TABLE generated_blogs ADD COLUMN IF NOT EXISTS published_url VARCHAR(500)"
            ))
            connection.execute(text(

                "ALTER TABLE payments ADD COLUMN IF NOT EXISTS plan_code VARCHAR(30)"
            ))
            connection.execute(text(
                "ALTER TABLE payments ADD COLUMN IF NOT EXISTS billing_period VARCHAR(10)"
            ))

        # 2. Seed default Super Admin user
        db = SessionLocal()
        try:
            init_db(db)
            seed_plans(db)
        finally:
            db.close()
    except Exception as exc:
        print(f"Startup DB init notice: {exc}")

    yield

    scheduler_task.cancel()



app = FastAPI(
    title=settings.APP_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    lifespan=lifespan,
)

# Set CORS middleware allowing all frontend domains (Vercel, Vite, Next.js, localhost)
origins = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "https://financial-markett.vercel.app",
    "https://financial-marketing.vercel.app",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_origin_regex=r"^https?://.*$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
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



