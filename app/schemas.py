"""Request validation. Unknown fields are rejected and every field is bounded."""

from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.matching import EDUCATION_LEVELS

IdStr = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginRequest(Strict):
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = Field(min_length=1, max_length=128)


class RefreshRequest(Strict):
    refresh_token: str = Field(min_length=20, max_length=200)


class HardPreferences(Strict):
    seeking_gender: Optional[Literal["male", "female"]] = None
    age_min: Optional[int] = Field(default=None, ge=18, le=80)
    age_max: Optional[int] = Field(default=None, ge=18, le=80)
    religions: Optional[list[Literal["hindu", "muslim", "sikh", "christian", "jain", "parsi", "none"]]] = Field(default=None, max_length=7)
    diets: Optional[list[Literal["vegetarian", "non_vegetarian", "vegan", "eggetarian", "jain"]]] = Field(default=None, max_length=5)
    smoking: Optional[list[Literal["never", "occasionally", "regularly"]]] = Field(default=None, max_length=3)
    wants_children: Optional[list[Literal["yes", "no", "open"]]] = Field(default=None, max_length=3)
    cities: Optional[list[str]] = Field(default=None, max_length=20)

    @model_validator(mode="after")
    def _age_order(self):
        if self.age_min is not None and self.age_max is not None and self.age_min > self.age_max:
            raise ValueError("age_min must not exceed age_max")
        return self


class SoftPreference(Strict):
    key: Literal["min_height_cm", "max_height_cm", "min_education", "professions", "drinking"]
    value: Union[int, str, list[str]]
    weight: int = Field(ge=1, le=5)

    @model_validator(mode="after")
    def _value_type(self):
        if self.key in ("min_height_cm", "max_height_cm") and not isinstance(self.value, int):
            raise ValueError("height must be a number")
        if self.key == "min_education" and self.value not in EDUCATION_LEVELS:
            raise ValueError("unknown education level")
        if self.key in ("professions", "drinking") and not isinstance(self.value, list):
            raise ValueError("value must be a list")
        return self


class PreferenceCardUpdate(Strict):
    hard: HardPreferences
    soft: list[SoftPreference] = Field(max_length=10)


class CheckRequest(Strict):
    client_id: IdStr
    candidate_id: IdStr


class ShareRequest(Strict):
    client_id: IdStr
    candidate_id: IdStr
    override_reason: Optional[str] = Field(default=None, min_length=5, max_length=500)


class OutcomeRequest(Strict):
    outcome: Literal["accepted", "rejected"]
    feedback: Optional[str] = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _feedback_for_rejection(self):
        if self.outcome == "rejected" and not (self.feedback and self.feedback.strip()):
            raise ValueError("feedback is required when a profile is rejected")
        return self
