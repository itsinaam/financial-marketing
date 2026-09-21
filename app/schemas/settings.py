from pydantic import BaseModel, Field, model_validator

MIN_PASSWORD_LENGTH = 8


class ProfileResponse(BaseModel):
    id: int
    first_name: str
    last_name: str
    full_name: str
    email: str
    avatar_url: str | None = None
    role: str


class ProfileUpdateRequest(BaseModel):
    first_name: str = Field(..., min_length=1, max_length=100)
    last_name: str = Field(..., min_length=1, max_length=100)


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=MIN_PASSWORD_LENGTH)
    confirm_password: str = Field(..., min_length=1)

    @model_validator(mode="after")
    def passwords_match(self) -> "ChangePasswordRequest":
        if self.new_password != self.confirm_password:
            raise ValueError("New password and confirm password do not match.")
        return self


class MessageResponse(BaseModel):
    message: str
