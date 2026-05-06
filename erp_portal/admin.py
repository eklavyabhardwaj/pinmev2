# admin.py
from django.utils import timezone
from django.conf import settings
import requests
from urllib.parse import quote
from .energy import award_points
from .models import CustomerLocationUpdate
from .models import UserMergeHistory
from django.contrib.auth import get_user_model
from django.db import transaction
from django.contrib import admin, messages
from .models import EnergyPointTransaction, OpportunityMeetingDetail
from django.contrib.auth.admin import UserAdmin as DefaultUserAdmin



@admin.action(description="Merge selected users (same person)")
def merge_users(request, queryset):
    if queryset.count() != 2:
        messages.error(request, "Select exactly two users to merge.")
        return

    users = list(queryset)
    primary, secondary = users[0], users[1]

    # Prevent reverse or repeated merge
    if UserMergeHistory.objects.filter(
        primary_user=primary, secondary_user=secondary
    ).exists() or UserMergeHistory.objects.filter(
        primary_user=secondary, secondary_user=primary
    ).exists():
        messages.info(request, f"These users have already been merged before.")
        return

    with transaction.atomic():
        # Migrate related models
        EnergyPointTransaction.objects.filter(user=secondary).update(user=primary)
        OpportunityMeetingDetail.objects.filter(user=secondary).update(user=primary)


        secondary.is_active = False
        secondary.username = f"{secondary.username}_merged_{secondary.id}"
        secondary.save()


        UserMergeHistory.objects.create(
            primary_user=primary,
            secondary_user=secondary
        )

    messages.success(
        request,
        f"Merged '{secondary}' into '{primary}'. History recorded."
    )


User = get_user_model()
admin.site.unregister(User)

@admin.register(User)
class CustomUserAdmin(DefaultUserAdmin):
    actions = [merge_users]

FRAPPE_BASE_URL = getattr(settings, "FRAPPE_BASE_URL", "https://erpv14.electrolabgroup.com")

def _push_to_erp(customer: str, lat: float, lon: float):
    sess = requests.Session()
    token = getattr(settings, "FRAPPE_API_TOKEN", None)
    if token:
        sess.headers.update({"Authorization": f"token {token}"})
    url = f"{FRAPPE_BASE_URL}/api/resource/Customer/{quote(customer)}"
    payload = {"custom_latitude": lat, "custom_longitude": lon}
    return sess.put(url, json=payload, timeout=15)

@admin.register(CustomerLocationUpdate)
class CustomerLocationUpdateAdmin(admin.ModelAdmin):
    list_display  = ("id","customer","latitude","longitude","status","requested_at","reviewed_at","requested_by","reviewed_by")
    list_filter   = ("status","requested_at")
    search_fields = ("customer",)
    actions       = ("approve_and_push","reject_requests")

    @admin.action(description="Approve & Push to ERP")
    def approve_and_push(self, request, queryset):
        ok = fail = 0
        for upd in queryset.select_for_update().filter(status="PENDING"):
            resp = _push_to_erp(upd.customer, upd.latitude, upd.longitude)
            upd.reviewed_by = request.user
            upd.reviewed_at = timezone.now()
            upd.erp_push_response = (resp.text or "")[:5000]
            if resp.ok:
                upd.status = "APPROVED"; ok += 1
                if upd.requested_by:
                    award_points(
                        upd.requested_by, 2,
                        EnergyPointTransaction.Reason.LOCATION_UPDATE_APPROVED,
                        {"customer": upd.customer, "lat": upd.latitude, "lon": upd.longitude}
                    )

            else:
                upd.status = "FAILED"; fail += 1
            upd.save(update_fields=["status","reviewed_by","reviewed_at","erp_push_response"])
        if ok:   messages.success(request, f"Pushed {ok} update(s) to ERP.")
        if fail: messages.error(request, f"{fail} update(s) failed. See row details for ERP response.")

    @admin.action(description="Reject")
    def reject_requests(self, request, queryset):
        n = queryset.filter(status="PENDING").update(
            status="REJECTED", reviewed_by=request.user, reviewed_at=timezone.now()
        )
        messages.info(request, f"Rejected {n} update(s).")
