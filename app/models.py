from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


class OAuthToken(Base):
    __tablename__ = "oauth_token"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64))
    access_token: Mapped[str] = mapped_column(Text)
    refresh_token: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    scopes: Mapped[str] = mapped_column(String(512), default="")


class ShopProfile(Base):
    __tablename__ = "shop_profile"

    shop_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shop_name: Mapped[str] = mapped_column(String(255), default="")
    currency_code: Mapped[str] = mapped_column(String(8), default="USD")
    shipping_profile_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    readiness_state_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    shipping_profiles_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    processing_profiles_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    shop_sections_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    default_who_made: Mapped[str] = mapped_column(String(32), default="i_did")
    default_when_made: Mapped[str] = mapped_column(String(32), default="made_to_order")
    default_is_supply: Mapped[bool] = mapped_column(default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    @property
    def ready_for_physical(self) -> bool:
        return bool(self.shipping_profile_id and self.readiness_state_id)


class Product(Base):
    __tablename__ = "product"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shop_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    name: Mapped[str] = mapped_column(String(255), default="")
    listing_type: Mapped[str] = mapped_column(String(16), default="physical")
    price: Mapped[str] = mapped_column(String(32), default="0.00")
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    who_made: Mapped[str] = mapped_column(String(32), default="i_did")
    when_made: Mapped[str] = mapped_column(String(32), default="made_to_order")
    is_supply: Mapped[bool] = mapped_column(default=False)

    taxonomy_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    taxonomy_path: Mapped[str] = mapped_column(String(512), default="")

    title: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    materials: Mapped[list[str]] = mapped_column(JSON, default=list)

    images: Mapped[list[str]] = mapped_column(JSON, default=list)
    digital_file: Mapped[str | None] = mapped_column(Text, nullable=True)

    variations: Mapped[list[dict]] = mapped_column(JSON, default=list)
    variants: Mapped[list[dict]] = mapped_column(JSON, default=list)

    status: Mapped[str] = mapped_column(String(32), default="draft")
    etsy_listing_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")

    logs: Mapped[list[SubmissionLog]] = relationship(
        back_populates="product", cascade="all, delete-orphan", order_by="SubmissionLog.id"
    )

    def tag_list(self) -> list[str]:
        return self.tags if isinstance(self.tags, list) else []

    def material_list(self) -> list[str]:
        return self.materials if isinstance(self.materials, list) else []

    def image_list(self) -> list[str]:
        return self.images if isinstance(self.images, list) else []

    def variations_list(self) -> list[dict]:
        return self.variations if isinstance(self.variations, list) else []

    def variants_list(self) -> list[dict]:
        return self.variants if isinstance(self.variants, list) else []

    def to_summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "taxonomy_path": self.taxonomy_path,
            "price": self.price,
            "quantity": self.quantity,
            "status": self.status,
            "etsy_listing_id": self.etsy_listing_id,
            "listing_type": self.listing_type,
            "error": self.error,
        }


class SubmissionLog(Base):
    __tablename__ = "submission_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("product.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    step: Mapped[str] = mapped_column(String(64), default="")
    detail: Mapped[str] = mapped_column(Text, default="")

    product: Mapped[Product] = relationship(back_populates="logs")

    @classmethod
    def record(cls, session, product_id: int, step: str, detail: str = "") -> None:
        session.add(cls(product_id=product_id, step=step, detail=detail))


def get_engine():
    engine = create_engine(
        settings.db_url,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return engine


engine = get_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def load_json(value) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value
