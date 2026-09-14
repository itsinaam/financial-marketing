from sqlalchemy.orm import Session
from app.core.config import settings
from app.core.security import get_password_hash
from app.models.companies import Company,Role

def init_db(db: Session) -> None:
    """
    Seeds initial database data, creating the default Super Admin if it does not exist.
    """
    user = db.query(Company).filter(Company.email == settings.SUPERADMIN_EMAIL).first()
    if not user:
        superadmin_user = Company(
            email=settings.SUPERADMIN_EMAIL,
            hashed_password=get_password_hash(settings.SUPERADMIN_PASSWORD),
            name="Super Admin",
            role=Role.SUPERADMIN,
            is_superuser=True,
            is_active=True,
        )
        db.add(superadmin_user)
        db.commit()
        db.refresh(superadmin_user)
        print(f"Super Admin created: {settings.SUPERADMIN_EMAIL}")
    else:
        print(f"Super Admin already exists: {settings.SUPERADMIN_EMAIL}")
