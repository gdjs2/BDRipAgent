"""Authenticated global prompt settings with optimistic concurrency control."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from shared.agent_prompts import CATALOG, MAX_PROMPT_LENGTH, default_text, snapshot
from shared.db import session
from shared.models import AgentPrompt, now

router = APIRouter()


class SavePrompt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0, strict=True)
    text: str | None = Field(max_length=MAX_PROMPT_LENGTH)

    @field_validator("text")
    @classmethod
    def content(cls, value):
        if value is not None and (not value.strip() or "\x00" in value):
            raise ValueError("Enter a non-empty prompt without null characters")
        return value


def describe(key, row=None):
    title, description = CATALOG[key]
    return {
        **snapshot(key, row),
        "title": title,
        "description": description,
        "default_text": default_text(key),
        "customized": row is not None and row.text is not None,
        "updated_at": row.updated_at if row is not None else None,
    }


@router.get("/agent/prompts")
def list_prompts():
    with session() as db:
        return [describe(key, db.get(AgentPrompt, key)) for key in CATALOG]


@router.put("/agent/prompts/{key}")
def save_prompt(key: str, body: SavePrompt):
    default_text(key)  # Validate the catalog key before any database mutation.
    conflict = HTTPException(
        409, "This prompt changed in another window. Load the saved version before saving."
    )
    with session() as db:
        try:
            if body.revision == 0:
                db.add(AgentPrompt(key=key, text=body.text, revision=1, updated_at=now()))
                db.flush()
            else:
                changed = db.execute(
                    update(AgentPrompt)
                    .where(AgentPrompt.key == key, AgentPrompt.revision == body.revision)
                    .values(text=body.text, revision=body.revision + 1, updated_at=now())
                )
                if changed.rowcount != 1:
                    raise conflict
            db.commit()
        except IntegrityError:
            db.rollback()
            raise conflict from None
        return describe(key, db.get(AgentPrompt, key))
