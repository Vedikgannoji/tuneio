from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

Name = Annotated[str, Field(min_length=1, max_length=100, pattern=r'\S')]
Email = Annotated[str, Field(max_length=254, pattern=r'^[^\s@]+@[^\s@]+\.[^\s@]+$')]
Positive = Annotated[int, Field(strict=True, gt=0, le=9223372036854775807)]
Money = Annotated[int, Field(strict=True, gt=0, le=1000000000)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class UserCreate(StrictModel):
    name: Name
    email: Email


class ProductCreate(StrictModel):
    name: Name
    price_cents: Money


class Patch(StrictModel):
    @model_validator(mode='after')
    def nonempty(self):
        if not self.model_fields_set or any(getattr(self, f) is None for f in self.model_fields_set):
            raise ValueError('Provide at least one non-null field')
        return self


class UserPatch(Patch):
    name: Name | None = None
    email: Email | None = None


class ProductPatch(Patch):
    name: Name | None = None
    price_cents: Money | None = None


class OrderCreate(StrictModel):
    user_id: Positive
    product_id: Positive
    quantity: Annotated[int, Field(strict=True, gt=0, le=10000)]


class OrderStatus(StrictModel):
    status: Literal['pending', 'paid', 'shipped', 'cancelled']


class CacheMode(StrictModel):
    mode: Literal['off', 'adaptive']
