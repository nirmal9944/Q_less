from django.conf import settings
from django.db import models

from core.models import BaseModel
from organizations.models import Department, Organization


class StaffProfile(BaseModel):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='staff_profile',
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='staff_members',
    )
    department = models.ForeignKey(
        Department,
        on_delete=models.SET_NULL,
        related_name='staff_members',
        null=True,
        blank=True,
    )
    employee_id = models.CharField(max_length=30, blank=True)
    counter_number = models.CharField(max_length=20, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['user__first_name']
        indexes = [
            models.Index(fields=['organization', 'is_active']),
        ]

    def __str__(self):
        return f'{self.user.get_full_name() or self.user.username} — {self.organization.name}'
