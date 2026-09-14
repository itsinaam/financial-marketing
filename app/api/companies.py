from typing import Any, List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core import deps
from app.core import security
from app.models.companies import Company,Role
from app.schemas.companies import UserCreate, UserResponse, UserDeleteResponse

router = APIRouter()

@router.get(
    "/",
    response_model=List[UserResponse],
    dependencies=[Depends(deps.get_current_superadmin)],
    summary="List all companies (Admin or Super Admin)"
)
def read_companies(
    db: Session = Depends(deps.get_db),
    skip: int = 0,
    limit: int = 100,
) -> Any:
    """
    Retrieve all companies. Admin or Super Admin privilege required.
    """
    companies = db.query(Company).offset(skip).limit(limit).all()
    return companies

@router.post(
    "/",
    response_model=UserResponse,
    dependencies=[Depends(deps.get_current_superadmin)],
    status_code=status.HTTP_201_CREATED,
    summary="Create company (Super Admin only)"
)
def create_company(
    *,
    db: Session = Depends(deps.get_db),
    user_in: UserCreate,
) -> Any:
    """
    Create a new company. Super Admin privilege required.
    """
    existing_user = db.query(Company).filter(Company.email == user_in.email).first()
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Company/User with this email already exists.",
        )
    company_name = user_in.name or user_in.full_name or "Company"
    company = Company(
        email=user_in.email,
        hashed_password=security.get_password_hash(user_in.password),
        name=company_name,
        role=user_in.role,
        is_active=user_in.is_active,
        is_superuser=(user_in.role == Role.SUPERADMIN),
    )
    db.add(company)
    db.commit()
    db.refresh(company)
    return company

@router.get(
    "/{company_id}",
    response_model=UserResponse,
    summary="Get company by ID (Super Admin or Self)"
)
def read_company_by_id(
    company_id: int,
    db: Session = Depends(deps.get_db),
    current_user: Company = Depends(deps.get_current_user),
) -> Any:
    """
    Get company by ID. Available to Super Admin or the company themselves.
    """
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Company not found",
        )
    if current_user.id != company.id and current_user.role != Role.SUPERADMIN and not current_user.is_superuser:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The user does not have enough privileges",
        )
    return company

@router.delete(
    "/{company_id}",
    response_model=UserDeleteResponse,
    summary="Delete company by ID (Admin or Super Admin only)"
)
def delete_company(
    company_id: int,
    db: Session = Depends(deps.get_db),
    current_user: Company = Depends(deps.get_current_user),
) -> Any:
    """
    Delete a company by ID. Only accessible by Admin or Super Admin.
    """
    target_company = db.query(Company).filter(Company.id == company_id).first()
    if not target_company:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Company not found",
        )

    if current_user.id == target_company.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot delete your own account",
        )

    # Protect Super Admin from deletion by standard Admin
    if (target_company.role == Role.SUPERADMIN or target_company.is_superuser) and (current_user.role != Role.SUPERADMIN and not current_user.is_superuser):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin cannot delete a Super Admin user",
        )

    db.delete(target_company)
    db.commit()
    return {
        "message": f"Company '{target_company.email}' (ID: {company_id}) has been deleted successfully",
        "deleted_user_id": company_id,
    }
