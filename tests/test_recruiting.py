from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from civicflow.application import CivicFlow
from civicflow.credentials import CredentialService
from civicflow.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from civicflow.memberships import MembershipService
from civicflow.organizations import OrganizationService
from civicflow.recruiting import RecruitingService
from civicflow.security import AccessContext


def perms(*items: str) -> frozenset[str]:
    return frozenset(items)


class RecruitingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "recruiting.sqlite3"
        self.app = CivicFlow.open(self.db_path, fixed_now="2026-10-01T09:00:00+08:00")
        self.center = AccessContext.system("talent-center")
        self.recruiter = AccessContext(actor_id="person:recruiter-a", permissions=perms(
            "write:recruiting_jobs", "transition:recruiting_jobs", "read:recruiting_jobs",
            "history:recruiting_jobs", "write:candidates", "read:candidates",
            "write:candidate_consents", "withdraw:candidate_consents",
            "write:referrals", "read:referrals", "write:interviews",
            "write:offers", "read:offers", "withdraw:offers",
            "write:offer_amendments", "write:eligibility_exceptions",
        ))
        self.recruiter_c = AccessContext(actor_id="person:recruiter-c", permissions=perms(
            "read:recruiting_jobs", "approve:eligibility_exceptions", "read:referrals",
        ))
        self.candidate_actor = AccessContext(actor_id="person:candidate-li", permissions=perms(
            "confirm:offers", "decide:offer_amendments",
        ))
        self.svc = RecruitingService(self.app.repository, self.app.jobs, self.app.outbox)
        self._setup_org()

    def tearDown(self):
        self.temp.cleanup()

    # ------------------------------------------------------------------ #
    # 装配辅助
    # ------------------------------------------------------------------ #

    def _setup_org(self):
        orgs = OrganizationService(self.app.repository)
        memberships = MembershipService(self.app.repository)
        self.org = orgs.create(self.center, {"name": "数智科技", "kind": "enterprise", "jurisdiction": "CN", "owner_id": "person:hr-lead"}, request_key="org")
        for actor in ("person:recruiter-a", "person:recruiter-c"):
            membership = memberships.create(self.center, {"organization_id": self.org["entity_id"], "person_ref": actor, "role": "recruiter", "valid_from": self.app.clock.now(), "valid_to": "2027-12-31T00:00:00Z"}, request_key=f"mem-{actor}")
            memberships.transition(self.center, membership["entity_id"], "active", expected_version=1, reason="入职", request_key=f"mem-{actor}:active")

    def make_job(self, *, skills=("python", "ml"), languages=("en",), permit=("work-permit-z",), key="job", budget_amount=1200000, openings=2):
        job = self.svc.create_job(self.recruiter, {
            "employer_org": self.org["entity_id"], "title": "高级算法工程师",
            "skills": list(skills),
            "languages": [{"code": code, "min_level": "b2"} for code in languages],
            "work_permit": {"required_types": list(permit), "jurisdiction": "CN"},
            "salary_band": {"amount": 600000, "currency": "CNY", "min": 400000, "max": 800000},
            "budget": {"amount": budget_amount, "currency": "CNY", "openings": openings},
        }, request_key=key)
        return self.svc.transition_job(self.recruiter, job["entity_id"], "open", expected_version=1, reason="开放", request_key=key + ":open")

    def make_candidate(self, key="cand", contact="liming@example.com"):
        return self.svc.register_candidate(self.recruiter, {"full_name": "李明", "contact": contact}, request_key=key)

    def grant_consent(self, candidate_id, *, audience=("person:recruiter-a",), valid_until="2027-06-30T00:00:00Z", key="consent"):
        return self.svc.grant_consent(self.recruiter, candidate_id, {"audience": list(audience), "scopes": ["contact", "referral"], "valid_until": valid_until, "granted_by": "person:candidate-li"}, request_key=key)

    def make_credential(self, candidate_id, credential_type, *, valid_to="2027-12-31T00:00:00Z", key="cred", state="effective"):
        service = CredentialService(self.app.repository)
        unique_key = f"{key}:{candidate_id.rsplit(':', 1)[-1][:8]}"
        row = service.create(self.center, {"holder_type": "candidate", "holder_id": candidate_id, "credential_type": credential_type, "issuer": "issuer:gov", "valid_from": self.app.clock.now(), "valid_to": valid_to}, request_key=unique_key)
        transitions = {"verified": [("verified", 1)], "effective": [("verified", 1), ("effective", 2)]}
        version = 1
        for target, expected in transitions[state]:
            row = service.transition(self.center, row["entity_id"], target, expected_version=expected, reason=target, request_key=f"{unique_key}:{target}")
            version += 1
        return row["entity_id"]

    def full_credentials(self, candidate_id, *, permit_valid_to="2027-12-31T00:00:00Z"):
        return [
            self.make_credential(candidate_id, "skill:python", key="cred-py"),
            self.make_credential(candidate_id, "skill:ml", key="cred-ml"),
            self.make_credential(candidate_id, "lang:en/c1", key="cred-en"),
            self.make_credential(candidate_id, "work-permit-z", valid_to=permit_valid_to, key="cred-permit"),
        ]

    def recommend(self, job, candidate, credential_ids, *, receipt_key=None, key="ref"):
        return self.svc.recommend(self.recruiter, {"job_id": job["entity_id"], "candidate_id": candidate["entity_id"], "credential_ids": credential_ids, "receipt_key": receipt_key}, request_key=key)

    # ------------------------------------------------------------------ #
    # 1. 推荐与面试冻结岗位版本
    # ------------------------------------------------------------------ #

    def test_referral_freezes_job_version_and_keeps_it_after_revision(self):
        job = self.make_job()
        candidate = self.make_candidate()
        self.grant_consent(candidate["entity_id"])
        credentials = self.full_credentials(candidate["entity_id"])
        referral = self.recommend(job, candidate, credentials)

        self.assertEqual(referral["job_version"], 2)  # draft v1 -> open v2
        self.assertEqual(referral["eligibility"]["eligible"], True)
        self.assertEqual(referral["eligibility"]["missing"], [])
        frozen_skills = list(referral["job_snapshot"]["skills"])

        # 企业随后改版岗位（提高语言要求、增加技能），不影响已冻结的推荐
        self.svc.revise_job(self.recruiter, job["entity_id"], {"skills": ["python", "ml", "rust"], "languages": [{"code": "en", "min_level": "c1"}]}, expected_version=2, request_key="job-v3")
        interview = self.svc.schedule_interview(self.recruiter, {"referral_id": referral["entity_id"], "round": 1, "scheduled_at": "2026-10-05T10:00:00+08:00", "panel": ["person:recruiter-a"]}, request_key="int-1")

        self.assertEqual(interview["job_version"], 3)  # 面试锚定安排时的最新版……
        # ……而推荐冻结的仍是第 2 版的要求
        self.assertEqual(referral["requirements_digest"], self.svc.explain_referral(self.center, referral["entity_id"])["why"]["requirements_digest"])
        self.assertEqual(frozen_skills, ["ml", "python"])
        explanation = self.svc.explain_referral(self.center, referral["entity_id"])
        self.assertEqual(explanation["why"]["job_version"], 2)
        self.assertEqual(explanation["why"]["frozen_requirements"]["skills"], ["ml", "python"])
        self.assertEqual(explanation["why"]["frozen_requirements"]["languages"], [{"code": "en", "min_level": "b2"}])

    # ------------------------------------------------------------------ #
    # 2. 资格判定、例外申请与“企业不能批准自己的资格例外”
    # ------------------------------------------------------------------ #

    def test_missing_qualification_blocks_interview_until_independent_exception(self):
        job = self.make_job()
        candidate = self.make_candidate()
        self.grant_consent(candidate["entity_id"])
        # 缺少 ml 技能，语言只到 b1（低于 b2）
        credentials = [
            self.make_credential(candidate["entity_id"], "skill:python", key="cred-py"),
            self.make_credential(candidate["entity_id"], "lang:en/b1", key="cred-en"),
            self.make_credential(candidate["entity_id"], "work-permit-z", key="cred-permit"),
        ]
        referral = self.recommend(job, candidate, credentials)
        self.assertFalse(referral["eligibility"]["eligible"])
        missing = {item["requirement"] for item in referral["eligibility"]["missing"]}
        self.assertIn("skill:ml", missing)
        self.assertIn("lang:en/b2", missing)

        with self.assertRaises(ConflictError):
            self.svc.schedule_interview(self.recruiter, {"referral_id": referral["entity_id"], "round": 1, "scheduled_at": "2026-10-05T10:00:00+08:00", "panel": ["person:recruiter-a"]}, request_key="int-blocked")

        exception = self.svc.request_exception(self.recruiter, referral["entity_id"], {"requirement": "skill:ml", "justification": "十年机器学习项目经验"}, request_key="exc")
        # 申请人不能自批
        with self.assertRaises(PermissionDenied):
            self.svc.decide_exception(self.recruiter, exception["entity_id"], approve=True, note="同意", expected_version=1, request_key="exc-self")
        # 同企业的另一名招聘人员也不能批
        with self.assertRaises(PermissionDenied):
            self.svc.decide_exception(self.recruiter_c, exception["entity_id"], approve=True, note="同意", expected_version=1, request_key="exc-org")
        # 人才服务中心（非用人企业）审批通过
        decided = self.svc.decide_exception(self.center, exception["entity_id"], approve=True, note="项目经验充分，准予例外", expected_version=1, request_key="exc-ok")
        self.assertEqual(decided["state"], "approved")
        self.assertEqual(decided["reviewer"], "talent-center")

        # 仍有未处理的语言缺口，面试继续被拦截
        with self.assertRaises(ConflictError):
            self.svc.schedule_interview(self.recruiter, {"referral_id": referral["entity_id"], "round": 1, "scheduled_at": "2026-10-05T10:00:00+08:00", "panel": ["person:recruiter-a"]}, request_key="int-still")

        lang_exc = self.svc.request_exception(self.recruiter, referral["entity_id"], {"requirement": "lang:en/b2", "justification": "工作语言为中文"}, request_key="exc-lang")
        self.svc.decide_exception(self.center, lang_exc["entity_id"], approve=True, note="可", expected_version=1, request_key="exc-lang-ok")
        interview = self.svc.schedule_interview(self.recruiter, {"referral_id": referral["entity_id"], "round": 1, "scheduled_at": "2026-10-05T10:00:00+08:00", "panel": ["person:recruiter-a"]}, request_key="int-ok")
        self.assertEqual(interview["state"], "scheduled")

        explanation = self.svc.explain_referral(self.center, referral["entity_id"])
        by_requirement = {item["requirement"]: item for item in explanation["why"]["exceptions"]}
        self.assertEqual(by_requirement["skill:ml"]["reviewer"], "talent-center")
        self.assertEqual(by_requirement["skill:ml"]["state"], "approved")
        self.assertEqual(by_requirement["lang:en/b2"]["state"], "approved")

    def test_exception_only_for_actually_missing_items(self):
        job = self.make_job()
        candidate = self.make_candidate()
        self.grant_consent(candidate["entity_id"])
        referral = self.recommend(job, candidate, self.full_credentials(candidate["entity_id"]))
        with self.assertRaises(ValidationError):
            self.svc.request_exception(self.recruiter, referral["entity_id"], {"requirement": "skill:python", "justification": "无缺口不能申请"}, request_key="exc-bad")

    # ------------------------------------------------------------------ #
    # 3. 联系方式只向获授权人员开放
    # ------------------------------------------------------------------ #

    def test_contact_visible_only_to_authorized_audience(self):
        candidate = self.make_candidate()
        self.grant_consent(candidate["entity_id"])
        # 获授权招聘人员可见
        self.assertEqual(self.svc.get_candidate(self.recruiter, candidate["entity_id"])["contact"], "liming@example.com")
        # 未授权人员只能看到掩码
        outsider = AccessContext(actor_id="person:recruiter-b", permissions=perms("read:candidates"))
        self.assertEqual(self.svc.get_candidate(outsider, candidate["entity_id"])["contact"], "***")
        # 人才服务中心运营视角可见
        self.assertEqual(self.svc.get_candidate(self.center, candidate["entity_id"])["contact"], "liming@example.com")

    def test_contact_hidden_after_consent_withdrawn(self):
        candidate = self.make_candidate()
        consent = self.grant_consent(candidate["entity_id"])
        self.svc.withdraw_consent(self.recruiter, consent["entity_id"], reason="候选人撤回", request_key="consent-off")
        self.assertEqual(self.svc.get_candidate(self.recruiter, candidate["entity_id"])["contact"], "***")

    def test_recommend_requires_active_consent(self):
        job = self.make_job()
        candidate = self.make_candidate()
        credentials = self.full_credentials(candidate["entity_id"])
        with self.assertRaises(ConflictError):
            self.recommend(job, candidate, credentials)
        # 授权已过期同样不能推荐
        self.grant_consent(candidate["entity_id"], valid_until="2026-09-01T00:00:00Z", key="consent-old")
        with self.assertRaises(ConflictError):
            self.recommend(job, candidate, credentials, key="ref-late")

    # ------------------------------------------------------------------ #
    # 4. 简历回执：重复保留进度；同标识不同材料人工核验
    # ------------------------------------------------------------------ #

    def test_duplicate_resume_keeps_progress(self):
        candidate = self.make_candidate()
        materials = {"resume": "v1", "skills": ["python"]}
        first = self.svc.receive_resume(self.recruiter, receipt_key="rcpt-1", candidate_id=candidate["entity_id"], materials=materials, request_key="recv-1")
        self.assertEqual(first["status"], "accepted")
        # 同标识同内容重复回执
        second = self.svc.receive_resume(self.recruiter, receipt_key="rcpt-1", candidate_id=candidate["entity_id"], materials=materials, request_key="recv-2")
        self.assertEqual(second["status"], "duplicate")
        self.assertTrue(second["progress_kept"])
        # 同内容换标识提交，仍识别为同一材料，不产生新进度
        third = self.svc.receive_resume(self.recruiter, receipt_key="rcpt-2", candidate_id=candidate["entity_id"], materials=materials, request_key="recv-3")
        self.assertEqual(third["status"], "duplicate")
        self.assertEqual(third["receipt_key"], "rcpt-1")

    def test_same_key_different_materials_goes_to_manual_review(self):
        candidate = self.make_candidate()
        self.svc.receive_resume(self.recruiter, receipt_key="rcpt-9", candidate_id=candidate["entity_id"], materials={"resume": "v1"}, request_key="recv-a")
        with self.assertRaises(ConflictError):
            self.svc.receive_resume(self.recruiter, receipt_key="rcpt-9", candidate_id=candidate["entity_id"], materials={"resume": "v2-different"}, request_key="recv-b")
        pending = self.svc.list_manual_reviews(self.center)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["receipt_key"], "rcpt-9")
        self.assertEqual(pending[0]["status"], "pending")

        # keep：维持原材料与原进度，冲突关闭
        result = self.svc.resolve_manual_review(self.center, pending[0]["review_id"], resolution="keep", note="核验为误传", request_key="rev-keep")
        self.assertEqual(result["resolution"], "keep")
        again = self.svc.receive_resume(self.recruiter, receipt_key="rcpt-9", candidate_id=candidate["entity_id"], materials={"resume": "v1"}, request_key="recv-c")
        self.assertEqual(again["status"], "duplicate")

    def test_manual_review_accept_new_replaces_materials_but_keeps_history(self):
        candidate = self.make_candidate()
        self.svc.receive_resume(self.recruiter, receipt_key="rcpt-8", candidate_id=candidate["entity_id"], materials={"resume": "v1"}, request_key="recv-a")
        with self.assertRaises(ConflictError):
            self.svc.receive_resume(self.recruiter, receipt_key="rcpt-8", candidate_id=candidate["entity_id"], materials={"resume": "v2"}, request_key="recv-b")
        review = self.svc.list_manual_reviews(self.center)[0]
        self.svc.resolve_manual_review(self.center, review["review_id"], resolution="accept_new", note="确认候选人更新", request_key="rev-new")
        # 新材料现在被视为原标识的最新材料，重复提交保留进度
        again = self.svc.receive_resume(self.recruiter, receipt_key="rcpt-8", candidate_id=candidate["entity_id"], materials={"resume": "v2"}, request_key="recv-d")
        self.assertEqual(again["status"], "duplicate")
        # 旧材料再次出现又会进入核验，历史不丢
        with self.assertRaises(ConflictError):
            self.svc.receive_resume(self.recruiter, receipt_key="rcpt-8", candidate_id=candidate["entity_id"], materials={"resume": "v1"}, request_key="recv-e")

    # ------------------------------------------------------------------ #
    # 5. 撤回授权 / 岗位关闭 / 预算变化只终止未完成环节，承诺保留
    # ------------------------------------------------------------------ #

    def _offer_pipeline(self, *, consent_valid="2027-06-30T00:00:00Z", permit_valid_to="2027-12-31T00:00:00Z", offer_until="2026-10-20T00:00:00+08:00"):
        job = self.make_job()
        candidate = self.make_candidate()
        consent = self.grant_consent(candidate["entity_id"], valid_until=consent_valid)
        credentials = self.full_credentials(candidate["entity_id"], permit_valid_to=permit_valid_to)
        resume = self.svc.receive_resume(self.recruiter, receipt_key="rcpt-x", candidate_id=candidate["entity_id"], materials={"v": 1}, request_key="recv")
        referral = self.recommend(job, candidate, credentials, receipt_key=resume["receipt_key"])
        interview = self.svc.schedule_interview(self.recruiter, {"referral_id": referral["entity_id"], "round": 1, "scheduled_at": "2026-10-05T10:00:00+08:00", "panel": ["person:recruiter-a"]}, request_key="int-1")
        self.svc.complete_interview(self.recruiter, interview["entity_id"], result="通过", request_key="int-done")
        offer = self.svc.issue_offer(self.recruiter, {"referral_id": referral["entity_id"], "terms": {"compensation": 650000, "currency": "CNY"}, "valid_until": offer_until}, request_key="offer")
        return job, candidate, consent, referral, interview, offer

    def test_consent_withdrawal_cancels_pending_steps_but_keeps_offer(self):
        job, candidate, consent, referral, interview, offer = self._offer_pipeline()
        # 承诺后又安排了第二轮面试（尚未完成）
        second = self.svc.schedule_interview(self.recruiter, {"referral_id": referral["entity_id"], "round": 2, "scheduled_at": "2026-10-12T10:00:00+08:00", "panel": ["person:recruiter-a"]}, request_key="int-2")
        self.svc.withdraw_consent(self.recruiter, consent["entity_id"], reason="候选人改变计划", request_key="wd-consent")

        self.assertEqual(self.app.repository.get("interviews", second["entity_id"])["state"], "cancelled")
        self.assertEqual(self.app.repository.get("interviews", second["entity_id"])["cancel_reason"], "consent_withdrawn")
        # 已完成的第一轮不动
        self.assertEqual(self.app.repository.get("interviews", interview["entity_id"])["state"], "completed")
        # 已发出的录用承诺原样保留，不会被悄悄覆盖
        offer_row = self.app.repository.get("offers", offer["entity_id"])
        self.assertEqual(offer_row["state"], "offered")
        self.assertEqual(offer_row["terms"]["compensation"], 650000)
        # 推荐记录已进入承诺阶段，不被终止
        self.assertEqual(self.app.repository.get("referrals", referral["entity_id"])["state"], "offered")

    def test_job_close_cancels_unfinished_referrals_only(self):
        job, candidate, consent, referral, interview, offer = self._offer_pipeline()
        # 同一岗位另一名候选人尚在推荐阶段
        other = self.make_candidate(key="cand-2", contact="wang@example.com")
        self.grant_consent(other["entity_id"], key="consent-2")
        other_ref = self.recommend(job, other, self.full_credentials(other["entity_id"]), key="ref-2")
        pending_interview = self.svc.schedule_interview(self.recruiter, {"referral_id": other_ref["entity_id"], "round": 1, "scheduled_at": "2026-10-06T10:00:00+08:00", "panel": ["person:recruiter-a"]}, request_key="int-other")

        self.svc.transition_job(self.recruiter, job["entity_id"], "closed", expected_version=2, reason="招聘结束", request_key="job-close")

        self.assertEqual(self.app.repository.get("referrals", other_ref["entity_id"])["state"], "cancelled")
        self.assertEqual(self.app.repository.get("referrals", other_ref["entity_id"])["cancel_reason"], "job_closed")
        self.assertEqual(self.app.repository.get("interviews", pending_interview["entity_id"])["state"], "cancelled")
        # 已有承诺的候选人不受影响
        self.assertEqual(self.app.repository.get("offers", offer["entity_id"])["state"], "offered")
        self.assertEqual(self.app.repository.get("referrals", referral["entity_id"])["state"], "offered")

    def test_budget_change_cancels_unfinished_but_not_offer(self):
        job, candidate, consent, referral, interview, offer = self._offer_pipeline()
        other = self.make_candidate(key="cand-2", contact="wang@example.com")
        self.grant_consent(other["entity_id"], key="consent-2")
        other_ref = self.recommend(job, other, self.full_credentials(other["entity_id"]), key="ref-2")
        self.svc.revise_job(self.recruiter, job["entity_id"], {"budget": {"amount": 300000, "currency": "CNY", "openings": 1}}, expected_version=2, request_key="budget-cut")
        self.assertEqual(self.app.repository.get("referrals", other_ref["entity_id"])["state"], "cancelled")
        self.assertEqual(self.app.repository.get("referrals", other_ref["entity_id"])["cancel_reason"], "budget_changed")
        self.assertEqual(self.app.repository.get("offers", offer["entity_id"])["state"], "offered")

    # ------------------------------------------------------------------ #
    # 6. 录用承诺不可覆盖，只能撤回或补充协议
    # ------------------------------------------------------------------ #

    def test_offer_cannot_be_overwritten_only_withdrawn_or_amended(self):
        job, candidate, consent, referral, interview, offer = self._offer_pipeline()
        # 不能再发第二份承诺覆盖第一份
        with self.assertRaises(ConflictError):
            self.svc.issue_offer(self.recruiter, {"referral_id": referral["entity_id"], "terms": {"compensation": 500000, "currency": "CNY"}, "valid_until": "2026-10-25T00:00:00+08:00"}, request_key="offer-2")

        # 薪酬高于冻结区间上限不能直接承诺
        # （先撤回再演示补充协议衔接）
        withdrawn = self.svc.withdraw_offer(self.recruiter, offer["entity_id"], reason="编制调整", request_key="offer-wd")
        self.assertEqual(withdrawn["state"], "withdrawn")
        self.assertEqual(withdrawn["withdrawal"]["reason"], "编制调整")
        self.assertEqual(len(withdrawn["withdrawal"]["prior_terms_digest"]), 64)
        # 撤回必须留原因
        with self.assertRaises(ValidationError):
            self.svc.withdraw_offer(self.recruiter, offer["entity_id"], reason="  ", request_key="offer-wd-again")

    def test_amendment_preserves_original_terms_and_requires_candidate_acceptance(self):
        job, candidate, consent, referral, interview, offer = self._offer_pipeline()
        amendment = self.svc.propose_amendment(self.recruiter, offer["entity_id"], {"changes": {"signing_bonus": 50000}, "reason": "挽留谈判", "valid_until": "2026-10-18T00:00:00+08:00"}, request_key="amd-1")
        # 企业不能替候选人接受补充协议
        with self.assertRaises(PermissionDenied):
            self.svc.decide_amendment(self.recruiter, amendment["entity_id"], accept=True, request_key="amd-self")
        # 候选人接受
        accepted = self.svc.decide_amendment(self.candidate_actor, amendment["entity_id"], accept=True, request_key="amd-ok")
        self.assertEqual(accepted["state"], "accepted")
        offer_row = self.svc.get_offer(self.center, offer["entity_id"])
        self.assertEqual(offer_row["accepted_amendment_ids"], [amendment["entity_id"]])
        # 原条款仍在，补充协议作为衔接记录可追溯
        self.assertEqual(offer_row["terms"]["compensation"], 650000)
        explanation = self.svc.explain_referral(self.center, referral["entity_id"])
        self.assertEqual(explanation["offer"]["amendments"][0]["changes"], {"signing_bonus": 50000})
        self.assertEqual(explanation["offer"]["amendments"][0]["decided_by"], "person:candidate-li")

    def test_candidate_confirms_offer_completes_hire(self):
        job, candidate, consent, referral, interview, offer = self._offer_pipeline()
        confirmed = self.svc.confirm_offer(self.candidate_actor, offer["entity_id"], request_key="offer-confirm")
        self.assertEqual(confirmed["state"], "confirmed")
        self.assertEqual(self.app.repository.get("referrals", referral["entity_id"])["state"], "hired")

    # ------------------------------------------------------------------ #
    # 7. 服务重启后待办不丢失：证件到期、面试提醒、承诺期限
    # ------------------------------------------------------------------ #

    def test_pending_jobs_survive_restart_and_expire_offer(self):
        job, candidate, consent, referral, interview, offer = self._offer_pipeline(offer_until="2026-10-05T00:00:00+08:00")
        # 另有一轮尚未举行、时间早于重启时刻的面试（用于验证协调提醒）
        pending = self.svc.schedule_interview(self.recruiter, {"referral_id": referral["entity_id"], "round": 2, "scheduled_at": "2026-10-03T10:00:00+08:00", "panel": ["person:recruiter-a"]}, request_key="int-reminder")
        with self.app.database.connect() as connection:
            waiting = connection.execute("SELECT COUNT(*) AS n FROM scheduled_jobs WHERE status='waiting'").fetchone()["n"]
        self.assertGreaterEqual(waiting, 7)  # 4 证件到期 + 2 面试提醒 + 1 承诺期限

        # 模拟服务重启，时间推进到期限之后
        later = CivicFlow.open(self.db_path, fixed_now="2026-10-10T09:00:00+08:00")
        svc = RecruitingService(later.repository, later.jobs, later.outbox)
        results = svc.run_due_jobs(self.center, limit=50)
        actions = {(item["job_type"], item["action"]) for item in results}
        self.assertIn(("recruiting.offer_deadline", "expired"), actions)
        self.assertIn(("recruiting.interview_reminder", "reminded"), actions)
        self.assertEqual(later.repository.get("interviews", pending["entity_id"])["state"], "scheduled")
        # 承诺到期后状态为 expired，而不是被静默修改条款
        self.assertEqual(later.repository.get("offers", offer["entity_id"])["state"], "expired")
        # 再跑一次没有重复处理
        self.assertEqual(svc.run_due_jobs(self.center, limit=50), [])

    def test_credential_expiry_cancels_unfinished_referral_after_restart(self):
        job = self.make_job()
        candidate = self.make_candidate(key="cand-exp")
        self.grant_consent(candidate["entity_id"], key="consent-exp")
        credentials = self.full_credentials(candidate["entity_id"], permit_valid_to="2026-10-04T00:00:00+08:00")
        referral = self.recommend(job, candidate, credentials, key="ref-exp")
        self.svc.schedule_interview(self.recruiter, {"referral_id": referral["entity_id"], "round": 1, "scheduled_at": "2026-10-08T10:00:00+08:00", "panel": ["person:recruiter-a"]}, request_key="int-exp")

        later = CivicFlow.open(self.db_path, fixed_now="2026-10-06T09:00:00+08:00")
        svc = RecruitingService(later.repository, later.jobs, later.outbox)
        results = svc.run_due_jobs(self.center, limit=50)
        self.assertTrue(any(item["action"] == "cancelled" and item.get("referral_id") == referral["entity_id"] for item in results))
        self.assertEqual(later.repository.get("referrals", referral["entity_id"])["state"], "cancelled")
        self.assertEqual(later.repository.get("referrals", referral["entity_id"])["cancel_reason"], "credential_expired")

    def test_leased_jobs_are_reclaimed_after_worker_crash(self):
        job, candidate, consent, referral, interview, offer = self._offer_pipeline(offer_until="2026-10-05T00:00:00+08:00")
        # worker 在 10-06 领取了全部到期任务，但进程崩溃未 finish
        crashed = CivicFlow.open(self.db_path, fixed_now="2026-10-06T09:00:00+08:00")
        claimed = crashed.jobs.claim_due(seconds=30, limit=50)
        self.assertTrue(claimed)
        self.assertEqual(crashed.jobs.claim_due(seconds=30, limit=50), [])  # 租约内不会重复领取
        # 租约过期后服务重启，任务被重新领取并处理
        later = CivicFlow.open(self.db_path, fixed_now="2026-10-10T09:00:00+08:00")
        svc = RecruitingService(later.repository, later.jobs, later.outbox)
        results = svc.run_due_jobs(self.center, limit=50)
        self.assertTrue(any((item["job_type"], item["action"]) == ("recruiting.offer_deadline", "expired") for item in results))

    # ------------------------------------------------------------------ #
    # 8. 可追溯解释：为何推荐、哪版要求、谁的例外、下一步谁处理
    # ------------------------------------------------------------------ #

    def test_explain_referral_answers_why_who_what_next(self):
        job = self.make_job()
        candidate = self.make_candidate()
        self.grant_consent(candidate["entity_id"])
        referral = self.recommend(job, candidate, self.full_credentials(candidate["entity_id"]))
        interview = self.svc.schedule_interview(self.recruiter, {"referral_id": referral["entity_id"], "round": 1, "scheduled_at": "2026-10-05T10:00:00+08:00", "panel": ["person:recruiter-a"]}, request_key="int-ex")
        self.svc.complete_interview(self.recruiter, interview["entity_id"], result="通过", request_key="int-ex-done")
        self.svc.issue_offer(self.recruiter, {"referral_id": referral["entity_id"], "terms": {"compensation": 620000, "currency": "CNY"}, "valid_until": "2026-10-20T00:00:00+08:00"}, request_key="offer-ex")

        explanation = self.svc.explain_referral(self.center, referral["entity_id"])
        self.assertEqual(explanation["recommended_by"], "person:recruiter-a")
        self.assertEqual(explanation["why"]["job_id"], job["entity_id"])
        self.assertEqual(explanation["why"]["job_version"], 2)
        self.assertIsNotNone(explanation["why"]["job_version_published_at"])
        self.assertTrue(explanation["why"]["eligibility"]["eligible"])
        self.assertEqual(explanation["credential_freeze"][0]["credential_id"].startswith("credentials:"), True)
        self.assertEqual(explanation["consent_freeze"]["consent_id"].startswith("candidate_consents:"), True)
        self.assertEqual(len(explanation["interviews"]), 1)
        self.assertEqual(explanation["interviews"][0]["round"], 1)
        self.assertEqual(explanation["offer"]["committed_by"], "person:recruiter-a")
        self.assertEqual(explanation["next_owner"], candidate["entity_id"])  # 等候选人确认
        self.assertIn({"version": 1, "state": "recommended"}, [{"version": e["version"], "state": e["state"]} for e in explanation["timeline"]])

    # ------------------------------------------------------------------ #
    # 9. 岗位维护规则
    # ------------------------------------------------------------------ #

    def test_closed_job_cannot_be_revised_and_employer_org_immutable(self):
        job = self.make_job()
        self.svc.transition_job(self.recruiter, job["entity_id"], "closed", expected_version=2, reason="结束", request_key="close")
        with self.assertRaises(ConflictError):
            self.svc.revise_job(self.recruiter, job["entity_id"], {"title": "新名称"}, expected_version=3, request_key="rev-closed")
        reopened_job = self.make_job(key="job-2")
        with self.assertRaises(ValidationError):
            self.svc.revise_job(self.recruiter, reopened_job["entity_id"], {"employer_org": "organizations:other", "title": "x"}, expected_version=2, request_key="rev-org")

    def test_enterprise_cannot_maintain_another_orgs_job(self):
        orgs = OrganizationService(self.app.repository)
        other_org = orgs.create(self.center, {"name": "竞品科技", "kind": "enterprise", "jurisdiction": "CN", "owner_id": "person:other-hr"}, request_key="org-other")
        with self.assertRaises(PermissionDenied):
            self.svc.create_job(self.recruiter, {
                "employer_org": other_org["entity_id"], "title": "渗透岗位",
                "skills": ["python"], "languages": [{"code": "en", "min_level": "b1"}],
                "work_permit": {"required_types": ["work-permit-z"]},
                "salary_band": {"amount": 100, "currency": "CNY"},
                "budget": {"amount": 100, "currency": "CNY", "openings": 1},
            }, request_key="cross-org-job")

    def test_invalid_language_level_rejected(self):
        with self.assertRaises(ValidationError):
            self.svc.create_job(self.recruiter, {
                "employer_org": self.org["entity_id"], "title": "x",
                "skills": ["python"], "languages": [{"code": "en", "min_level": "native"}],
                "work_permit": {"required_types": ["work-permit-z"]},
                "salary_band": {"amount": 100, "currency": "CNY"},
                "budget": {"amount": 100, "currency": "CNY", "openings": 1},
            }, request_key="bad-job")


if __name__ == "__main__":
    unittest.main()
