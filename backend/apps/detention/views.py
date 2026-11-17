"""Detention register: business rules and API. Rows are closed with an outcome or voided with a reason, never deleted. Every write is
audited and also appended to the person's custody log (DetentionEvent). Access: detention.view to read, detention.manage to book in,
log events, release/transfer and void, both narrowed to the caller's unit scope (an out-of-scope record answers 404)."""
from datetime import date, timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.http import Http404
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import JSONParser
from rest_framework.response import Response

from apps.accounts import scope
from apps.accounts.permissions import ScopedPermission
from apps.audit.services import record, record_read
from apps.core.concurrency import VersionConflict, require_version
from apps.core.errors import field_error
from apps.core.params import json_body
from apps.organization.models import OrganizationalUnit
from apps.personnel.models import Personnel
from apps.personnel.services import NOT_SERVING
from apps.vehicles.views import _person_brief, _uid, _unit_brief

from .models import CustodyFacility, DetentionCounter, DetentionEvent, DetentionRecord

R = DetentionRecord
E = DetentionEvent
MANUAL_EVENT_KINDS = ("welfare", "medical", "visit", "cell_move", "property", "statement", "note")
EDITABLE = {"detainee_name": 200, "detainee_alias": 200, "id_ref": 80, "address": 200, "arrest_place": 200, "arresting_officer_name": 200,
            "reason": 300, "authority_ref": 150, "cell": 60, "property_held": 500, "health_note": 300}


class _Page(PageNumberPagination):
    page_size = 25
    max_page_size = 100
    page_size_query_param = "page_size"


# ---- helpers ---------------------------------------------------------------------------------------------------------------
def _text(d, name, limit, required=False):
    v = d.get(name)
    v = "" if v is None else str(v).strip()
    if required and not v:
        raise field_error(name, "required", "This field is required.")
    if len(v) > limit:
        raise field_error(name, "too_long", "Too long.")
    return v


def _when(value, name, default=None):
    if value in (None, ""):
        return default
    dt = parse_datetime(str(value))
    if dt is None:
        raise field_error(name, "invalid_datetime", "Use a date and time such as 2026-11-16T08:30.")
    return timezone.make_aware(dt) if timezone.is_naive(dt) else dt


def _day(value, name):
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise field_error(name, "invalid_date", "Use YYYY-MM-DD.")


def _not_future(dt, name):
    if dt > timezone.now() + timedelta(minutes=5):
        raise field_error(name, "future", "This cannot be in the future.")
    return dt


def _actor(request):
    return getattr(getattr(request, "user", None), "username", "") or ""


def review_hours():
    return int(getattr(settings, "DETENTION_REVIEW_HOURS", 48))


