"""Promotion eligibility by rank and time in rank. Advisory only: nothing here changes a rank."""
import uuid
from datetime import date

from django.db.models import Q
from django.utils import timezone
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.accounts import scope
from apps.accounts.permissions import ScopedPermission
from apps.audit.services import record as audit
from apps.audit.services import record_read
from apps.core.errors import field_error
from apps.core.params import json_body, uuid_param
from apps.organization.models import OrganizationalUnit
from apps.personnel.models import Personnel, PersonnelHistory
from apps.personnel.services import NOT_SERVING
from apps.ranks.models import Rank

from .models import PromotionRule

MAX_PEOPLE = 1000
RETIRED_FROM = 1000  # retired ranks are parked at 1000+ and never compared (see Rank.sort_order)
BLOCKING_STATUS = {"suspended", "jailed"}


def _uid(value):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


def months_between(start: date, end: date) -> int:
    """Whole months from start to end (never negative)."""
    m = (end.year - start.year) * 12 + (end.month - start.month) - (1 if end.day < start.day else 0)
    return max(0, m)


def _rank_out(r):
    return {"id": str(r.pk), "code": r.code, "name_en": r.name_en, "name_so": r.name_so}


def _next_rank(rule, rank, by_branch):
    """The rank this one is promoted to: the one named in the rule, else the next active, approved rank up in the same branch."""
    if rule.next_rank_id:
        return rule.next_rank
    ups = [r for r in by_branch.get(rank.branch_id, []) if rank.sort_order < r.sort_order < RETIRED_FROM]
    return min(ups, key=lambda r: r.sort_order) if ups else None


class PromotionViewSet(viewsets.ViewSet):
    permission_classes = [ScopedPermission]
    permission_map = {"eligibility": ("promotion.view", "any"), "rules": ("promotion.view", "any"), "set_rule": ("promotion.configure", "any")}
    queryset = PromotionRule.objects.none()

    @action(detail=False, methods=["get"])
    def eligibility(self, request):
        """?org_unit=<id>[&state=eligible]: serving people in the unit and below, with time in rank against the rule for their rank."""
        uid = uuid_param(request, "org_unit")
        unit = OrganizationalUnit.objects.filter(pk=uid).first() if uid else None
        if unit is None or not scope.can(request.user, "promotion.view", unit=unit):
            raise field_error("org_unit", "out_of_scope", "That unit is outside your scope.")
        q = scope.unit_scope_q(request.user, "promotion.view", "org_unit__")
        people = list(Personnel.objects.filter(q, org_unit__path__startswith=unit.path).exclude(status__in=NOT_SERVING)
                      .select_related("rank", "org_unit").order_by("-rank__sort_order", "full_name").distinct()[:MAX_PEOPLE])
        rules = {r.rank_id: r for r in PromotionRule.objects.filter(active=True).select_related("next_rank")}
        by_branch = {}
        for r in Rank.objects.filter(active=True, sort_order__lt=RETIRED_FROM):
            by_branch.setdefault(r.branch_id, []).append(r)
        since = {}
        for h in PersonnelHistory.objects.filter(personnel_id__in=[p.pk for p in people], field="rank").order_by("-effective_date", "-recorded_at"):
            since.setdefault(h.personnel_id, (h.effective_date, bool(h.old_ref)))  # latest rank entry per person; no old rank = first entry
        today, wanted = timezone.localdate(), request.query_params.get("state")
        rows, counts = [], {"eligible": 0, "not_yet": 0, "blocked": 0, "no_rule": 0}
        for p in people:
            rule = rules.get(p.rank_id)
            start, changed = since.get(p.pk) or (p.created_at.date(), False)
            source = "history" if changed else "record"  # "record": the date the rank was first entered, not a recorded promotion
            months = months_between(start, today)
            if rule is None:
                state, nxt = "no_rule", None
            else:
                nxt = _next_rank(rule, p.rank, by_branch)
                state = "blocked" if p.status in BLOCKING_STATUS else "eligible" if months >= rule.min_months and nxt else "not_yet"
            counts[state] += 1
            if wanted and wanted != state:
                continue
            rows.append({"personnel": {"id": str(p.pk), "full_name": p.full_name, "service_number": p.service_number, "status": p.status},
                         "unit": {"code": p.org_unit.code, "name_en": p.org_unit.name_en, "name_so": p.org_unit.name_so},
                         "rank": _rank_out(p.rank), "next_rank": _rank_out(nxt) if nxt else None,
                         "rank_since": start.isoformat(), "since_source": source, "months": months,
                         "required": rule.min_months if rule else None,
                         "months_left": max(0, rule.min_months - months) if rule else None, "state": state})
        record_read(request, "promotion.read", entity_type="promotion", org_unit=unit, count=len(rows))
        return Response({"org_unit": str(unit.pk), "counts": counts, "results": rows})

    @action(detail=False, methods=["get"])
    def rules(self, request):
        rules = {r.rank_id: r for r in PromotionRule.objects.select_related("next_rank")}
        ranks = Rank.objects.filter(active=True, sort_order__lt=RETIRED_FROM).order_by("branch__code", "sort_order")
        return Response({"results": [{**_rank_out(r), "sort_order": r.sort_order,
                                      "rule": None if r.pk not in rules else {
                                          "min_months": rules[r.pk].min_months, "active": rules[r.pk].active,
                                          "next_rank": _rank_out(rules[r.pk].next_rank) if rules[r.pk].next_rank_id else None}}
                                     for r in ranks]})

    @action(detail=False, methods=["post"], url_path="rules/set")
    def set_rule(self, request):
        """{rank, min_months, next_rank?, active?} sets the rule for a rank; {rank, remove: true} deletes it. Super administrator only."""
        if not scope.can(request.user, "promotion.configure"):
            return Response({"code": "permission_denied", "detail": "Not allowed."}, status=403)
        d = json_body(request)
        rank = Rank.objects.filter(pk=_uid(d.get("rank")), active=True).first() if _uid(d.get("rank")) else None
        if rank is None:
            raise field_error("rank", "rank_not_found", "Unknown rank.")
        if d.get("remove"):
            PromotionRule.objects.filter(rank=rank).delete()
            audit("promotion.rule_removed", request=request, entity_type="promotion", entity_id=rank.pk, metadata={"rank": rank.code})
            return Response(status=204)
        months = d.get("min_months")
        if not isinstance(months, int) or isinstance(months, bool) or not 1 <= months <= 600:
            raise field_error("min_months", "months_invalid", "Enter a whole number of months from 1 to 600.")
        nxt = None
        if d.get("next_rank"):
            nxt = Rank.objects.filter(pk=_uid(d["next_rank"]), active=True, branch_id=rank.branch_id).first() if _uid(d["next_rank"]) else None
            if nxt is None or nxt.sort_order <= rank.sort_order:
                raise field_error("next_rank", "next_rank_invalid", "Choose a more senior rank of the same service.")
        rule, _ = PromotionRule.objects.update_or_create(rank=rank, defaults={
            "min_months": months, "next_rank": nxt, "active": bool(d.get("active", True)), "updated_by_username": request.user.username})
        audit("promotion.rule_set", request=request, entity_type="promotion", entity_id=rank.pk,
              metadata={"rank": rank.code, "months": months, "active": rule.active})
        return Response({"rank": rank.code, "min_months": rule.min_months, "active": rule.active}, status=201)
