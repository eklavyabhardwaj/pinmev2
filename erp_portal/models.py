# models.py
from django.db import models
from django.conf import settings



class UserMergeHistory(models.Model):
    primary_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="merge_primary_history"
    )
    secondary_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="merge_secondary_history"
    )
    merged_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["primary_user", "secondary_user"],
                name="unique_user_merge"
            )
        ]


    def __str__(self):
        return f"{self.secondary_user} → {self.primary_user} at {self.merged_at}"

# --- Energy points -----------------------------------------
class EnergyPointTransaction(models.Model):
    class Reason(models.TextChoices):
        PUNCH_IN = "PUNCH_IN", "Punch In"
        PUNCH_OUT_NEAR = "PUNCH_OUT_NEAR", "Punch Out (≤200m)"
        PUNCH_OUT_FAR = "PUNCH_OUT_FAR", "Punch Out (>200m)"
        MEETING_DETAILS = "MEETING_DETAILS", "Meeting Details Added"
        LOCATION_UPDATE_APPROVED = "LOCATION_UPDATE_APPROVED", "Customer Location Approved"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="energy_txs")
    points = models.IntegerField()
    reason = models.CharField(max_length=64, choices=Reason.choices)
    meta = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]




class CustomerLocationUpdate(models.Model):
    class Status(models.TextChoices):
        PENDING  = "PENDING",  "Pending"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"
        FAILED   = "FAILED",   "Failed"

    customer   = models.CharField(max_length=255)     # ERP DocName (Customer.name)
    latitude   = models.FloatField()
    longitude  = models.FloatField()

    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="requested_location_updates"
    )
    requested_at = models.DateTimeField(auto_now_add=True)

    status      = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="reviewed_location_updates"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    erp_push_response = models.TextField(blank=True)

    class Meta:
        ordering = ["-requested_at"]

    def __str__(self):
        return f"{self.customer} -> ({self.latitude}, {self.longitude}) [{self.status}]"



class OpportunityMeetingDetail(models.Model):
    """
    Stores meeting details associated with an employee's check-in and opportunity.
    """
    # Reference to Django user (if using Django auth) or store employee identifier
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='meeting_details',
        help_text='Django user who performed the check-in'
    )
    # Frappe checkin identifier (document name)
    checkin_id = models.CharField(
        max_length=255,
        help_text='ERP Document ID for the check-in record'
    )
    # Customer and opportunity as referenced in ERP
    customer = models.CharField(
        max_length=255,
        help_text='Customer linked to this check-in'
    )
    opportunity = models.CharField(
        max_length=255,
        help_text='Opportunity linked to this check-in'
    )
    # Meeting-specific details
    agenda = models.TextField(
        blank=True,
        help_text='Meeting agenda or purpose'
    )
    visited_department = models.CharField(
        max_length=255,
        blank=True,
        help_text='Department visited during the meeting'
    )
    contact_person = models.CharField(
        max_length=255,
        blank=True,
        help_text='Primary contact person encountered'
    )
    notes = models.TextField(
        blank=True,
        help_text='Additional notes from the meeting'
    )
    # Timestamp when this detail was created locally
    created_at = models.DateTimeField(
        auto_now_add=True,
        help_text='Local timestamp when meeting detail was recorded'
    )

    class Meta:
        verbose_name = 'Opportunity Meeting Detail'
        verbose_name_plural = 'Opportunity Meeting Details'
        ordering = ['-created_at']

    def __str__(self):
        return f"Meeting ({self.customer} - {self.opportunity}) by {self.user} at {self.created_at:%Y-%m-%d %H:%M}"
