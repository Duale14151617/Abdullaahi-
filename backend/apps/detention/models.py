"""Detention register (Police custody). One row per person held at a station, plus an append-only log of what happened to them.

* A record is never deleted. A wrong entry is voided with a reason; a person leaving custody is closed with an outcome.
* The custody clock runs from `booked_at` until `ended_at`. Anything held past DETENTION_REVIEW_HOURS is flagged for review (a
  configurable alert, not a statement of law).
* Every change also writes a DetentionEvent (append-only), so the register shows the whole custody story in order.
"""
import uuid

from django.db import models

from apps.core.appendonly import AppendOnlyError, AppendOnlyModel


class DetentionCounter(models.Model):
    """Single row that hands out register numbers (locked with SELECT ... FOR UPDATE), so numbers are never reused."""

    name = models.CharField(max_length=20, primary_key=True)
    last = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "detention_counters"


class CustodyFacility(models.Model):
    """A place where people are held. Two kinds, kept apart on purpose:

    * Jail center (Jeel / lock-up): the police cell at a station. Short-term custody run by the Police: people arrested and held
      while the case is investigated and until they are charged, released or handed to the court. Belongs to one station.
    * Prison (Xabsi): a long-term institution run by the prison authority, NOT the Police. It receives people the court has
      sentenced (or remanded). The Police only hand people over; the system records the hand-over, nothing inside the prison.

    Never deleted: a facility that closes is switched off (active = False).
    """

    class Kind(models.TextChoices):
        JAIL_CENTER = "jail_center", "Jail center (police lock-up)"
        PRISON = "prison", "Prison"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=64, unique=True)
    kind = models.CharField(max_length=12, choices=Kind.choices)
    name_en = models.CharField(max_length=150)
    name_so = models.CharField(max_length=150, blank=True)
    org_unit = models.ForeignKey("organization.OrganizationalUnit", null=True, blank=True, on_delete=models.PROTECT, related_name="custody_facilities")
    location = models.CharField(max_length=200, blank=True)
    capacity = models.PositiveIntegerField(null=True, blank=True)
    operated_by = models.CharField(max_length=150, blank=True)
    active = models.BooleanField(default=True)
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "custody_facilities"
        ordering = ["kind", "name_en"]
        constraints = [
            models.CheckConstraint(condition=~models.Q(kind="jail_center") | models.Q(org_unit__isnull=False), name="facility_jail_has_station"),
            models.CheckConstraint(condition=~models.Q(kind="prison") | models.Q(org_unit__isnull=True), name="facility_prison_no_station"),
        ]

    def delete(self, *args, **kwargs):
        raise AppendOnlyError("Facilities cannot be deleted; switch them off instead.")


