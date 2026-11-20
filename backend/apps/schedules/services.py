"""Scheduled email reports: when a schedule is due, who may receive it, what the email says, and sending it.

Only counts are emailed (the same figures as the in-app personnel report, never names or service numbers). A recipient must be an
active user with a verified email address AND hold `personnel.view` over the schedule's unit, so an email can never show someone more
than they could open in the app. The figures are built with the creator's permissions; if the creator has lost access the run is
skipped and says so rather than sending anything.
"""
import logging
from datetime import datetime, time, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from apps.accounts import scope
from apps.accounts.models import User
from apps.audit.services import record
from apps.personnel import report as strength

from .models import ReportSchedule

log = logging.getLogger("armyhq.schedules")
MAX_RECIPIENTS = 25
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def tz():
    return ZoneInfo(getattr(settings, "SCHEDULES_TIMEZONE", "Africa/Mogadishu"))


def compute_next(s, after=None):
    """The first send time strictly after `after` (default now), in UTC."""
    zone = tz()
    local = (after or timezone.now()).astimezone(zone)
    for i in range(0, 400):
        day = local.date() + timedelta(days=i)
        if s.frequency == ReportSchedule.Frequency.WEEKLY and day.weekday() != s.weekday:
            continue
        if s.frequency == ReportSchedule.Frequency.MONTHLY and day.day != s.day_of_month:
            continue
        cand = datetime.combine(day, time(s.send_hour), tzinfo=zone)
        if cand > local:
            return cand.astimezone(dt_timezone.utc)
    return None


def eligible_recipients(unit):
    """Active users with a verified email who may view personnel for `unit`."""
    users = User.objects.filter(is_active=True, email_verified_at__isnull=False).exclude(email="").order_by("full_name", "username")
    return [u for u in users if scope.can(u, "personnel.view", unit=unit)]



def render(s, data):
    t = data["totals"]
    lines = [f"{s.name}", f"Area: {s.org_unit.name_en}",
             f"Period: {data['from']} to {data['to']}   Prepared: {timezone.localtime(data['as_of'], tz()).strftime('%Y-%m-%d %H:%M')}", "",
             "STRENGTH", f"  Serving ........ {t['serving']}", f"  On file ........ {t['on_file']}",
             f"  Active ......... {t['active']}   On leave ... {t['on_leave']}   Suspended ... {t['suspended']}   Jailed ... {t['jailed']}",
             "", "MOVEMENT IN THE PERIOD", f"  Joined ......... {t['joined']}", f"  Left ........... {t['left']}",
             f"  Promoted ....... {t['promoted']}", ""]
    if data["regions"]:
        lines.append("BY REGION (serving / on file)")
        for r in data["regions"]:
            lines.append(f"  {(r['unit'] or {}).get('name_en', '-'):<34} {r['serving']:>6} / {r['on_file']:<6}")
        lines.append("")
    if data["ranks"]:
        lines.append("BY RANK (serving / on file)")
        for r in data["ranks"]:
            lines.append(f"  {r['name_en']:<34} {r['serving']:>6} / {r['on_file']:<6}")
        lines.append("")
    base = (getattr(settings, "SITE_URL", "") or "").rstrip("/")
    lines += ["Counts only. No names or service numbers are included in this email.",
              f"Full report: {base}/personnel-report" if base else "", "",
              "You receive this because a Police HQ administrator added you to this schedule."]
    return "\n".join(lines).strip() + "\n"


def _console_backend():
    return str(getattr(settings, "EMAIL_BACKEND", "")).endswith("console.EmailBackend")


def deliver(s, *, only_to=None):
    """Build and send one run. Returns (status, detail). Never raises."""
    try:
        if not getattr(settings, "SCHEDULED_REPORTS_ENABLED", True):
            return "disabled", "Scheduled reports are switched off on this server."
        creator = s.created_by
        if not creator.is_active or not scope.can(creator, "personnel.view", unit=s.org_unit):
            return "no_access", "The person who created this schedule can no longer view this area."
        today = timezone.localdate()
        data = strength.build(creator, s.org_unit, today - timedelta(days=max(s.period_days, 1) - 1), today)
        if data is None:
            return "no_access", "The person who created this schedule can no longer view this area."
        pool = [only_to] if only_to is not None else list(s.recipients.filter(is_active=True, email_verified_at__isnull=False).exclude(email=""))
        recipients = [u for u in pool if scope.can(u, "personnel.view", unit=s.org_unit)][:MAX_RECIPIENTS]
        if not recipients:
            return "no_recipients", "No recipient with a verified email who can view this area."
        if _console_backend():
            return "email_not_configured", "Outgoing email is not set up on this server, so nothing was sent."
        body = render(s, data)
        prefix = getattr(settings, "ISSUANCE_REF_PREFIX", "PHQ")
        subject = f"[{prefix}] {s.name} - {today.isoformat()}"
        sent = failed = 0
        for u in recipients:
            try:
                send_mail(subject, body, None, [u.email], fail_silently=False)
                sent += 1
            except Exception:
                failed += 1
                log.warning("scheduled report email could not be sent")
        if sent and not failed:
            return "sent", f"Sent to {sent} recipient(s)."
        if sent:
            return "partial", f"Sent to {sent}, failed for {failed}."
        return "failed", "The email provider did not accept the messages."
    except Exception:
        log.exception("scheduled report failed")
        return "failed", "Unexpected error while building or sending the report."


def run(s, *, only_to=None, actor=None):
    status, detail = deliver(s, only_to=only_to)
    ReportSchedule.objects.filter(pk=s.pk).update(last_run_at=timezone.now(), last_status=status, last_detail=detail[:200])
    record("schedule.run", actor=actor, entity_type="report_schedule", entity_id=s.pk,
           outcome="success" if status in ("sent", "partial") else "denied", metadata={"status": status, "test": only_to is not None})
    return status, detail


def run_due(now=None):
    """Send every enabled schedule whose time has come. Safe to call from several processes at once and as often as you like."""
    now = now or timezone.now()
    done = []
    for s in ReportSchedule.objects.filter(enabled=True, next_run_at__isnull=False, next_run_at__lte=now).select_related("org_unit", "created_by"):
        claimed = ReportSchedule.objects.filter(pk=s.pk, next_run_at=s.next_run_at).update(next_run_at=compute_next(s, now))
        if claimed:
            done.append((s.name, *run(s)))
    return done
