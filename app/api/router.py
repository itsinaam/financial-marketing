from fastapi import APIRouter
from app.api import auth, companies, payments, credentials, library, posts, blogs, approvals, calendar, planner, settings

api_router = APIRouter()
api_router.include_router(auth.router, prefix="/auth", tags=["Authentication"])
api_router.include_router(companies.router, prefix="/company", tags=["Company"])
api_router.include_router(payments.router, prefix="/payments", tags=["Payments"])
api_router.include_router(credentials.router, prefix="/credentials", tags=["Credentials"])
api_router.include_router(library.router, prefix="/library", tags=["Library"])
api_router.include_router(posts.router, prefix="/posts", tags=["Posts"])
api_router.include_router(blogs.router, prefix="/blogs", tags=["Blogs"])
api_router.include_router(approvals.router, prefix="/approval-queue", tags=["Approval Queue"])
api_router.include_router(calendar.router, prefix="/calendar", tags=["Calendar"])
api_router.include_router(planner.router, prefix="/planner", tags=["Planner"])
api_router.include_router(settings.router, prefix="/settings", tags=["Settings"])

