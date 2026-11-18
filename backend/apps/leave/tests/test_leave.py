"""Leave requests and the duty roster. Synthetic data only."""
from datetime import date, timedelta

from apps.audit.models import AuditEvent
from apps.leave.models import LeaveRequest
from apps.personnel.models import Personnel
from apps.personnel.tests.test_personnel import PersonnelBase

L = "/api/v1/leave/"
R = "/api/v1/roster/"


class LeaveBase(PersonnelBase):
    def setUp(self):
        super().setUp()
        self.day = date.today() + timedelta(days=10)
        self.unit_a = self.unit("BARI-ARMY-A")
        self.cmdr_user = self.make_user("leave.cmdr", "unit_commander", unit="BARI-ARMY-A")
        self.cmdr = self.client_for(self.cmdr_user)
        self.other_cmdr = self.client_for(self.make_user("leave.other", "unit_commander", unit="BARI-ARMY-B"))
        self.p1 = self.person("BARI-ARMY-A", "Soldier One")
        self.p2 = self.person("BARI-ARMY-A", "Soldier Two")
        self.member_user = self.make_user("leave.member", "member", unit="BARI-ARMY-A")
        Personnel.objects.filter(pk=self.p1.pk).update(account_id=self.member_user.pk)
        self.member = self.client_for(self.member_user)

    def lbody(self, days=3, **kw):
        b = {"leave_type": "annual", "start_date": self.day.isoformat(), "end_date": (self.day + timedelta(days=days - 1)).isoformat(),
             "reason": "Family matter"}
        b.update(kw)
        return b

    def ask(self, c=None, **kw):
        return (c or self.member).post(L, self.lbody(**kw), format="json")


class LeaveRequestTests(LeaveBase):
    def test_a_member_requests_their_own_leave(self):
        r = self.ask()
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual((r.json()["status"], r.json()["days"], r.json()["personnel"]["id"]), ("pending", 3, str(self.p1.pk)))
        self.assertTrue(AuditEvent.objects.filter(action="leave.requested").exists())

    def test_overlapping_requests_for_one_person_are_refused(self):
        self.assertEqual(self.ask().status_code, 201)
        r = self.ask(days=2)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.codes(r, "start_date"), ["overlap"])

    def test_dates_and_type_are_validated(self):
        r = self.ask(end_date=(self.day - timedelta(days=1)).isoformat())
        self.assertEqual(self.codes(r, "end_date"), ["end_before_start"])
        self.assertEqual(self.codes(self.ask(leave_type="nope"), "leave_type"), ["invalid_type"])
        self.assertEqual(self.codes(self.ask(days=31), "end_date"), ["too_long"])  # annual leave is capped at 30 days

    def test_a_commander_can_ask_for_someone_in_scope_only(self):
        ok = self.cmdr.post(L, self.lbody(personnel=str(self.p2.pk)), format="json")
        self.assertEqual(ok.status_code, 201, ok.content)
        out = self.other_cmdr.post(L, self.lbody(personnel=str(self.p2.pk)), format="json")
        self.assertEqual(self.codes(out, "personnel"), ["person_unknown"])

    def test_a_member_cannot_ask_for_someone_else(self):
        r = self.member.post(L, self.lbody(personnel=str(self.p2.pk)), format="json")
        self.assertEqual(self.codes(r, "personnel"), ["person_unknown"])

    def test_members_see_only_their_own_requests(self):
        self.ask()
        self.cmdr.post(L, self.lbody(days=2, start_date=(self.day + timedelta(days=20)).isoformat(),
                                    end_date=(self.day + timedelta(days=21)).isoformat(), personnel=str(self.p2.pk)), format="json")
        mine = self.member.get(L).json()["results"]
        self.assertEqual([x["personnel"]["id"] for x in mine], [str(self.p1.pk)])
        self.assertEqual(len(self.cmdr.get(L).json()["results"]), 2)
        self.assertEqual(self.other_cmdr.get(L).json()["results"], [])


