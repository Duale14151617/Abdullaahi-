"""Custody facilities API: jail centers (police lock-ups, one or more per station) and prisons (outside the Police).

Read: detention.view. A jail center is shown to callers whose scope covers its station; prisons belong to no station and are shown to
everyone who can read the register. Write: detention.manage. A jail center needs that permission for its station; a prison, which sits
outside the unit tree, needs it at headquarters level (a global grant). Facilities are never deleted, only switched off."""
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.http import Http404
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.parsers import JSONParser
from rest_framework.response import Response

from apps.accounts import scope
from apps.accounts.permissions import ScopedPermission
from apps.audit.services import record
from apps.core.concurrency import VersionConflict, require_version
from apps.core.errors import field_error
from apps.core.params import json_body
from apps.organization.models import OrganizationalUnit
from apps.vehicles.views import _uid, _unit_brief

from .models import CustodyFacility as F
from .views import _text

KINDS = {k for k, _ in F.Kind.choices}


def _out(f):
    held = getattr(f, "held_now", None)
    return {"id": str(f.pk), "code": f.code, "kind": f.kind, "name_en": f.name_en, "name_so": f.name_so, "org_unit": _unit_brief(f.org_unit),
            "location": f.location, "capacity": f.capacity, "operated_by": f.operated_by, "active": f.active, "version": f.version,
            "held_now": held if f.kind == "jail_center" else None, "received_total": getattr(f, "received_total", None) if f.kind == "prison" else None}


def _capacity(d):
    if d.get("capacity") in (None, ""):
        return None
    try:
        n = int(str(d["capacity"]).strip())
    except (TypeError, ValueError):
        raise field_error("capacity", "invalid_number", "Enter a whole number.")
    if not 0 <= n <= 100000:
        raise field_error("capacity", "invalid_number", "Enter a number between 0 and 100000.")
    return n


class CustodyFacilityViewSet(viewsets.GenericViewSet):
    permission_classes = [ScopedPermission]
    parser_classes = [JSONParser]
    lookup_value_regex = r"[0-9a-f\-]{36}"
    permission_map = {"list": ("detention.view", "any"), "create": ("detention.manage", "any"), "edit": ("detention.manage", "any")}

    def _scoped(self):
        q = scope.unit_scope_q(self.request.user, "detention.view", "org_unit__")
        if q is None:
            return F.objects.none()
        return F.objects.filter(q | Q(kind="prison")).select_related("org_unit")

    def _may_write(self, kind, unit):
        return scope.can(self.request.user, "detention.manage") if kind == "prison" else (unit is not None and scope.can(self.request.user, "detention.manage", unit=unit))

    def list(self, request):
        p = request.query_params
        qs = self._scoped().annotate(held_now=Count("records", filter=Q(records__status="in_custody"), distinct=True),
                                     received_total=Count("received", distinct=True))
        if p.get("kind") in KINDS:
            qs = qs.filter(kind=p["kind"])
        if p.get("active") in ("0", "1"):
            qs = qs.filter(active=p["active"] == "1")
        if _uid(p.get("unit")):
            qs = qs.filter(org_unit_id=_uid(p["unit"]))
        return Response({"results": [_out(f) for f in qs.order_by("kind", "name_en")]})

    def create(self, request):
        d = json_body(request)
        kind = d.get("kind")
        if kind not in KINDS:
            raise field_error("kind", "invalid_choice", "Choose jail center or prison.")
        unit = None
        if kind == "jail_center":
            unit = OrganizationalUnit.objects.filter(pk=_uid(d.get("org_unit"))).first() if _uid(d.get("org_unit")) else None
            if unit is None:
                raise field_error("org_unit", "required", "Choose the station this jail center belongs to.")
        if not self._may_write(kind, unit):
            raise Http404
        code = _text(d, "code", 64, required=True).upper().replace(" ", "-")
        if F.objects.filter(code=code).exists():
            raise field_error("code", "duplicate", "This code is already used.")
        try:
            with transaction.atomic():
                f = F.objects.create(code=code, kind=kind, org_unit=unit, name_en=_text(d, "name_en", 150, required=True), name_so=_text(d, "name_so", 150),
                                     location=_text(d, "location", 200), capacity=_capacity(d),
                                     operated_by=_text(d, "operated_by", 150) or ("Police" if kind == "jail_center" else ""))
        except IntegrityError:
            raise field_error("code", "duplicate", "This code is already used.")
        record("detention.facility_create", request=request, entity_type="custody_facility", entity_id=f.pk, metadata={"code": f.code, "kind": f.kind})
        return Response(_out(f), status=201)

    @action(detail=True, methods=["post"])
    def edit(self, request, pk=None):
        d = json_body(request)
        row = self._scoped().filter(pk=_uid(pk)).first() if _uid(pk) else None
        if row is None or not self._may_write(row.kind, row.org_unit):
            raise Http404
        with transaction.atomic():
            f = F.objects.select_for_update().get(pk=row.pk)
            if f.version != require_version(d):
                raise VersionConflict()
            changed = []
            for name, limit in (("name_en", 150), ("name_so", 150), ("location", 200), ("operated_by", 150)):
                if name in d:
                    new = _text(d, name, limit, required=name == "name_en")
                    if new != getattr(f, name):
                        setattr(f, name, new)
                        changed.append(name)
            if "capacity" in d and _capacity(d) != f.capacity:
                f.capacity = _capacity(d)
                changed.append("capacity")
            if "active" in d and bool(d["active"]) != f.active:
                if not d["active"] and f.records.filter(status="in_custody").exists():
                    raise field_error("active", "in_use", "People are still held here. Move or release them first.")
                f.active = bool(d["active"])
                changed.append("active")
            if changed:
                f.version += 1
                f.save()
                record("detention.facility_edit", request=request, entity_type="custody_facility", entity_id=f.pk, metadata={"code": f.code, "fields": changed})
        return Response(_out(F.objects.select_related("org_unit").annotate(held_now=Count("records", filter=Q(records__status="in_custody"), distinct=True),
                                                                         received_total=Count("received", distinct=True)).get(pk=f.pk)))
