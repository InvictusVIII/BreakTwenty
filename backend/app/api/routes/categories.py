from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes import CurrentUser, get_current_user, get_db
from app.services.categories import (
    VALID_CLASSIFICATIONS,
    create_category,
    delete_category,
    list_categories_for_user,
    reset_category_to_seed,
    serialize_category,
    update_category,
)

router = APIRouter()


class CategoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=80)
    parent_id: int | None = None
    icon: str | None = None
    icon_set: str | None = None
    color_dark: str | None = None
    color_light: str | None = None
    # Optional: when a subcategory is created without one, the service inherits
    # the parent group's classification (mirrors the create-leaf UI). A
    # top-level group must still declare its classification.
    classification: str | None = None


class CategoryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(None, min_length=1, max_length=80)
    icon: str | None = None
    icon_set: str | None = None
    color_dark: str | None = None
    color_light: str | None = None
    classification: str | None = None


@router.get("/categories")
async def list_categories(
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    rows = await list_categories_for_user(db, current_user.id)
    return {
        "categories": [serialize_category(c) for c in rows],
        "classifications": sorted(VALID_CLASSIFICATIONS),
    }


@router.post("/categories")
async def create_category_endpoint(
    payload: CategoryCreate,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    category = await create_category(
        db,
        current_user.id,
        name=payload.name,
        parent_id=payload.parent_id,
        icon=payload.icon,
        icon_set=payload.icon_set,
        color_dark=payload.color_dark,
        color_light=payload.color_light,
        classification=payload.classification,
    )
    await db.commit()
    return serialize_category(category)


@router.patch("/categories/{category_id}")
async def update_category_endpoint(
    category_id: int,
    payload: CategoryUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    category = await update_category(
        db,
        current_user.id,
        category_id,
        name=payload.name,
        icon=payload.icon,
        icon_set=payload.icon_set,
        color_dark=payload.color_dark,
        color_light=payload.color_light,
        classification=payload.classification,
    )
    await db.commit()
    return serialize_category(category)


@router.delete("/categories/{category_id}")
async def delete_category_endpoint(
    category_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    await delete_category(db, current_user.id, category_id)
    await db.commit()
    return {"deleted": True}


@router.post("/categories/{category_id}/reset-to-seed")
async def reset_category(
    category_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    category = await reset_category_to_seed(db, current_user.id, category_id)
    await db.commit()
    return serialize_category(category)