class DetentionRecord(models.Model):
    class Status(models.TextChoices):
        IN_CUSTODY = "in_custody", "In custody"
        RELEASED = "released", "Released"
        TRANSFERRED = "transferred", "Transferred"
        VOID = "void", "Void"

    class Sex(models.TextChoices):
        MALE = "male", "Male"
        FEMALE = "female", "Female"
        UNKNOWN = "unknown", "Not recorded"

    class Outcome(models.TextChoices):
        NO_CHARGE = "no_charge", "Released without charge"
        BAIL = "bail", "Released on bail / guarantee"
        COURT_ORDER = "court_order", "Released on court order"
        SENTENCE_SERVED = "sentence_served", "Sentence served"
        TRANSFER_STATION = "transfer_station", "Transferred to another station"
        TRANSFER_COURT = "transfer_court", "Handed to the court"
        TRANSFER_PRISON = "transfer_prison", "Handed to the prison"
        OTHER = "other", "Other"

    RELEASE_OUTCOMES = ("no_charge", "bail", "court_order", "sentence_served", "other")
    TRANSFER_OUTCOMES = ("transfer_station", "transfer_court", "transfer_prison")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    register_no = models.CharField(max_length=20, unique=True)  # DET-000001, system generated, never reused
    org_unit = models.ForeignKey("organization.OrganizationalUnit", on_delete=models.PROTECT, related_name="detention_records")
    detainee_name = models.CharField(max_length=200)
    detainee_alias = models.CharField(max_length=200, blank=True)
    sex = models.CharField(max_length=8, choices=Sex.choices, default=Sex.UNKNOWN)
    age_years = models.PositiveSmallIntegerField(null=True, blank=True)
    id_ref = models.CharField(max_length=80, blank=True)  # national ID / document number shown by the person, if any
    address = models.CharField(max_length=200, blank=True)
    booked_at = models.DateTimeField()
    arrest_place = models.CharField(max_length=200, blank=True)
    arresting_officer = models.ForeignKey("personnel.Personnel", null=True, blank=True, on_delete=models.PROTECT, related_name="detentions_made")
    arresting_officer_name = models.CharField(max_length=200, blank=True)  # when the officer has no personnel record
    reason = models.CharField(max_length=300)  # offence or ground for holding
    authority_ref = models.CharField(max_length=150, blank=True)  # warrant / court order / prosecutor instruction number
    cell = models.CharField(max_length=60, blank=True)
    facility = models.ForeignKey(CustodyFacility, null=True, blank=True, on_delete=models.PROTECT, related_name="records")  # the jail center holding the person
    destination_facility = models.ForeignKey(CustodyFacility, null=True, blank=True, on_delete=models.PROTECT, related_name="received")  # the prison, on hand-over
    property_held = models.CharField(max_length=500, blank=True)
    health_note = models.CharField(max_length=300, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.IN_CUSTODY)
    ended_at = models.DateTimeField(null=True, blank=True)
    outcome = models.CharField(max_length=20, choices=Outcome.choices, blank=True)
    destination = models.CharField(max_length=200, blank=True)  # receiving station / court / prison for a transfer
    outcome_note = models.CharField(max_length=300, blank=True)
    created_by_username = models.CharField(max_length=150, blank=True)
    ended_by_username = models.CharField(max_length=150, blank=True)
    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by_username = models.CharField(max_length=150, blank=True)
    voided_reason = models.CharField(max_length=300, blank=True)
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "detention_records"
        ordering = ["-booked_at"]
        indexes = [models.Index(fields=["org_unit", "status", "-booked_at"], name="det_unit_status_idx"),
                   models.Index(fields=["status", "-booked_at"], name="det_status_idx")]
        constraints = [models.CheckConstraint(condition=models.Q(ended_at__isnull=True) | models.Q(ended_at__gte=models.F("booked_at")),
                                              name="det_end_after_booking")]

    def delete(self, *args, **kwargs):
        raise AppendOnlyError("Detention register rows cannot be deleted.")


class DetentionEvent(AppendOnlyModel):
    class Kind(models.TextChoices):
        BOOKED = "booked", "Booked in"
        WELFARE = "welfare", "Welfare check"
        MEDICAL = "medical", "Medical"
        VISIT = "visit", "Visit"
        CELL_MOVE = "cell_move", "Moved cell"
        PROPERTY = "property", "Property"
        STATEMENT = "statement", "Statement / interview"
        NOTE = "note", "Note"
        RELEASED = "released", "Released"
        TRANSFERRED = "transferred", "Transferred"
        CORRECTED = "corrected", "Details corrected"
        VOIDED = "voided", "Voided"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    record = models.ForeignKey(DetentionRecord, on_delete=models.PROTECT, related_name="events")
    kind = models.CharField(max_length=12, choices=Kind.choices)
    occurred_at = models.DateTimeField()
    text = models.CharField(max_length=500, blank=True)
    by_username = models.CharField(max_length=150, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "detention_events"
        ordering = ["occurred_at", "created_at"]
        indexes = [models.Index(fields=["record", "occurred_at"], name="detevt_record_idx")]