def held_hours(r, now=None):
    end = r.ended_at or (now or timezone.now())
    return max(int((end - r.booked_at).total_seconds() // 3600), 0)


def is_overdue(r, now=None):
    return r.status == R.Status.IN_CUSTODY and held_hours(r, now) >= review_hours()


def _next_number():
    c, _ = DetentionCounter.objects.select_for_update().get_or_create(name="detention")
    c.last += 1
    c.save(update_fields=["last"])
    return f"DET-{c.last:06d}"


def _log(record_row, kind, request, text="", occurred_at=None):
    return E.objects.create(record=record_row, kind=kind, occurred_at=occurred_at or timezone.now(), text=text, by_username=_actor(request))


def _officer(d):
    raw = d.get("arresting_officer")
    if raw in (None, ""):
        return None
    p = Personnel.objects.filter(pk=_uid(raw)).exclude(status__in=NOT_SERVING).select_related("rank").first() if _uid(raw) else None
    if p is None:
        raise field_error("arresting_officer", "unknown", "Unknown or non-serving person.")
    return p


def _locked(pk, version):
    r = R.objects.select_for_update().get(pk=pk)
    if r.version != version:
        raise VersionConflict()
    return r


def _jail_center(d, unit):
    """The jail center that holds the person: the one chosen, else the station's only active one, else none (it is optional)."""
    raw = d.get("facility")
    if raw in (None, ""):
        mine = list(CustodyFacility.objects.filter(org_unit=unit, kind="jail_center", active=True)[:2])
        return mine[0] if len(mine) == 1 else None
    fac = CustodyFacility.objects.filter(pk=_uid(raw)).first() if _uid(raw) else None
    if fac is None or fac.kind != "jail_center" or not fac.active or fac.org_unit_id != unit.pk:
        raise field_error("facility", "invalid_choice", "Choose a jail center of this station.")
    return fac


# ---- business rules --------------------------------------------------------------------------------------------------------
@transaction.atomic
def book_in(request, unit, d):
    name = _text(d, "detainee_name", 200, required=True)
    reason = _text(d, "reason", 300, required=True)
    sex = d.get("sex") or R.Sex.UNKNOWN
    if sex not in {c.value for c in R.Sex}:
        raise field_error("sex", "invalid_choice", "Choose male, female or not recorded.")
    age = None
    if d.get("age_years") not in (None, ""):
        try:
            age = int(str(d["age_years"]).strip())
        except (TypeError, ValueError):
            raise field_error("age_years", "invalid_number", "Enter a whole number.")
        if age < 0 or age > 120:
            raise field_error("age_years", "invalid_number", "Enter an age between 0 and 120.")
    booked = _not_future(_when(d.get("booked_at"), "booked_at", timezone.now()), "booked_at")
    officer = _officer(d)
    facility = _jail_center(d, unit)
    r = R.objects.create(register_no=_next_number(), org_unit=unit, facility=facility, detainee_name=name, detainee_alias=_text(d, "detainee_alias", 200), sex=sex,
                         age_years=age, id_ref=_text(d, "id_ref", 80), address=_text(d, "address", 200), booked_at=booked,
                         arrest_place=_text(d, "arrest_place", 200), arresting_officer=officer,
                         arresting_officer_name=_text(d, "arresting_officer_name", 200) or (officer.full_name if officer else ""), reason=reason,
                         authority_ref=_text(d, "authority_ref", 150), cell=_text(d, "cell", 60), property_held=_text(d, "property_held", 500),
                         health_note=_text(d, "health_note", 300), created_by_username=_actor(request))
    _log(r, "booked", request, f"Booked in: {reason}", booked)
    record("detention.book", request=request, entity_type="detention_record", entity_id=r.pk, metadata={"register_no": r.register_no, "org_unit": str(unit.pk)})
    if d.get("log_in_ob") in (True, "true", "1", 1, "on"):  # the arrest is also entered in the station's Occurrence Book
        from apps.occurrence.services import log_arrest
        log_arrest(request, r)
    return r


@transaction.atomic
def edit_details(request, row, d, version):
    r = _locked(row.pk, version)
    if r.status == R.Status.VOID:
        raise field_error("state", "void", "A voided record cannot be changed.")
    changed = []
    for name, limit in EDITABLE.items():
        if name in d:
            new = _text(d, name, limit, required=name in ("detainee_name", "reason"))
            if new != getattr(r, name):
                setattr(r, name, new)
                changed.append(name)
    if "arresting_officer" in d:
        officer = _officer(d)
        if (officer.pk if officer else None) != r.arresting_officer_id:
            r.arresting_officer = officer
            changed.append("arresting_officer")
    if "sex" in d and d["sex"] != r.sex:
        if d["sex"] not in {c.value for c in R.Sex}:
            raise field_error("sex", "invalid_choice", "Choose male, female or not recorded.")
        r.sex = d["sex"]
        changed.append("sex")
    if not changed:
        return r
    r.version += 1
    r.save()
    _log(r, "corrected", request, "Changed: " + ", ".join(changed))  # field names only; values stay in the record itself
    record("detention.edit", request=request, entity_type="detention_record", entity_id=r.pk, metadata={"register_no": r.register_no, "fields": changed})
    return r


@transaction.atomic
def add_event(request, row, d, version):
    r = _locked(row.pk, version)
    if r.status != R.Status.IN_CUSTODY:
        raise field_error("state", "not_in_custody", "Events can only be added while the person is in custody.")
    kind = d.get("kind")
    if kind not in MANUAL_EVENT_KINDS:
        raise field_error("kind", "invalid_choice", "Choose a welfare check, medical, visit, cell move, property, statement or note.")
    text = _text(d, "text", 500, required=kind != "cell_move")
    when = _not_future(_when(d.get("occurred_at"), "occurred_at", timezone.now()), "occurred_at")
    if when < r.booked_at:
        raise field_error("occurred_at", "before_booking", "This cannot be earlier than the booking time.")
    if kind == "cell_move":
        cell = _text(d, "cell", 60, required=True)
        text = (f"Moved to {cell}. " + text).strip()
        r.cell = cell
    elif kind == "property" and d.get("property_held") not in (None, ""):
        r.property_held = _text(d, "property_held", 500)
    r.version += 1
    r.save()
    ev = _log(r, kind, request, text, when)
    record("detention.event", request=request, entity_type="detention_record", entity_id=r.pk, metadata={"register_no": r.register_no, "kind": kind})
    return r, ev


@transaction.atomic
def close_record(request, row, d, version):
    r = _locked(row.pk, version)
    if r.status != R.Status.IN_CUSTODY:
        raise field_error("state", "not_in_custody", "This person is no longer in custody.")
    outcome = d.get("outcome")
    if outcome not in R.RELEASE_OUTCOMES + R.TRANSFER_OUTCOMES:
        raise field_error("outcome", "invalid_choice", "Choose how the person left custody.")
    transfer = outcome in R.TRANSFER_OUTCOMES
    dest_fac = None
    if d.get("destination_facility") not in (None, ""):
        dest_fac = CustodyFacility.objects.filter(pk=_uid(d["destination_facility"])).first() if _uid(d["destination_facility"]) else None
        if outcome != "transfer_prison" or dest_fac is None or dest_fac.kind != "prison" or not dest_fac.active:
            raise field_error("destination_facility", "invalid_choice", "Choose a prison, and only when handing the person to the prison.")
    destination = _text(d, "destination", 200) or (dest_fac.name_en if dest_fac else "")
    if transfer and not destination:
        raise field_error("destination", "required", "This field is required.")
    ended = _not_future(_when(d.get("ended_at"), "ended_at", timezone.now()), "ended_at")
    if ended < r.booked_at:
        raise field_error("ended_at", "before_booking", "This cannot be earlier than the booking time.")
    note = _text(d, "outcome_note", 300, required=outcome in ("other", "bail", "court_order"))
    r.status = R.Status.TRANSFERRED if transfer else R.Status.RELEASED
    r.outcome, r.destination, r.outcome_note, r.ended_at, r.ended_by_username = outcome, destination, note, ended, _actor(request)
    r.destination_facility = dest_fac
    r.version += 1
    r.save()
    label = R.Outcome(outcome).label
    _log(r, "transferred" if transfer else "released", request, label + (f" to {destination}" if destination else "") + (f". {note}" if note else ""), ended)
    record("detention.transfer" if transfer else "detention.release", request=request, entity_type="detention_record", entity_id=r.pk,
           metadata={"register_no": r.register_no, "outcome": outcome, "held_hours": held_hours(r)})
    return r


@transaction.atomic
def void_record(request, row, d, version):
    r = _locked(row.pk, version)
    if r.status == R.Status.VOID:
        raise field_error("state", "already_void", "This record is already voided.")
    reason = _text(d, "reason", 300, required=True)
    r.status, r.voided_at, r.voided_by_username, r.voided_reason = R.Status.VOID, timezone.now(), _actor(request), reason
    r.version += 1
    r.save()
    _log(r, "voided", request, reason)
    record("detention.void", request=request, entity_type="detention_record", entity_id=r.pk, metadata={"register_no": r.register_no, "reason": reason})
    return r


# ---- serialisers -----------------------------------------------------------------------------------------------------------
def _fac_brief(f):
    return None if f is None else {"id": str(f.pk), "code": f.code, "kind": f.kind, "name_en": f.name_en, "name_so": f.name_so}


def _out(r, now=None):
    return {"id": str(r.pk), "register_no": r.register_no, "org_unit": _unit_brief(r.org_unit), "detainee_name": r.detainee_name,
            "detainee_alias": r.detainee_alias, "sex": r.sex, "age_years": r.age_years, "id_ref": r.id_ref, "address": r.address,
            "booked_at": r.booked_at, "arrest_place": r.arrest_place, "arresting_officer": _person_brief(r.arresting_officer),
            "arresting_officer_name": r.arresting_officer_name, "reason": r.reason, "authority_ref": r.authority_ref, "cell": r.cell,
            "facility": _fac_brief(r.facility), "destination_facility": _fac_brief(r.destination_facility),
            "property_held": r.property_held, "health_note": r.health_note, "status": r.status, "ended_at": r.ended_at, "outcome": r.outcome,
            "destination": r.destination, "outcome_note": r.outcome_note, "held_hours": held_hours(r, now), "overdue": is_overdue(r, now),
            "recorded_by": r.created_by_username, "voided_reason": r.voided_reason if r.status == R.Status.VOID else "", "version": r.version}


def _event_out(e):
    return {"id": str(e.pk), "kind": e.kind, "occurred_at": e.occurred_at, "text": e.text, "by": e.by_username}


# ---- API -------------------------------------------------------------------------------------------------------------------
class DetentionViewSet(viewsets.GenericViewSet):
    """Detention register. Reads are audited and narrowed to the caller's unit scope."""

    permission_classes = [ScopedPermission]
    pagination_class = _Page
    parser_classes = [JSONParser]
    lookup_value_regex = r"[0-9a-f\-]{36}"
    permission_map = {"list": ("detention.view", "any"), "retrieve": ("detention.view", "any"), "summary": ("detention.view", "any"),
                      "create": ("detention.manage", "any"), "edit": ("detention.manage", "any"), "event": ("detention.manage", "any"),
                      "close": ("detention.manage", "any"), "void": ("detention.manage", "any")}

    def _scoped(self):
        q = scope.unit_scope_q(self.request.user, "detention.view", "org_unit__")
        return R.objects.none() if q is None else R.objects.filter(q)

    def _row(self, pk, write=False):
        row = self._scoped().select_related("org_unit", "arresting_officer__rank", "facility", "destination_facility").filter(pk=_uid(pk)).first() if _uid(pk) else None
        if row is None or (write and not scope.can(self.request.user, "detention.manage", unit=row.org_unit)):
            raise Http404
        return row

    def _fresh(self, pk):
        return _out(R.objects.select_related("org_unit", "arresting_officer__rank", "facility", "destination_facility").get(pk=pk))

    def list(self, request):
        p = request.query_params
        qs = self._scoped().select_related("org_unit", "arresting_officer__rank", "facility", "destination_facility")
        if p.get("status") in {"in_custody", "released", "transferred", "void"}:
            qs = qs.filter(status=p["status"])
        elif p.get("include_void") != "1":
            qs = qs.exclude(status="void")
        if p.get("overdue") == "1":
            qs = qs.filter(status="in_custody", booked_at__lte=timezone.now() - timedelta(hours=review_hours()))
        if _uid(p.get("unit")):
            prefix = OrganizationalUnit.objects.filter(pk=_uid(p["unit"])).values_list("path", flat=True).first()
            qs = qs.filter(org_unit__path__startswith=prefix) if prefix else qs.none()
        if p.get("from"):
            qs = qs.filter(booked_at__date__gte=_day(p["from"], "from"))
        if p.get("to"):
            qs = qs.filter(booked_at__date__lte=_day(p["to"], "to"))
        if p.get("q"):
            s = p["q"].strip()[:60]
            qs = qs.filter(Q(register_no__icontains=s) | Q(detainee_name__icontains=s) | Q(detainee_alias__icontains=s) | Q(reason__icontains=s)
                           | Q(id_ref__icontains=s) | Q(authority_ref__icontains=s))
        page = self.paginate_queryset(qs.order_by("-booked_at"))
        record_read(request, "detention.read_list", entity_type="detention_register", count=len(page))
        now = timezone.now()
        return self.get_paginated_response([_out(r, now) for r in page])

    def retrieve(self, request, pk=None):
        r = self._row(pk)
        record_read(request, "detention.read_record", entity_type="detention_record", entity_id=r.pk, org_unit=r.org_unit)
        return Response({**_out(r), "events": [_event_out(e) for e in r.events.all()]})

    def create(self, request):
        d = json_body(request)
        unit = OrganizationalUnit.objects.filter(pk=_uid(d.get("org_unit"))).first() if _uid(d.get("org_unit")) else None
        if unit is None or not scope.can(request.user, "detention.manage", unit=unit):
            raise field_error("org_unit", "unknown", "Choose the station holding the person.")
        r = book_in(request, unit, d)
        return Response(self._fresh(r.pk), status=201)

    @action(detail=True, methods=["post"])
    def edit(self, request, pk=None):
        d = json_body(request)
        r = edit_details(request, self._row(pk, write=True), d, require_version(d))
        return Response(self._fresh(r.pk))

    @action(detail=True, methods=["post"])
    def event(self, request, pk=None):
        d = json_body(request)
        r, _ev = add_event(request, self._row(pk, write=True), d, require_version(d))
        return Response(self._fresh(r.pk), status=201)

    @action(detail=True, methods=["post"])
    def close(self, request, pk=None):
        """Release or transfer: the person leaves custody with an outcome."""
        d = json_body(request)
        r = close_record(request, self._row(pk, write=True), d, require_version(d))
        return Response(self._fresh(r.pk))

    @action(detail=True, methods=["post"])
    def void(self, request, pk=None):
        d = json_body(request)
        r = void_record(request, self._row(pk, write=True), d, require_version(d))
        return Response(self._fresh(r.pk))

    @action(detail=False, methods=["get"])
    def summary(self, request):
        qs = self._scoped().exclude(status="void")
        now = timezone.now()
        today = timezone.localdate()
        held = qs.filter(status="in_custody")
        out = {"in_custody": held.count(), "overdue": held.filter(booked_at__lte=now - timedelta(hours=review_hours())).count(),
               "booked_today": qs.filter(booked_at__date=today).count(), "left_today": qs.filter(ended_at__date=today).count(),
               "review_hours": review_hours()}
        record_read(request, "detention.read_summary", entity_type="detention_summary")
        return Response(out)
