from fastapi import APIRouter
from app.api import auth, companies, payments, credentials

api_router = APIRouter()
api_router.include_router(auth.router, prefix="/auth", tags=["Authentication"])
api_router.include_router(companies.router, prefix="/company", tags=["Company"])
api_router.include_router(payments.router, prefix="/payments", tags=["Payments"])
api_router.include_router(credentials.router, prefix="/credentials", tags=["Credentials"])

