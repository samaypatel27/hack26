from __future__ import annotations

from recordkit import Model


class Address(Model):
    street: str
    city: str
    zip_code: str


class Customer(Model):
    email: str
    name: str
    address: Address | None = None
    tags: list = []
