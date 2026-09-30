from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey
from sqlalchemy.sql import func

from app.core.database import Base


class SupportMessage(Base):
    """
    One message in a company's Support chat.

    Every company has exactly one thread and the other side of it is always the
    Super Admin, so the company being talked about identifies the thread and no
    separate conversation table is needed. `sender_role` says which side wrote
    the message, and `read_at` is stamped when the *other* side has seen it.
    """

    __tablename__ = "support_messages"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(
        Integer,
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Who wrote it. Kept nullable so a message survives its author being deleted.
    sender_id = Column(Integer, ForeignKey("companies.id"), nullable=True)
    sender_role = Column(String(20), nullable=False)  # "company" | "superadmin"
    body = Column(Text, nullable=False)
    read_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
