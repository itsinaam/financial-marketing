from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.sql import func

from app.core.database import Base


class SupportRequest(Base):
    """
    One message sent through the Support form.

    Separate from the Support chat: this is a one-off request that the Super
    Admin works through as a list, not a running conversation. The name and
    email are kept as the sender typed them rather than read back from the
    company, because the form lets them be changed - somebody may ask to be
    answered on a different address.
    """

    __tablename__ = "support_requests"

    id = Column(Integer, primary_key=True, index=True)
    # Who was logged in when it was sent. Nullable so a request outlives its company.
    company_id = Column(Integer, ForeignKey("companies.id", ondelete="SET NULL"), nullable=True, index=True)
    name = Column(String(255), nullable=False)
    email = Column(String(255), nullable=False)
    message = Column(Text, nullable=False)
    # The request is stored first and emailed after, so a mail outage loses the
    # notification but never the request itself.
    email_sent = Column(Boolean, nullable=False, default=False, server_default="false")
    # "open" until somebody has dealt with it, then "closed".
    status = Column(String(20), nullable=False, default="open", server_default="open", index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)


class SupportRequestEvent(Base):
    """
    One thing that happened to a support request.

    The request row only carries where it ended up; this is how it got there -
    when it arrived, whether the notifications went out, and every time somebody
    opened or closed it, so the detail page can show the whole story rather than
    just the current status.
    """

    __tablename__ = "support_request_events"

    id = Column(Integer, primary_key=True, index=True)
    request_id = Column(
        Integer,
        ForeignKey("support_requests.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # created | support_emailed | support_email_failed | closed | reopened
    # | resolved_emailed | resolved_email_failed
    kind = Column(String(40), nullable=False)
    # Free text for the one line the timeline shows under the event.
    detail = Column(Text, nullable=True)
    # Who did it, when it was a person rather than the system.
    actor = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
