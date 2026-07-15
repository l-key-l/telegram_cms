from __future__ import annotations

from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models


class User(AbstractUser):
    class Role(models.TextChoices):
        MASTER = "MASTER", "主账号"
        SUB = "SUB", "子账号"

    role = models.CharField(max_length=16, choices=Role.choices, default=Role.SUB, db_index=True)
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="subaccounts",
    )
    display_name = models.CharField("昵称", max_length=80, blank=True)
    force_password_change = models.BooleanField(default=False)
    created_by = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_users",
    )

    class Meta:
        indexes = [models.Index(fields=["parent", "role", "is_active"])]

    def clean(self):
        super().clean()
        if self.role == self.Role.MASTER and self.parent_id:
            raise ValidationError({"parent": "主账号不能隶属于其他账号。"})
        if self.role == self.Role.SUB:
            if self.parent_id and self.parent.role != self.Role.MASTER:
                raise ValidationError({"parent": "子账号只能隶属于主账号。"})
            if self.pk is not None and self.parent_id == self.pk:
                raise ValidationError({"parent": "账号不能隶属于自己。"})

    @property
    def label(self) -> str:
        return self.display_name or self.username

    @property
    def is_master(self) -> bool:
        return self.role == self.Role.MASTER

    @property
    def is_subaccount(self) -> bool:
        return self.role == self.Role.SUB

    def __str__(self):
        return self.label