class LeaveDecisionTests(LeaveBase):
    def test_commander_approves_and_the_decision_is_recorded(self):
        rid = self.ask().json()["id"]
        r = self.cmdr.post(f"{L}{rid}/approve/", {}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((r.json()["status"], r.json()["decided_by"]), ("approved", "leave.cmdr"))
        self.assertEqual(self.cmdr.post(f"{L}{rid}/approve/", {}, format="json").status_code, 409)  # decided once
        self.assertTrue(AuditEvent.objects.filter(action="leave.approved").exists())

    def test_rejecting_needs_a_reason(self):
        rid = self.ask().json()["id"]
        r = self.cmdr.post(f"{L}{rid}/reject/", {}, format="json")
        self.assertEqual(self.codes(r, "note"), ["note_required"])
        r = self.cmdr.post(f"{L}{rid}/reject/", {"note": "Unit is short-handed"}, format="json")
        self.assertEqual(r.json()["status"], "rejected")

    def test_a_member_cannot_decide(self):
        rid = self.ask().json()["id"]
        self.assertEqual(self.member.post(f"{L}{rid}/approve/", {}, format="json").status_code, 403)

    def test_a_commander_outside_the_unit_cannot_decide(self):
        rid = self.ask().json()["id"]
        self.assertEqual(self.other_cmdr.post(f"{L}{rid}/approve/", {}, format="json").status_code, 404)

    def test_nobody_decides_their_own_request(self):
        Personnel.objects.filter(pk=self.p2.pk).update(account_id=self.cmdr_user.pk)
        rid = self.ask(c=self.cmdr, personnel=str(self.p2.pk)).json()["id"]
        r = self.cmdr.post(f"{L}{rid}/approve/", {}, format="json")
        self.assertEqual((r.status_code, r.json()["code"]), (403, "own_request"))
        self.assertEqual(LeaveRequest.objects.get(pk=rid).status, "pending")

    def test_cancel(self):
        rid = self.ask().json()["id"]
        self.assertEqual(self.member.post(f"{L}{rid}/cancel/", {}, format="json").json()["status"], "cancelled")
        self.assertEqual(self.member.post(f"{L}{rid}/cancel/", {}, format="json").status_code, 409)
        self.assertEqual(self.ask().status_code, 201)  # cancelled leave no longer blocks the dates


class RosterTests(LeaveBase):
    def entry(self, p, day=None, shift="day", **kw):
        return {"personnel": str(p.pk), "date": (day or self.day).isoformat(), "shift": shift, **kw}

    def assign(self, entries, c=None, unit=None):
        return (c or self.cmdr).post(R + "assign/", {"org_unit": str((unit or self.unit_a).pk), "entries": entries}, format="json")

    def test_assign_and_read_back_with_leave(self):
        self.assertEqual(self.assign([self.entry(self.p1, post="Gate 1"), self.entry(self.p2, shift="night")]).status_code, 201)
        self.assertEqual(self.assign([self.entry(self.p1, shift="standby")]).status_code, 201)  # rewriting a day replaces it
        rid = self.ask(start_date=(self.day + timedelta(days=2)).isoformat(), end_date=(self.day + timedelta(days=3)).isoformat()).json()["id"]
        self.cmdr.post(f"{L}{rid}/approve/", {}, format="json")
        d = self.cmdr.get(R, {"org_unit": str(self.unit_a.pk), "from": self.day.isoformat(),
                             "to": (self.day + timedelta(days=6)).isoformat()}).json()
        self.assertEqual({(s["personnel"], s["shift"]) for s in d["shifts"]},
                         {(str(self.p1.pk), "standby"), (str(self.p2.pk), "night")})
        self.assertEqual([(x["personnel"], x["type"]) for x in d["leave"]], [(str(self.p1.pk), "annual")])
        self.assertEqual(len(d["people"]), 2)

    def test_nobody_on_approved_leave_can_be_rostered(self):
        rid = self.ask().json()["id"]
        self.cmdr.post(f"{L}{rid}/approve/", {}, format="json")
        r = self.assign([self.entry(self.p2), self.entry(self.p1)])
        self.assertEqual((r.status_code, r.json()["rows"]), (400, [{"index": 1, "code": "on_leave"}]))
        self.assertEqual(self.assign([self.entry(self.p1, shift="off")]).status_code, 201)  # a day off is fine

    def test_bad_entries_save_nothing(self):
        r = self.assign([self.entry(self.p1), self.entry(self.p1), {"personnel": str(self.p2.pk), "date": "nope", "shift": "day"},
                         self.entry(self.p2, shift="weekend")])
        self.assertEqual((r.status_code, [x["code"] for x in r.json()["rows"]]), (400, ["duplicate_entry", "invalid_date", "invalid_shift"]))
        d = self.cmdr.get(R, {"org_unit": str(self.unit_a.pk)}).json()
        self.assertEqual(d["shifts"], [])

    def test_scope_is_enforced(self):
        self.assertEqual(self.assign([self.entry(self.p1)], c=self.other_cmdr).status_code, 400)
        self.assertEqual(self.other_cmdr.get(R, {"org_unit": str(self.unit_a.pk)}).status_code, 400)
        self.assertEqual(self.member.get(R, {"org_unit": str(self.unit_a.pk)}).status_code, 403)


A = "/api/v1/attendance/"


class LeaveInAttendanceTests(LeaveBase):
    """Approved leave counts as "on leave" in attendance until the day is recorded; a recorded day always wins."""

    def approve(self, start, end, c=None):
        rid = self.ask(start_date=start.isoformat(), end_date=end.isoformat()).json()["id"]
        self.assertEqual(self.cmdr.post(f"{L}{rid}/approve/", {}, format="json").status_code, 200)
        return rid

    def roll(self, day):
        rows = self.cmdr.get(A + "roll/", {"org_unit": str(self.unit_a.pk), "date": day.isoformat()}).json()["results"]
        return {r["id"]: r for r in rows}

    def summary(self, day):
        return self.cmdr.get(A + "summary/", {"org_unit": str(self.unit_a.pk), "period": "day", "date": day.isoformat()}).json()

    def test_the_roll_shows_approved_leave_and_nothing_else(self):
        self.approve(self.day, self.day + timedelta(days=2))
        rows = self.roll(self.day + timedelta(days=1))
        self.assertEqual(rows[str(self.p1.pk)]["leave"]["type"], "annual")
        self.assertIsNone(rows[str(self.p1.pk)]["attendance"])
        self.assertIsNone(rows[str(self.p2.pk)]["leave"])
        self.assertIsNone(self.roll(self.day + timedelta(days=3))[str(self.p1.pk)]["leave"])  # the day after the leave ends

    def test_leave_counts_in_the_totals_without_being_recorded(self):
        self.approve(self.day, self.day + timedelta(days=2))
        s = self.summary(self.day + timedelta(days=1))
        self.assertEqual((s["on_leave"], s["recorded"], s["not_recorded"]), (1, 1, 1))

    def test_pending_rejected_or_cancelled_leave_does_not_count(self):
        rid = self.ask().json()["id"]
        self.assertEqual(self.summary(self.day)["on_leave"], 0)  # still pending
        self.cmdr.post(f"{L}{rid}/approve/", {}, format="json")
        self.assertEqual(self.summary(self.day)["on_leave"], 1)
        self.member.post(f"{L}{rid}/cancel/", {}, format="json")
        self.assertEqual(self.summary(self.day)["on_leave"], 0)  # nothing to undo: leave was never copied into attendance

    def test_a_recorded_day_wins_over_leave(self):
        today = date.today()
        self.approve(today, today + timedelta(days=1))
        self.assertEqual(self.summary(today)["on_leave"], 1)
        r = self.cmdr.post(A + "record/", {"org_unit": str(self.unit_a.pk), "date": today.isoformat(),
                                          "entries": [{"personnel": str(self.p1.pk), "status": "present"}]}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        s = self.summary(today)
        self.assertEqual((s["present"], s["on_leave"], s["recorded"]), (1, 0, 1))  # called back early: counted once, as present
        self.assertIsNone(self.roll(today)[str(self.p1.pk)]["leave"])

    def test_the_situation_figures_use_it_too(self):
        from apps.attendance import situation
        self.approve(self.day, self.day)
        q = Personnel.objects.filter(org_unit__path__startswith=self.unit_a.path)
        from apps.attendance.models import AttendanceRecord
        from apps.leave import services
        self.assertEqual(services.unrecorded_leave_days(q, self.day, self.day, AttendanceRecord), 1)
        self.assertEqual(services.unrecorded_leave_days(q, self.day + timedelta(days=1), self.day + timedelta(days=1), AttendanceRecord), 0)
        self.assertIn("attendance_on_leave", situation.FIGURE_KEYS)
