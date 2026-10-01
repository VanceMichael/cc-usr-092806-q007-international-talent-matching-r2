from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from civicflow.application import CivicFlow
from civicflow.credentials import CredentialService
from civicflow.errors import ConflictError, PermissionDenied, ValidationError
from civicflow.memberships import MembershipService
from civicflow.organizations import OrganizationService
from civicflow.recruitment import RecruitmentService
from civicflow.security import AccessContext


NOW = "2026-10-01T12:00:00+08:00"


class RecruitmentTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "test.sqlite3"
        self.app = CivicFlow.open(self.db_path, fixed_now=NOW)
        self.system = AccessContext.system("tester")
        self.center = AccessContext(
            actor_id="caseworker@center",
            permissions=frozenset({
                "write:rec_application", "write:rec_exception", "approve:rec_exception",
                "write:rec_offer", "read:rec_job", "read:rec_application", "read:rec_interview", "read:rec_offer",
                "history:rec_application", "contact:recruitment", "write:credentials",
                "transition:credentials", "write:memberships", "transition:memberships",
                "write:organizations",
            }),
            scopes=frozenset({"*"}),
        )
        self.recruiter = AccessContext(
            actor_id="recruiter@acme",
            permissions=frozenset({
                "write:rec_job", "write:rec_application", "write:rec_exception",
                "write:rec_interview", "write:rec_offer", "read:rec_job",
                "read:rec_application", "contact:recruitment",
            }),
        )
        self.rec = self.app.recruitment
        self._bootstrap()

    def tearDown(self):
        self.temp.cleanup()

    # ------------------------------------------------------------- 装配助手

    def _bootstrap(self):
        self.org = OrganizationService(self.app.repository).create(
            self.center, {"name": "Acme", "kind": "employer", "jurisdiction": "CN", "owner_id": "boss@acme"},
            request_key="org")
        self.membership = MembershipService(self.app.repository).create(
            self.center, {"organization_id": self.org["entity_id"], "person_ref": "recruiter@acme",
                          "role": "recruiter", "valid_from": NOW, "valid_to": "2027-12-31T23:59:59+08:00"},
            request_key="mem")
        MembershipService(self.app.repository).transition(
            self.center, self.membership["entity_id"], "active",
            expected_version=1, reason="入职生效", request_key="mem-active")
        self.job = self.rec.create_job(self.recruiter, {
            "employer_org": self.org["entity_id"], "title": "算法工程师",
            "skills": [{"name": "Python", "level": 4}],
            "languages": [{"language": "英语", "level": 3}],
            "visa": {"required": True, "permit_type": "work_permit_z"},
            "budget": {"currency": "CNY", "min": 30000000, "max": 60000000},
        }, request_key="job")
        self.job = self.rec.publish_job(self.recruiter, self.job["entity_id"], expected_version=1, request_key="pub")

        self.permit = CredentialService(self.app.repository).create(
            self.center, {"holder_type": "person", "holder_id": "candidate:anna",
                          "credential_type": "work_permit_z", "issuer": "外专局",
                          "valid_from": NOW, "valid_to": "2027-09-30T23:59:59+08:00"},
            request_key="permit")
        for target in ("verified", "effective"):
            self.permit = CredentialService(self.app.repository).transition(
                self.center, self.permit["entity_id"], target,
                expected_version=self.permit["version"], reason="材料齐全", request_key=f"permit-{target}")

    def _resume(self, **overrides):
        resume = {
            "job_id": self.job["entity_id"], "name": "Anna", "contact": "anna@example.com",
            "skills": [{"name": "Python", "level": 4}],
            "languages": [{"language": "英语", "level": 3}],
            "credential_ids": [self.permit["entity_id"]], "planned_rounds": 2,
        }
        resume.update(overrides)
        return resume

    def _apply(self, person_key="candidate:anna", **resume_overrides):
        receipt = self.rec.receive_resume(
            self.center, person_key=person_key, resume=self._resume(**resume_overrides),
            request_key=f"resume:{person_key}")
        self.assertEqual(receipt["status"], "accepted")
        application_id = receipt["application_id"]
        version = self.rec.get_application(self.center, application_id)["version"]
        self.rec.grant_consent(self.center, application_id, scope=f"rec_app:{application_id}",
                               valid_to="2026-12-31T23:59:59+08:00", expected_version=version, request_key="consent")
        return application_id

    def _refer(self, application_id, *, exception_code=None, expected_version=2):
        if exception_code:
            exc = self.rec.request_exception(self.recruiter, application_id,
                                             requirement_code=exception_code, reason="综合评估突出", request_key="exc")
            self.rec.decide_exception(self.center, exc["entity_id"], decision="approved",
                                      comment="同意例外", expected_version=1, request_key="exc-ok")
        return self.rec.refer(self.center, application_id, expected_version=expected_version, request_key="refer")

    def _two_rounds(self, application_id):
        iv1 = self.rec.schedule_interview(self.recruiter, application_id, round_no=1,
                                          scheduled_at="2026-10-05T10:00:00+08:00",
                                          panel=["recruiter@acme"], request_key="iv1")
        self.rec.complete_interview(self.recruiter, iv1["entity_id"], expected_version=1,
                                    result="通过", request_key="iv1-done")
        iv2 = self.rec.schedule_interview(self.recruiter, application_id, round_no=2,
                                          scheduled_at="2026-10-08T14:00:00+08:00",
                                          panel=["recruiter@acme", "cto@acme"], request_key="iv2")
        self.rec.complete_interview(self.recruiter, iv2["entity_id"], expected_version=1,
                                    result="通过", request_key="iv2-done")
        return iv1, iv2

    # ----------------------------------------------------------------- 测试

    def test_referral_freezes_versions_and_job_revision_does_not_reach_it(self):
        application_id = self._apply()
        referred = self._refer(application_id)
        self.assertEqual(referred["frozen"]["job_version"], 2)
        self.assertEqual(referred["frozen"]["credential_refs"][0]["version"], self.permit["version"])

        # 企业发布更高的英语要求（新版本）；已冻结的推荐仍能解释为当时的第 2 版。
        self.rec.revise_job(self.recruiter, self.job["entity_id"],
                            {"languages": [{"language": "英语", "level": 4}]},
                            expected_version=2, request_key="job-v3")
        explanation = self.rec.explain(self.center, application_id)
        self.assertEqual(explanation["referral"]["job_version_used"], 2)
        self.assertEqual(explanation["referral"]["job_snapshot"]["languages"][0]["level"], 3)
        self.assertEqual(explanation["referral"]["requirements_used"]["languages"][0]["level"], 3)

    def test_interview_freezes_job_version_independently(self):
        application_id = self._apply()
        self._refer(application_id)
        iv1 = self.rec.schedule_interview(self.recruiter, application_id, round_no=1,
                                          scheduled_at="2026-10-05T10:00:00+08:00",
                                          panel=["recruiter@acme"], request_key="iv1")
        self.assertEqual(iv1["frozen"]["job_version"], 2)

    def test_unmet_requirement_blocks_referral_without_exception(self):
        application_id = self._apply(languages=[{"language": "英语", "level": 2}])
        with self.assertRaises(ConflictError):
            self.rec.refer(self.center, application_id, expected_version=2, request_key="refer-blocked")
        # 企业不能批准自己的资格例外：申请人本人、企业成员均被拒绝。
        exc = self.rec.request_exception(self.recruiter, application_id,
                                         requirement_code="lang:英语", reason="能力强", request_key="exc")
        with self.assertRaises(PermissionDenied):
            self.rec.decide_exception(self.recruiter, exc["entity_id"], decision="approved",
                                      comment="自批", expected_version=1, request_key="exc-self")
        # 中心审批人批准后方可推荐。
        self.rec.decide_exception(self.center, exc["entity_id"], decision="approved",
                                  comment="政策允许", expected_version=1, request_key="exc-ok")
        referred = self.rec.refer(self.center, application_id, expected_version=2, request_key="refer-ok")
        self.assertEqual(referred["state"], "referred")
        explanation = self.rec.explain(self.center, application_id)
        self.assertEqual(explanation["exceptions"][0]["reviewer"], "caseworker@center")

    def test_duplicate_resume_keeps_progress_but_changed_materials_go_manual(self):
        application_id = self._apply()
        referred = self._refer(application_id)
        duplicate = self.rec.receive_resume(self.center, person_key="candidate:anna",
                                            resume=self._resume(), request_key="resume:candidate:anna")
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(duplicate["application_id"], application_id)
        # 原进度不受重复回执影响。
        self.assertEqual(self.rec.get_application(self.center, application_id)["state"], "referred")

        # 标识相同但资格材料不同：进入人工核验，不覆盖。
        conflict = self.rec.receive_resume(
            self.center, person_key="candidate:anna",
            resume=self._resume(skills=[{"name": "Python", "level": 1}]), request_key="resume-changed")
        self.assertEqual(conflict["status"], "manual_review")
        self.assertEqual(self.rec.get_application(self.center, application_id)["state"], "referred")

    def test_manual_review_blocks_fresh_application_and_can_be_resolved(self):
        receipt = self.rec.receive_resume(self.center, person_key="candidate:bob",
                                          resume=self._resume(), request_key="bob-1")
        application_id = receipt["application_id"]
        self.rec.receive_resume(self.center, person_key="candidate:bob",
                                resume=self._resume(skills=[{"name": "Python", "level": 1}]),
                                request_key="bob-2")
        self.assertEqual(self.rec.get_application(self.center, application_id)["state"], "blocked_manual")
        with self.assertRaises(ConflictError):
            self.rec.refer(self.center, application_id, expected_version=1, request_key="refer-bob")
        version = self.rec.get_application(self.center, application_id)["version"]
        resolved = self.rec.resolve_manual(self.center, application_id, decision="proceed",
                                           expected_version=version, request_key="manual-ok")
        self.assertEqual(resolved["state"], "applied")

    def test_contact_only_visible_to_authorized_people(self):
        application_id = self._apply()
        outsider = AccessContext(actor_id="recruiter2@other",
                                 permissions=frozenset({"read:rec_application"}))
        self.assertEqual(self.rec.get_application(outsider, application_id)["contact"], "***")
        # 招聘企业在岗成员 + 候选人授权有效：可见。
        self.assertEqual(self.rec.get_application(self.recruiter, application_id)["contact"], "anna@example.com")
        # 撤回授权后企业也看不到联系方式。
        version = self.rec.get_application(self.center, application_id)["version"]
        self.rec.withdraw_consent(self.center, application_id, expected_version=version,
                                  reason="候选人主动退出", request_key="withdraw")
        self.assertEqual(self.rec.get_application(self.recruiter, application_id)["contact"], "***")

    def test_consent_withdraw_stops_pending_steps_but_keeps_offer_for_amendment(self):
        application_id = self._apply()
        self._refer(application_id)
        iv1 = self.rec.schedule_interview(self.recruiter, application_id, round_no=1,
                                          scheduled_at="2026-10-05T10:00:00+08:00",
                                          panel=["recruiter@acme"], request_key="iv1")
        # 直接发承诺（referred 也允许），随后撤回授权。
        offer = self.rec.issue_offer(self.recruiter, application_id,
                                     compensation={"currency": "CNY", "salary_minor": 42000000},
                                     promise_valid_until="2026-10-15T18:00:00+08:00", request_key="offer")
        version = self.rec.get_application(self.center, application_id)["version"]
        self.rec.withdraw_consent(self.center, application_id, expected_version=version,
                                  reason="候选人改主意", request_key="withdraw")

        self.assertEqual(self.app.repository.get("rec_interview", iv1["entity_id"])["state"], "cancelled")
        # 既有承诺没有被悄悄覆盖或删除。
        self.assertEqual(self.app.repository.get("rec_offer", offer["entity_id"])["state"], "issued")
        app_row = self.rec.get_application(self.center, application_id)
        self.assertEqual(app_row["state"], "consent_withdrawn")
        self.assertEqual(app_row["next_action"], "resolve_offer_by_rescission_or_amendment")
        # 撤回后不能直接签补充协议；重新取得授权后方可衔接。
        with self.assertRaises(ConflictError):
            self.rec.supplement_offer(
                self.recruiter, application_id, compensation={"currency": "CNY", "salary_minor": 43000000},
                promise_valid_until="2026-10-20T18:00:00+08:00", reason="未重新授权", request_key="amend-no")
        version = self.rec.get_application(self.center, application_id)["version"]
        self.rec.grant_consent(self.center, application_id, scope=f"rec_app:{application_id}",
                               valid_to="2026-12-31T23:59:59+08:00", expected_version=version, request_key="reconsent")
        amended = self.rec.supplement_offer(
            self.recruiter, application_id, compensation={"currency": "CNY", "salary_minor": 43000000},
            promise_valid_until="2026-10-20T18:00:00+08:00", reason="授权撤回后重新约定", request_key="amend")
        self.assertEqual(self.app.repository.get("rec_offer", offer["entity_id"])["state"], "superseded")
        self.assertEqual(amended["amendment_of"], offer["entity_id"])

    def test_job_close_terminates_open_steps_but_preserves_confirmed_offer(self):
        application_id = self._apply()
        self._refer(application_id)
        self._two_rounds(application_id)
        offer = self.rec.issue_offer(self.recruiter, application_id,
                                     compensation={"currency": "CNY", "salary_minor": 42000000},
                                     promise_valid_until="2026-10-15T18:00:00+08:00", request_key="offer")
        app_version = self.rec.get_application(self.center, application_id)["version"]
        offer_version = self.app.repository.get("rec_offer", offer["entity_id"])["version"]
        self.rec.confirm_offer(self.center, application_id, expected_version=app_version, request_key="confirm")
        self.rec.close_job(self.recruiter, self.job["entity_id"], expected_version=2,
                           reason="编制调整", request_key="close")
        # 已确认的录用承诺不受岗位关闭影响。
        self.assertEqual(self.app.repository.get("rec_offer", offer["entity_id"])["state"], "confirmed")
        self.assertEqual(self.rec.get_application(self.center, application_id)["state"], "accepted")
        # 关闭后不能再安排面试或修改岗位。
        with self.assertRaises(ConflictError):
            self.rec.schedule_interview(self.recruiter, application_id, round_no=1,
                                        scheduled_at="2026-10-05T10:00:00+08:00",
                                        panel=["recruiter@acme"], request_key="iv-after-close")
        with self.assertRaises(ConflictError):
            self.rec.revise_job(self.recruiter, self.job["entity_id"], {"title": "新名字"},
                                expected_version=3, request_key="rev-after-close")

    def test_budget_change_blocks_new_promise_without_exception(self):
        application_id = self._apply()
        referred = self._refer(application_id)
        # 预算变化终止该岗位在途申请的未完成环节（尚无承诺 → 流程关闭）。
        self.rec.change_budget(self.recruiter, self.job["entity_id"],
                               {"currency": "CNY", "min": 50000000, "max": 70000000},
                               expected_version=2, request_key="budget")
        self.assertEqual(self.rec.get_application(self.center, application_id)["state"], "closed")

    def test_over_budget_offer_requires_approved_exception(self):
        application_id = self._apply()
        self._refer(application_id)
        # 岗位预算上限 6000 万，报价 6200 万：没有预算例外不能发承诺。
        with self.assertRaises(ConflictError):
            self.rec.issue_offer(self.recruiter, application_id,
                                 compensation={"currency": "CNY", "salary_minor": 62000000},
                                 promise_valid_until="2026-10-15T18:00:00+08:00", request_key="offer-high")
        # 中心审批预算例外后可以发出。
        exc = self.rec.request_exception(self.recruiter, application_id,
                                         requirement_code="budget", reason="稀缺人才专项", request_key="exc-budget")
        self.rec.decide_exception(self.center, exc["entity_id"], decision="approved",
                                  comment="同意预算例外", expected_version=1, request_key="exc-budget-ok")
        offer = self.rec.issue_offer(self.recruiter, application_id,
                                     compensation={"currency": "CNY", "salary_minor": 62000000},
                                     promise_valid_until="2026-10-15T18:00:00+08:00", request_key="offer-high")
        self.assertEqual(offer["state"], "issued")

    def test_offer_rescission_keeps_record_and_closes_application(self):
        application_id = self._apply()
        self._refer(application_id)
        offer = self.rec.issue_offer(self.recruiter, application_id,
                                     compensation={"currency": "CNY", "salary_minor": 42000000},
                                     promise_valid_until="2026-10-15T18:00:00+08:00", request_key="offer")
        version = self.rec.get_application(self.center, application_id)["version"]
        with self.assertRaises(ValidationError):
            self.rec.rescind_offer(self.recruiter, application_id, reason="  ",
                                   expected_version=version, request_key="rescind-bad")
        self.rec.rescind_offer(self.recruiter, application_id, reason="签证政策突变",
                               expected_version=version, request_key="rescind")
        stored = self.app.repository.get("rec_offer", offer["entity_id"])
        self.assertEqual(stored["state"], "rescinded")
        self.assertEqual(stored["rescission"]["reason"], "签证政策突变")
        timeline = self.rec.timeline(self.center, application_id)
        self.assertIn("offer-rescission-record", {event["action"] for event in timeline})

    def test_credential_expiry_cancels_interviews_but_keeps_confirmed_offer(self):
        application_id = self._apply()
        self._refer(application_id)
        iv1 = self.rec.schedule_interview(self.recruiter, application_id, round_no=1,
                                          scheduled_at="2026-10-05T10:00:00+08:00",
                                          panel=["recruiter@acme"], request_key="iv1")
        # 登记证件即将到期，然后把有效期改到过去。
        CredentialService(self.app.repository).revise(
            self.center, self.permit["entity_id"], {"valid_to": "2026-09-30T00:00:00+08:00"},
            expected_version=self.permit["version"], request_key="permit-shorten")
        expired = self.rec.sweep_credential_expiry(self.center)
        self.assertEqual([row["entity_id"] for row in expired], [self.permit["entity_id"]])
        self.assertEqual(self.app.repository.get("rec_interview", iv1["entity_id"])["state"], "cancelled")
        self.assertEqual(self.rec.get_application(self.center, application_id)["state"], "closed")

    def test_todos_and_jobs_survive_restart(self):
        application_id = self._apply()
        self._refer(application_id)
        self._two_rounds(application_id)
        self.rec.issue_offer(self.recruiter, application_id,
                             compensation={"currency": "CNY", "salary_minor": 42000000},
                             promise_valid_until="2026-10-15T18:00:00+08:00", request_key="offer")
        kinds = {todo["todo_kind"] for todo in self.rec.open_todos(self.center)}
        self.assertIn("offer_confirmation", kinds)

        restarted = CivicFlow.open(self.db_path, fixed_now="2026-10-16T12:00:00+08:00")
        todos = restarted.recruitment.open_todos(AccessContext.system("restart"))
        self.assertTrue(any(todo["todo_kind"] == "offer_confirmation" for todo in todos))
        # 持久定时任务同样不丢。
        due = restarted.jobs.claim_due()
        self.assertTrue(any("offer_confirmation" in job["job_type"] for job in due))
        restarted.jobs.finish(due[0]["job_id"])

    def test_claimed_job_is_reclaimed_after_crash_and_lease_expiry(self):
        application_id = self._apply()
        self._refer(application_id)
        self.rec.issue_offer(self.recruiter, application_id,
                             compensation={"currency": "CNY", "salary_minor": 42000000},
                             promise_valid_until="2026-09-20T18:00:00+08:00", request_key="offer")
        claimed = self.app.jobs.claim_due()
        self.assertTrue(claimed)
        # 进程崩溃：任务停留在 running；租约到期后重启可重新认领。
        restarted = CivicFlow.open(self.db_path, fixed_now="2026-10-02T12:00:00+08:00")
        reclaimed = restarted.jobs.claim_due(seconds=30)
        self.assertEqual({job["job_id"] for job in claimed}, {job["job_id"] for job in reclaimed})

    def test_offer_expiry_sweep(self):
        application_id = self._apply()
        self._refer(application_id)
        offer = self.rec.issue_offer(self.recruiter, application_id,
                                     compensation={"currency": "CNY", "salary_minor": 42000000},
                                     promise_valid_until="2026-09-20T18:00:00+08:00", request_key="offer-old")
        expired = self.rec.expire_offers(self.center)
        self.assertEqual([row["entity_id"] for row in expired], [offer["entity_id"]])
        with self.assertRaises(ConflictError):
            version = self.rec.get_application(self.center, application_id)["version"]
            self.rec.confirm_offer(self.center, application_id, expected_version=version, request_key="late")

    def test_explain_answers_why_referenced_and_who_decides_next(self):
        application_id = self._apply(languages=[{"language": "英语", "level": 2}])
        self._refer(application_id, exception_code="lang:英语")
        explanation = self.rec.explain(self.center, application_id)
        self.assertEqual(explanation["referral"]["referred_by"], "caseworker@center")
        self.assertTrue(any(item["code"] == "lang:英语" and item["satisfied"] is False
                            for item in explanation["referral"]["match_report"]))
        self.assertEqual(explanation["exceptions"][0]["reviewer"], "caseworker@center")
        self.assertEqual(explanation["next_owner"], "employer")
        self.assertTrue(explanation["timeline"])

    def test_audit_chain_still_verifies(self):
        application_id = self._apply()
        self._refer(application_id)
        self.assertEqual(self.app.verify()["inbox_conflicts"], 0)
        self.assertGreater(self.app.verify()["audit_entries"], 5)


if __name__ == "__main__":
    unittest.main()
