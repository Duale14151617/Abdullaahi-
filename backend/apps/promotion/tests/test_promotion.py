"""Promotion eligibility by rank and time in rank. Synthetic data only."""
from datetime import date, timedelta

from apps.audit.models import AuditEvent
from apps.personnel import services as svc
from apps.personnel.models import PersonnelHistory
from apps.personnel.tests.test_personnel import PersonnelBase
from apps.promotion.models import PromotionRule
from apps.promotion.views import months_between

P = "/api/v1/promotion/"


class PromotionBase(PersonnelBase):
    def setUp(self):
        super().setUp()
        self.unit_a = self.unit("BARI-ARMY-A")
        self.cmdr = self.client_for(self.make_user("promo.cmdr", "unit_commander", unit="BARI-ARMY-A"))
        self.other = self.client_for(self.make_user("promo.other", "unit_commander", unit="BARI-ARMY-B"))
        self.sa = self.client_for(self.make_user("promo.sa", "super_admin"))
        self.rank = self.rank("PA-03")

    def set_rule(self, months=24, c=None, **kw):
        return (c or self.sa).post(P + "rules/set/", {"rank": str(self.rank.pk), "min_months": months, **kw}, format="json")

    def served(self, name, months, status="active"):
        """A person whose rank was entered this many months ago (the effective date of their first rank entry)."""
        return svc.create_personnel(None, full_name=name, phone="", service_number=None, org_unit=self.unit_a, rank=self.rank,
                                    status=status, effective_date=date.today() - timedelta(days=int(months * 30.5) + 2))

    def table(self, c=None, **params):
        r = (c or self.cmdr).get(P + "eligibility/", {"org_unit": str(self.unit_a.pk), **params})
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()


class MonthsTests(PromotionBase):
    def test_whole_months_only(self):
        self.assertEqual(months_between(date(2024, 1, 31), date(2024, 3, 30)), 1)
        self.assertEqual(months_between(date(2024, 1, 15), date(2024, 3, 15)), 2)
        self.assertEqual(months_between(date(2024, 5, 1), date(2024, 4, 1)), 0)


class RuleTests(PromotionBase):
    def test_only_the_super_administrator_sets_rules(self):
        self.assertEqual(self.set_rule(c=self.cmdr).status_code, 403)
        self.assertEqual(self.client_for(self.make_user("promo.hq", "hq_admin")).post(
            P + "rules/set/", {"rank": str(self.rank.pk), "min_months": 12}, format="json").status_code, 403)
        r = self.set_rule()
        self.assertEqual(r.status_code, 201, r.content)
        self.assertTrue(AuditEvent.objects.filter(action="promotion.rule_set").exists())

    def test_rule_values_are_validated(self):
        for bad in (0, 601, "12", True):
            self.assertEqual(self.set_rule(months=bad).status_code, 400, bad)
        self.assertEqual(self.set_rule(next_rank=str(self.rank.pk)).status_code, 400)  # must be more senior

    def test_a_rule_can_be_changed_and_removed(self):
        self.set_rule(24)
        self.set_rule(36)
        self.assertEqual(PromotionRule.objects.get(rank=self.rank).min_months, 36)
        r = self.sa.post(P + "rules/set/", {"rank": str(self.rank.pk), "remove": True}, format="json")
        self.assertEqual((r.status_code, PromotionRule.objects.count()), (204, 0))

    def test_rules_list_shows_ranks_with_and_without_a_rule(self):
        self.set_rule(18)
        rows = self.cmdr.get(P + "rules/").json()["results"]
        mine = next(x for x in rows if x["code"] == "PA-03")
        self.assertEqual(mine["rule"]["min_months"], 18)
        self.assertTrue(any(x["rule"] is None for x in rows))


class EligibilityTests(PromotionBase):
    def test_time_in_rank_against_the_rule(self):
        self.set_rule(24)
        old, new = self.served("Long Served", 40), self.served("Recent", 6)
        d = self.table()
        by = {r["personnel"]["id"]: r for r in d["results"]}
        self.assertEqual(by[str(old.pk)]["state"], "eligible")
        self.assertIsNotNone(by[str(old.pk)]["next_rank"])
        self.assertEqual((by[str(new.pk)]["state"], by[str(new.pk)]["required"]), ("not_yet", 24))
        self.assertTrue(0 < by[str(new.pk)]["months_left"] <= 24)
        self.assertEqual(by[str(old.pk)]["since_source"], "record")
        self.assertEqual({k: d["counts"][k] for k in ("eligible", "not_yet")}, {"eligible": 1, "not_yet": 1})

    def test_the_last_rank_change_decides_not_the_first_record(self):
        self.set_rule(24)
        p = self.served("Promoted Recently", 60)
        PersonnelHistory.objects.create(personnel=p, field="rank", old_ref="previous", new_ref=str(self.rank.pk), new_label="PA-03",
                                        effective_date=date.today() - timedelta(days=90), recorded_by_username="t")
        row = self.table()["results"][0]
        self.assertEqual((row["state"], row["since_source"]), ("not_yet", "history"))
        self.assertIn(row["months"], (2, 3))  # about 90 days

    def test_no_rule_means_no_verdict(self):
        self.served("Anyone", 100)
        d = self.table()
        self.assertEqual((d["results"][0]["state"], d["results"][0]["required"], d["counts"]["no_rule"]), ("no_rule", None, 1))

    def test_suspended_and_jailed_people_are_blocked_and_gone_people_are_left_out(self):
        self.set_rule(12)
        self.served("Suspended", 40, status="suspended")
        self.served("Retired", 40, status="retired")
        self.served("Fine", 40)
        d = self.table()
        self.assertEqual({r["personnel"]["full_name"]: r["state"] for r in d["results"]}, {"Suspended": "blocked", "Fine": "eligible"})

    def test_state_filter(self):
        self.set_rule(24)
        self.served("Long Served", 40)
        self.served("Recent", 6)
        self.assertEqual([r["personnel"]["full_name"] for r in self.table(state="eligible")["results"]], ["Long Served"])

    def test_scope_and_audit(self):
        self.set_rule(24)
        self.served("Long Served", 40)
        r = self.other.get(P + "eligibility/", {"org_unit": str(self.unit_a.pk)})
        self.assertEqual((r.status_code, r.json()["errors"]["org_unit"][0]["code"]), (400, "out_of_scope"))
        self.table()
        self.assertTrue(AuditEvent.objects.filter(action="promotion.read").exists())

    def test_a_member_cannot_see_eligibility(self):
        m = self.client_for(self.make_user("promo.member", "member", unit="BARI-ARMY-A"))
        self.assertEqual(m.get(P + "eligibility/", {"org_unit": str(self.unit_a.pk)}).status_code, 403)


class SuperAdministratorTests(PromotionBase):
    def test_sees_every_unit_and_can_set_the_rules(self):
        self.set_rule(12)
        self.served("Long Served", 40)
        d = self.table(c=self.sa)
        self.assertEqual(d["counts"]["eligible"], 1)
        far = self.client_for(self.make_user("promo.far", "unit_commander", unit="NUGAAL-ARMY-A"))
        self.assertEqual(far.get(P + "eligibility/", {"org_unit": str(self.unit_a.pk)}).status_code, 400)  # others stay in their scope
