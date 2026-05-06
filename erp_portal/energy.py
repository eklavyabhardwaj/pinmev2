from django.contrib.auth import get_user_model
from .models import EnergyPointTransaction

def award_points(user, points, reason, meta=None):
    """Safe: ignore if anon or user is None; returns transaction or None."""
    if not user or not getattr(user, "is_authenticated", False):
        return None
    return EnergyPointTransaction.objects.create(
        user=user, points=int(points), reason=reason, meta=(meta or {})
    )
