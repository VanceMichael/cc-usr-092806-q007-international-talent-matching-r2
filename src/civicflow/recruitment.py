"""可追溯招聘流程服务。

把企业岗位、技能与语言要求、签证/工作许可、候选人授权、面试轮次和承诺期限
串成一条带版本冻结、例外审批和恢复待办的招聘流程。所有业务实体复用平台的
版本化实体仓库（entity_versions + 哈希审计链 + 幂等键），简历回执与待办
使用独立的持久表。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable, Mapping

from .audit import AuditLog
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .identifiers import new_id, require_safe
from .jsonutil import canonical_json, digest_json
from .repository import EntityRepository
from .security import AccessContext
from .timeutil import Clock, canonical_instant, parse_instant

JOB_TYPE = "rec_job"
APPLICATION_TYPE = "rec_application"
INTERVIEW_TYPE = "rec_interview"
OFFER_TYPE = "rec_offer"
EXCEPTION_TYPE = "rec_exception"

JOB_STATES = ("draft", "open", "closed")
APPLICATION_STATES = (
    "applied", "referred", "interviewing", "offered", "accepted",
    "consent_withdrawn", "closed", "blocked_manual",
)
INTERVIEW_STATES = ("scheduled", "completed", "cancelled")
OFFER_STATES = ("issued", "confirmed", "superseded", "rescinded", "expired")
EXCEPTION_STATES = ("pending", "approved", "rejected")

CONTACT_FIELDS = ("contact",)
# 截断事件（撤回授权、证件到期、岗位关闭、预算变化）作用于这些进行中的申请。
CUTOFF_STATES = ("referred", "interviewing", "offered", "accepted")
WITHDRAWABLE_STATES = ("applied",) + CUTOFF_STATES

TODO_KINDS = ("credential_expiry", "interview_coordination", "offer_confirmation")
# 撤回授权、证件到期、岗位关闭、预算变化等截断事件只取消这些尚未完成的环节。
CUTOFF_CANCELLABLE_TODOS = ("credential_expiry", "interview_coordination")


@dataclass(frozen=True)
class RecruitmentService:
    """招聘流程领域服务。"""

    repository: EntityRepository
    jobs: object
    database: object
    clock: Clock

    # ------------------------------------------------------------------ 岗位

    def create_job(self, context: AccessContext, values: Mapping[str, object], *, request_key: str) -> dict:
        context.require("write:rec_job")
        payload = self._validate_job(values, partial=False)
        payload["state"] = "draft"
        return self.repository.create(JOB_TYPE, payload, actor=context.actor_id, request_key=request_key)

    def revise_job(self, context: AccessContext, job_id: str, values: Mapping[str, object], *, expected_version: int, request_key: str) -> dict:
        """企业维护本方岗位。开放中的修改产生新版本，已冻结的推荐/面试/承诺不受影响。"""
        context.require("write:rec_job")
        current = self._job(job_id)
        if current["state"] == "closed":
            raise ConflictError("岗位已关闭，不能再修改")
        payload = self._validate_job(values, partial=True)
        if "state" in payload:
            raise ValidationError("状态必须通过专门的发布/关闭操作修改")
        return self.repository.update(JOB_TYPE, job_id, payload, actor=context.actor_id, expected_version=expected_version, request_key=request_key)

    def change_budget(self, context: AccessContext, job_id: str, budget: Mapping[str, object], *, expected_version: int, request_key: str) -> dict:
        """预算变化形成新版本，只截断尚未完成的环节；已发出的录用承诺不受影响。"""
        context.require("write:rec_job")
        current = self._job(job_id)
        if current["state"] == "closed":
            raise ConflictError("岗位已关闭，不能再调整预算")
        budget = self._validate_budget(budget)
        updated = self.repository.update(JOB_TYPE, job_id, {"budget": budget}, actor=context.actor_id, expected_version=expected_version, request_key=request_key)
        self._cascade_cutoff(job_id, reason="budget_changed", actor=context.actor_id,
                             keep_offer_confirmation=True)
        return updated

    def publish_job(self, context: AccessContext, job_id: str, *, expected_version: int, request_key: str) -> dict:
        context.require("write:rec_job")
        current = self._job(job_id)
        if current["state"] != "draft":
            raise ConflictError(f"岗位处于 {current['state']}，不能发布")
        return self.repository.update(JOB_TYPE, job_id, {"state": "open", "published_at": self.clock.now()}, actor=context.actor_id, expected_version=expected_version, request_key=request_key)

    def close_job(self, context: AccessContext, job_id: str, *, expected_version: int, reason: str, request_key: str) -> dict:
        """岗位关闭：终止尚未完成的环节，已发出的录用承诺继续有效。"""
        context.require("write:rec_job")
        current = self._job(job_id)
        if current["state"] == "closed":
            raise ConflictError("岗位已经关闭")
        closed = self.repository.update(JOB_TYPE, job_id, {"state": "closed", "closed_at": self.clock.now(), "close_reason": _reason(reason)}, actor=context.actor_id, expected_version=expected_version, request_key=request_key)
        self._cascade_cutoff(job_id, reason="job_closed", actor=context.actor_id, keep_offer_confirmation=True)
        return closed

    def get_job(self, context: AccessContext, job_id: str) -> dict:
        context.require("read:rec_job")
        return self._job(job_id)

    def job_version(self, context: AccessContext, job_id: str, version: int) -> dict:
        context.require("history:rec_job")
        return self._exact_version(JOB_TYPE, job_id, version)

    # ------------------------------------------------------------ 简历与授权

    def receive_resume(self, context: AccessContext, *, person_key: str, resume: Mapping[str, object], request_key: str) -> dict:
        """接收简历回执。

        同一份简历（相同摘要）重复到达：保留原进度，返回 duplicate。
        同一候选人标识但简历/资格材料摘要不同：进入人工核验，不覆盖原申请。
        """
        context.require("write:rec_application")
        person_key = require_safe(person_key, "候选人标识")
        if not isinstance(resume, Mapping):
            raise ValidationError("简历内容必须是对象")
        job_id = resume.get("job_id")
        if not isinstance(job_id, str) or not job_id.strip():
            raise ValidationError("简历必须指明应聘岗位 job_id")
        job_id = job_id.strip()
        contact = resume.get("contact")
        if not isinstance(contact, str) or not contact.strip():
            raise ValidationError("缺少候选人联系方式")
        digest = digest_json(resume)
        now = self.clock.now()
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM recruitment_receipts WHERE person_key=? AND job_id=? ORDER BY created_at LIMIT 1",
                (person_key, job_id),
            ).fetchone()
            if existing:
                if existing["digest_value"] == digest:
                    connection.execute("UPDATE recruitment_receipts SET updated_at=? WHERE receipt_id=?", (now, existing["receipt_id"]))
                    return {"status": "duplicate", "application_id": existing["application_id"], "digest": digest}
                receipt_id = new_id("receipt")
                connection.execute(
                    "INSERT INTO recruitment_receipts(receipt_id,person_key,job_id,digest_value,application_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (receipt_id, person_key, job_id, digest, existing["application_id"], "manual_review", now, now),
                )
                self._audit(connection, actor=context.actor_id, action="resume-manual-review",
                            entity_type=APPLICATION_TYPE, entity_id=existing["application_id"], version=0,
                            detail={"person_key": person_key, "existing_digest": existing["digest_value"], "incoming_digest": digest})
                app_row = connection.execute(
                    "SELECT version,state FROM entities WHERE entity_type=? AND entity_id=?",
                    (APPLICATION_TYPE, existing["application_id"]),
                ).fetchone()
                if app_row and app_row["state"] == "applied":
                    self._update_entity_in_txn(
                        connection, APPLICATION_TYPE, existing["application_id"],
                        {"state": "blocked_manual", "next_action": "manual_verification", "next_owner": "center_operator"},
                        expected_version=app_row["version"], actor=context.actor_id,
                        request_key=f"manual:{request_key}",
                    )
                return {"status": "manual_review", "application_id": existing["application_id"], "digest": digest}

            receipt_id = new_id("receipt")
            application_id = new_id(APPLICATION_TYPE)
            profile = dict(resume)
            profile.pop("job_id", None)
            payload = {
                "state": "applied",
                "job_id": job_id,
                "person_key": person_key,
                "applicant_name": str(profile.pop("name", person_key)),
                "contact": contact.strip(),
                "skills": _validate_requirement_list(profile.pop("skills", []), "name", "技能"),
                "languages": _validate_requirement_list(profile.pop("languages", []), "language", "语言"),
                "credential_ids": [_require_actor(c, "证件标识") for c in profile.pop("credential_ids", [])],
                "resume_digest": digest,
                "consent": {"state": "not_granted"},
                "next_owner": "center_operator",
                "next_action": "await_consent_and_referral",
                "planned_rounds": int(profile.pop("planned_rounds", 1) or 1),
            }
            if payload["planned_rounds"] < 1:
                raise ValidationError("面试轮次数必须大于 0")
            connection.execute(
                "INSERT INTO recruitment_receipts(receipt_id,person_key,job_id,digest_value,application_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (receipt_id, person_key, job_id, digest, application_id, "accepted", now, now),
            )
            self._insert_entity(connection, APPLICATION_TYPE, application_id, payload, actor=context.actor_id, request_key=request_key)
            return {"status": "accepted", "application_id": application_id, "digest": digest}

    def grant_consent(self, context: AccessContext, application_id: str, *, scope: str, valid_to: str, expected_version: int, request_key: str) -> dict:
        """候选人授权（联系方式开放与流程推进的前提）。"""
        context.require("write:rec_application")
        current = self._application(application_id)
        canonical_instant(valid_to)
        consent = {"state": "granted", "granted_at": self.clock.now(), "scope": scope, "valid_to": valid_to}
        changes: dict = {"consent": consent}
        # 仅在尚未产生任何环节（applied 阶段撤回）时，重新授权直接恢复申请。
        if current["state"] == "consent_withdrawn" and current.get("next_action") == "await_reconsent":
            changes.update({"state": "applied", "next_owner": "center_operator", "next_action": "await_consent_and_referral"})
        return self.repository.update(APPLICATION_TYPE, application_id, changes, actor=context.actor_id, expected_version=expected_version, request_key=request_key)

    def withdraw_consent(self, context: AccessContext, application_id: str, *, expected_version: int, reason: str, request_key: str) -> dict:
        """候选人撤回授权：只终止尚未完成的环节；既有录用承诺保持有效，须以撤回记录或补充协议衔接。"""
        context.require("write:rec_application")
        current = self._application(application_id)
        if current["state"] not in WITHDRAWABLE_STATES:
            raise ConflictError(f"申请处于 {current['state']}，无需撤回授权")
        consent = dict(current.get("consent", {}))
        consent.update({"state": "withdrawn", "withdrawn_at": self.clock.now(), "reason": _reason(reason)})
        if current["state"] == "applied":
            # 尚无进行中环节：只记录授权撤回，等待候选人重新授权。
            return self.repository.update(
                APPLICATION_TYPE, application_id,
                {"consent": consent, "state": "consent_withdrawn",
                 "next_owner": "center_operator", "next_action": "await_reconsent"},
                actor=context.actor_id, expected_version=expected_version, request_key=request_key)
        changes: dict = {"consent": consent, "next_owner": "employer", "next_action": "resolve_offer_by_rescission_or_amendment"}
        updated = self.repository.update(APPLICATION_TYPE, application_id, changes, actor=context.actor_id, expected_version=expected_version, request_key=request_key)
        self._cascade_cutoff(current["job_id"], reason="consent_withdrawn", actor=context.actor_id,
                             application_id=application_id, keep_offer_confirmation=False,
                             app_state="consent_withdrawn")
        return updated

    def resolve_manual(self, context: AccessContext, application_id: str, *, decision: str, expected_version: int, request_key: str) -> dict:
        """人工核验结论：proceed 回到 applied，reject 关闭。"""
        context.require("write:rec_application")
        current = self._application(application_id)
        if current["state"] != "blocked_manual":
            raise ConflictError("该申请不在人工核验状态")
        if decision == "proceed":
            changes = {"state": "applied", "next_action": "await_consent_and_referral", "next_owner": "center_operator"}
        elif decision == "reject":
            changes = {"state": "closed", "next_action": "manual_rejection", "next_owner": "center_operator"}
        else:
            raise ValidationError("decision 必须是 proceed 或 reject")
        return self.repository.update(APPLICATION_TYPE, application_id, changes, actor=context.actor_id, expected_version=expected_version, request_key=request_key)

    # ---------------------------------------------------------------- 推荐

    def request_exception(self, context: AccessContext, application_id: str, *, requirement_code: str, reason: str, request_key: str) -> dict:
        """企业招聘人员为不满足的某项要求申请资格例外。"""
        context.require("write:rec_exception")
        app = self._application(application_id)
        job = self._job(app["job_id"])
        payload = {
            "state": "pending",
            "application_id": application_id,
            "job_id": app["job_id"],
            "employer_org": job["employer_org"],
            "requirement_code": _require_code(requirement_code, "要求项"),
            "reason": _reason(reason),
            "requested_by": context.actor_id,
            "reviewer": None,
            "decision_detail": None,
        }
        return self.repository.create(EXCEPTION_TYPE, payload, actor=context.actor_id, request_key=request_key)

    def decide_exception(self, context: AccessContext, exception_id: str, *, decision: str, comment: str, expected_version: int, request_key: str) -> dict:
        """资格例外只能由企业之外的审批人批准。"""
        context.require("approve:rec_exception")
        if decision not in ("approved", "rejected"):
            raise ValidationError("decision 必须是 approved 或 rejected")
        current = self.repository.get(EXCEPTION_TYPE, exception_id)
        if current["state"] != "pending":
            raise ConflictError("该例外已有决定")
        if current["requested_by"] == context.actor_id:
            raise PermissionDenied("提交者不能批准自己申请的例外")
        self._assert_not_employer(context, current["employer_org"])
        changes = {"state": decision, "reviewer": context.actor_id, "decided_at": self.clock.now(),
                   "decision_detail": _reason(comment)}
        return self.repository.update(EXCEPTION_TYPE, exception_id, changes, actor=context.actor_id, expected_version=expected_version, request_key=request_key)

    def refer(self, context: AccessContext, application_id: str, *, expected_version: int, request_key: str) -> dict:
        """推荐：冻结当时使用的岗位版本、要求快照与证件版本，并记录匹配结论与例外。"""
        context.require("write:rec_application")
        app = self._application(application_id)
        if app["state"] != "applied":
            raise ConflictError(f"申请处于 {app['state']}，不能推荐")
        consent = app.get("consent", {})
        if consent.get("state") != "granted":
            raise ConflictError("候选人尚未授权")
        if parse_instant(consent["valid_to"]) <= parse_instant(self.clock.now()):
            raise ConflictError("候选人授权已过期")
        job = self._job(app["job_id"])
        if job["state"] != "open":
            raise ConflictError("岗位未开放，不能推荐")

        credential_refs = self._freeze_credentials(app.get("credential_ids", []), require_effective=True)
        match = self._match_requirements(job, app, credential_refs)
        missing = [item["code"] for item in match if not item["satisfied"]]
        exceptions = self._approved_exceptions(application_id)
        uncovered = [code for code in missing if code not in exceptions]
        if uncovered:
            raise ConflictError("以下要求未满足且无已批准例外: " + ", ".join(uncovered))

        frozen = {
            "job_version": job["version"],
            "requirements": {"skills": job["skills"], "languages": job["languages"], "visa": job["visa"]},
            "credential_refs": credential_refs,
            "frozen_at": self.clock.now(),
            "frozen_by": context.actor_id,
        }
        changes = {
            "state": "referred",
            "referred_at": self.clock.now(),
            "referred_by": context.actor_id,
            "frozen": frozen,
            "match_report": match,
            "exception_ids": [e["entity_id"] for e in self._exception_rows(application_id) if e["state"] == "approved"],
            "next_owner": "employer",
            "next_action": "coordinate_interview:1",
            "planned_rounds": int(app.get("planned_rounds", 1) or 1),
        }
        referred = self.repository.update(APPLICATION_TYPE, application_id, changes, actor=context.actor_id, expected_version=expected_version, request_key=request_key)
        self._ensure_todo(application_id, "interview_coordination", "r1", due_at=self.clock.now(), round_no=1)
        for ref in credential_refs:
            if ref.get("valid_to"):
                self._ensure_todo(application_id, "credential_expiry", ref["credential_id"], due_at=ref["valid_to"], credential_id=ref["credential_id"])
                self._ensure_job(application_id, "credential_expiry", ref["credential_id"], run_at=ref["valid_to"])
        return referred

    # ---------------------------------------------------------------- 面试

    def schedule_interview(self, context: AccessContext, application_id: str, *, round_no: int, scheduled_at: str, panel: Iterable[str], request_key: str) -> dict:
        """安排面试：再次冻结当时的岗位版本与候选人证件版本。"""
        context.require("write:rec_interview")
        app = self._application(application_id)
        if app["state"] not in ("referred", "interviewing"):
            raise ConflictError("当前状态不能安排面试")
        self._require_active_consent(app)
        round_no = int(round_no)
        if round_no < 1:
            raise ValidationError("面试轮次必须从 1 开始")
        canonical_instant(scheduled_at)
        panel = [_require_actor(p, "面试官") for p in panel]
        if not panel:
            raise ValidationError("至少指定一名面试官")

        job = self._job(app["job_id"])
        if job["state"] != "open":
            raise ConflictError("岗位已关闭，不能安排新的面试")
        existing_rounds = sorted(r["round_no"] for r in self.repository.list(INTERVIEW_TYPE, limit=500)
                                 if r["application_id"] == application_id)
        expected_round = (existing_rounds[-1] + 1) if existing_rounds else 1
        if app["state"] == "referred":
            expected_round = 1
        if round_no != expected_round:
            raise ConflictError(f"下一场必须是第 {expected_round} 轮面试")
        credential_refs = self._freeze_credentials(app.get("credential_ids", []), require_effective=True)
        payload = {
            "state": "scheduled",
            "application_id": application_id,
            "job_id": app["job_id"],
            "round_no": round_no,
            "scheduled_at": canonical_instant(scheduled_at),
            "panel": panel,
            "scheduled_by": context.actor_id,
            "frozen": {
                "job_version": job["version"],
                "requirements": {"skills": job["skills"], "languages": job["languages"], "visa": job["visa"]},
                "credential_refs": credential_refs,
                "frozen_at": self.clock.now(),
            },
        }
        interview = self.repository.create(INTERVIEW_TYPE, payload, actor=context.actor_id, request_key=request_key)
        self._complete_todo(application_id, "interview_coordination", round_no=round_no)
        return interview

    def complete_interview(self, context: AccessContext, interview_id: str, *, expected_version: int, result: str, request_key: str) -> dict:
        context.require("write:rec_interview")
        interview = self.repository.get(INTERVIEW_TYPE, interview_id)
        if interview["state"] != "scheduled":
            raise ConflictError("面试不在待举行状态")
        updated = self.repository.update(INTERVIEW_TYPE, interview_id, {"state": "completed", "result": _reason(result), "completed_at": self.clock.now()}, actor=context.actor_id, expected_version=expected_version, request_key=request_key)
        app = self._application(interview["application_id"])
        app_changes: dict = {}
        if app["state"] == "referred":
            app_changes["state"] = "interviewing"
        planned = int(app.get("planned_rounds", 1) or 1)
        if interview["round_no"] < planned:
            next_round = interview["round_no"] + 1
            app_changes.update({"next_owner": "employer", "next_action": f"coordinate_interview:{next_round}"})
            self._ensure_todo(app["entity_id"], "interview_coordination", f"r{next_round}", due_at=self.clock.now(), round_no=next_round)
        else:
            app_changes.update({"next_owner": "employer", "next_action": "issue_offer"})
        if app_changes:
            self.repository.update(APPLICATION_TYPE, app["entity_id"], app_changes, actor=context.actor_id, expected_version=app["version"], request_key=f"after:{request_key}")
        return updated

    # ---------------------------------------------------------------- 承诺

    def issue_offer(self, context: AccessContext, application_id: str, *, compensation: Mapping[str, object], promise_valid_until: str, request_key: str) -> dict:
        """发出录用承诺（薪酬+承诺期限），冻结岗位/证件版本并校验预算。"""
        context.require("write:rec_offer")
        app = self._application(application_id)
        if app["state"] not in ("interviewing", "referred"):
            raise ConflictError("当前状态不能发出录用承诺")
        self._require_active_consent(app)
        job = self._job(app["job_id"])
        compensation = self._validate_compensation(compensation)
        self._assert_within_budget(job, compensation, application_id)
        canonical_instant(promise_valid_until)
        credential_refs = self._freeze_credentials(app.get("credential_ids", []), require_effective=False)
        payload = {
            "state": "issued",
            "application_id": application_id,
            "job_id": app["job_id"],
            "job_version": job["version"],
            "credential_refs": credential_refs,
            "compensation": compensation,
            "promise_valid_until": canonical_instant(promise_valid_until),
            "issued_by": context.actor_id,
            "issued_at": self.clock.now(),
            "amendment_of": None,
        }
        offer = self.repository.create(OFFER_TYPE, payload, actor=context.actor_id, request_key=request_key)
        self.repository.update(APPLICATION_TYPE, application_id,
                               {"state": "offered", "offer_id": offer["entity_id"],
                                "next_owner": "candidate", "next_action": "confirm_offer"},
                               actor=context.actor_id, expected_version=app["version"], request_key=f"link:{request_key}")
        self._ensure_todo(application_id, "offer_confirmation", offer["entity_id"], due_at=promise_valid_until, offer_id=offer["entity_id"])
        self._ensure_job(application_id, "offer_confirmation", offer["entity_id"], run_at=promise_valid_until)
        return offer

    def confirm_offer(self, context: AccessContext, application_id: str, *, expected_version: int, request_key: str) -> dict:
        """候选人在承诺期限内确认。"""
        context.require("write:rec_application")
        app = self._application(application_id)
        if app["state"] != "offered" or not app.get("offer_id"):
            raise ConflictError("没有待确认的录用承诺")
        offer = self.repository.get(OFFER_TYPE, app["offer_id"])
        if offer["state"] != "issued":
            raise ConflictError(f"承诺处于 {offer['state']}，不能确认")
        if parse_instant(offer["promise_valid_until"]) < parse_instant(self.clock.now()):
            raise ConflictError("已超过承诺期限")
        confirmed = self.repository.update(OFFER_TYPE, offer["entity_id"], {"state": "confirmed", "confirmed_at": self.clock.now()}, actor=context.actor_id, expected_version=offer["version"], request_key=f"offer:{request_key}")
        self.repository.update(APPLICATION_TYPE, application_id, {"state": "accepted", "next_owner": "center_operator", "next_action": "visa_and_onboarding"}, actor=context.actor_id, expected_version=expected_version, request_key=request_key)
        self._complete_todo(application_id, "offer_confirmation", offer_id=offer["entity_id"])
        return confirmed

    def rescind_offer(self, context: AccessContext, application_id: str, *, reason: str, expected_version: int, request_key: str) -> dict:
        """既有录用承诺只能通过撤回记录衔接：必须给出原因，留下不可擦除的记录。"""
        context.require("write:rec_offer")
        app = self._application(application_id)
        if not app.get("offer_id"):
            raise ConflictError("没有可撤回的录用承诺")
        offer = self.repository.get(OFFER_TYPE, app["offer_id"])
        if offer["state"] not in ("issued", "confirmed"):
            raise ConflictError(f"承诺处于 {offer['state']}，不能撤回")
        record = {
            "kind": "offer_rescission",
            "offer_id": offer["entity_id"],
            "application_id": application_id,
            "reason": _reason(reason),
            "recorded_by": context.actor_id,
            "recorded_at": self.clock.now(),
        }
        with self.database.transaction() as connection:
            self._audit(connection, actor=context.actor_id, action="offer-rescission-record",
                        entity_type=OFFER_TYPE, entity_id=offer["entity_id"], version=offer["version"], detail=record)
            self._update_entity_in_txn(connection, OFFER_TYPE, offer["entity_id"],
                                       {"state": "rescinded", "rescission": record},
                                       expected_version=offer["version"], actor=context.actor_id,
                                       request_key=request_key)
            self._update_entity_in_txn(connection, APPLICATION_TYPE, application_id,
                                       {"state": "closed", "next_owner": "center_operator", "next_action": "offer_rescinded"},
                                       expected_version=expected_version, actor=context.actor_id,
                                       request_key=f"app:{request_key}")
        self._cancel_todos(application_id, kinds=("offer_confirmation", "credential_expiry", "interview_coordination"))
        stored = self.repository.get(OFFER_TYPE, offer["entity_id"])
        return stored

    def supplement_offer(self, context: AccessContext, application_id: str, *, compensation: Mapping[str, object], promise_valid_until: str, reason: str, request_key: str) -> dict:
        """补充协议：原承诺标记 superseded 并完整保留，新版本承接，承诺绝不被静默覆盖。"""
        context.require("write:rec_offer")
        app = self._application(application_id)
        if not app.get("offer_id"):
            raise ConflictError("没有可补充的录用承诺")
        original = self.repository.get(OFFER_TYPE, app["offer_id"])
        if original["state"] not in ("issued", "confirmed"):
            raise ConflictError(f"原承诺处于 {original['state']}，不能补充协议")
        # 撤回授权后的补充协议属于重新接洽，必须先重新取得候选人授权。
        self._require_active_consent(app)
        job = self._job(app["job_id"])
        compensation = self._validate_compensation(compensation)
        self._assert_within_budget(job, compensation, application_id)
        canonical_instant(promise_valid_until)
        credential_refs = self._freeze_credentials(app.get("credential_ids", []), require_effective=False)
        amendment = {
            "kind": "offer_amendment",
            "original_offer_id": original["entity_id"],
            "application_id": application_id,
            "reason": _reason(reason),
            "recorded_by": context.actor_id,
            "recorded_at": self.clock.now(),
        }
        self.repository.update(OFFER_TYPE, original["entity_id"], {"state": "superseded", "amendment": amendment}, actor=context.actor_id, expected_version=original["version"], request_key=f"super:{request_key}")
        payload = {
            "state": "issued",
            "application_id": application_id,
            "job_id": app["job_id"],
            "job_version": job["version"],
            "credential_refs": credential_refs,
            "compensation": compensation,
            "promise_valid_until": canonical_instant(promise_valid_until),
            "issued_by": context.actor_id,
            "issued_at": self.clock.now(),
            "amendment_of": original["entity_id"],
        }
        new_offer = self.repository.create(OFFER_TYPE, payload, actor=context.actor_id, request_key=request_key)
        self.repository.update(APPLICATION_TYPE, application_id,
                               {"state": "offered", "offer_id": new_offer["entity_id"],
                                "next_owner": "candidate", "next_action": "confirm_offer"},
                               actor=context.actor_id, expected_version=app["version"], request_key=f"link:{request_key}")
        self._cancel_todos(application_id, kinds=("offer_confirmation",))
        self._ensure_todo(application_id, "offer_confirmation", new_offer["entity_id"], due_at=promise_valid_until, offer_id=new_offer["entity_id"])
        self._ensure_job(application_id, "offer_confirmation", new_offer["entity_id"], run_at=promise_valid_until)
        return new_offer

    def expire_offers(self, context: AccessContext) -> list[dict]:
        """把超过承诺期限仍未确认的承诺置为 expired（重启后可重复执行的兜底扫描）。"""
        context.require("write:rec_offer")
        now = self.clock.now()
        result = []
        for offer in self.repository.list(OFFER_TYPE, state="issued", limit=500):
            if parse_instant(offer["promise_valid_until"]) <= parse_instant(now):
                expired = self.repository.update(OFFER_TYPE, offer["entity_id"], {"state": "expired", "expired_at": now}, actor=context.actor_id, expected_version=offer["version"], request_key=f"expire:{offer['entity_id']}")
                self._cancel_todos(offer["application_id"], kinds=("offer_confirmation",))
                app = self._application(offer["application_id"])
                if app["state"] == "offered":
                    self.repository.update(APPLICATION_TYPE, app["entity_id"],
                                           {"next_owner": "employer", "next_action": "offer_expired_renew_or_close"},
                                           actor=context.actor_id, expected_version=app["version"],
                                           request_key=f"expire-app:{offer['entity_id']}")
                result.append(expired)
        return result

    # ------------------------------------------------------------ 证件到期

    def sweep_credential_expiry(self, context: AccessContext) -> list[dict]:
        """证件到期：登记到期并截断相关申请中尚未完成的环节，已发承诺不受影响。"""
        context.require("write:rec_application")
        now = self.clock.now()
        affected = []
        for credential in self.repository.list("credentials", state="effective", limit=500):
            valid_to = credential.get("valid_to")
            if valid_to and parse_instant(valid_to) <= parse_instant(now):
                expired = self.repository.update("credentials", credential["entity_id"], {"state": "expired"}, actor=context.actor_id, expected_version=credential["version"], request_key=f"expire:{credential['entity_id']}")
                affected.append(expired)
                for app in self._applications_using_credential(credential["entity_id"], states=CUTOFF_STATES):
                    self._cascade_cutoff(app["job_id"], reason="credential_expired", actor=context.actor_id,
                                         application_id=app["entity_id"], keep_offer_confirmation=True)
                    self._cancel_todo_if_credential(app["entity_id"], credential["entity_id"])
        return affected

    # ---------------------------------------------------------------- 查询

    def get_application(self, context: AccessContext, application_id: str) -> dict:
        context.require("read:rec_application")
        return self._redact(self._application(application_id), context)

    def list_applications(self, context: AccessContext, *, state: str | None = None, limit: int = 100) -> list[dict]:
        context.require("read:rec_application")
        if state is not None and state not in APPLICATION_STATES:
            raise ValidationError("未知状态")
        rows = self.repository.list(APPLICATION_TYPE, state=state, limit=limit)
        return [self._redact(row, context) for row in rows]

    def open_todos(self, context: AccessContext, *, due_only: bool = False, limit: int = 100) -> list[dict]:
        """持久待办：证件到期、面试协调、承诺确认。进程重启后仍可完整读出。"""
        context.require("read:rec_application")
        sql = "SELECT * FROM recruitment_todos WHERE status='open'"
        params: list[object] = []
        if due_only:
            sql += " AND due_at<=?"; params.append(self.clock.now())
        sql += " ORDER BY due_at,todo_id LIMIT ?"; params.append(limit)
        with self.database.connect() as connection:
            return [dict(row) for row in connection.execute(sql, params)]

    def explain(self, context: AccessContext, application_id: str) -> dict:
        """回答：为何被推荐、当时满足哪版要求、谁作出例外决定、下一步由谁处理。"""
        context.require("read:rec_application")
        app = self._application(application_id)
        frozen = app.get("frozen")
        job_snapshot = None
        if frozen:
            try:
                job_snapshot = self._exact_version(JOB_TYPE, app["job_id"], frozen["job_version"])
            except NotFoundError:
                job_snapshot = None
        credential_snapshots = []
        for ref in (frozen or {}).get("credential_refs", []):
            try:
                credential_snapshots.append(self._exact_version("credentials", ref["credential_id"], ref["version"]))
            except NotFoundError:
                credential_snapshots.append({"credential_id": ref["credential_id"], "version": ref["version"], "missing": True})
        interviews = [row for row in self.repository.list(INTERVIEW_TYPE, limit=500) if row["application_id"] == application_id]
        exceptions = [self._redact_exception(row) for row in self._exception_rows(application_id)]
        offers = [row for row in self.repository.list(OFFER_TYPE, limit=500)
                  if row["application_id"] == application_id]
        with self.database.connect() as connection:
            todos = [dict(row) for row in connection.execute("SELECT * FROM recruitment_todos WHERE application_id=? ORDER BY due_at", (application_id,))]
        return {
            "application": self._redact(app, context),
            "referral": {
                "referred_at": app.get("referred_at"),
                "referred_by": app.get("referred_by"),
                "job_version_used": (frozen or {}).get("job_version"),
                "requirements_used": (frozen or {}).get("requirements"),
                "credentials_used": (frozen or {}).get("credential_refs"),
                "match_report": app.get("match_report"),
                "job_snapshot": job_snapshot,
                "credential_snapshots": credential_snapshots,
            },
            "exceptions": exceptions,
            "interviews": sorted(interviews, key=lambda r: (r["round_no"], r["entity_id"])),
            "offers": sorted(offers, key=lambda r: r["issued_at"]),
            "open_todos": [t for t in todos if t["status"] == "open"],
            "next_owner": app.get("next_owner"),
            "next_action": app.get("next_action"),
            "timeline": self.timeline(context, application_id),
        }

    def timeline(self, context: AccessContext, application_id: str) -> list[dict]:
        """汇总该申请相关的全部审计记录（哈希链保护）。"""
        context.require("history:rec_application")
        ids = self._related_entity_ids(application_id)
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT occurred_at,actor_id,action,entity_type,entity_id,version,detail_json FROM audit_entries WHERE entity_id IN ({placeholders}) ORDER BY audit_id",
                list(ids),
            ).fetchall()
        return [{"occurred_at": r["occurred_at"], "actor_id": r["actor_id"], "action": r["action"],
                 "entity_type": r["entity_type"], "entity_id": r["entity_id"], "version": r["version"],
                 "detail": json.loads(r["detail_json"])} for r in rows]

    # ------------------------------------------------------------ 内部方法

    def _job(self, job_id: str) -> dict:
        return self.repository.get(JOB_TYPE, job_id)

    def _application(self, application_id: str) -> dict:
        return self.repository.get(APPLICATION_TYPE, application_id)

    def _exception_rows(self, application_id: str) -> list[dict]:
        return [row for row in self.repository.list(EXCEPTION_TYPE, limit=500) if row["application_id"] == application_id]

    def _approved_exceptions(self, application_id: str) -> dict:
        return {row["requirement_code"]: row["entity_id"] for row in self._exception_rows(application_id) if row["state"] == "approved"}

    def _applications_using_credential(self, credential_id: str, *, states: Iterable[str]) -> list[dict]:
        result = []
        for app in self.repository.list(APPLICATION_TYPE, limit=500):
            if states and app["state"] not in states:
                continue
            refs = (app.get("frozen") or {}).get("credential_refs", [])
            if any(ref.get("credential_id") == credential_id for ref in refs):
                result.append(app)
        return result

    def _related_entity_ids(self, application_id: str) -> list[str]:
        ids = {application_id}
        for interview in self.repository.list(INTERVIEW_TYPE, limit=500):
            if interview["application_id"] == application_id:
                ids.add(interview["entity_id"])
        for offer in self.repository.list(OFFER_TYPE, limit=500):
            if offer["application_id"] == application_id:
                ids.add(offer["entity_id"])
                if offer.get("amendment_of"):
                    ids.add(offer["amendment_of"])
        for exception in self._exception_rows(application_id):
            ids.add(exception["entity_id"])
        receipt = self._receipt_for_application(application_id)
        if receipt:
            ids.add(receipt["receipt_id"])
        return sorted(ids)

    def _receipt_for_application(self, application_id: str):
        with self.database.connect() as connection:
            return connection.execute("SELECT * FROM recruitment_receipts WHERE application_id=? ORDER BY created_at LIMIT 1", (application_id,)).fetchone()

    def _exact_version(self, entity_type: str, entity_id: str, version: int) -> dict:
        for row in self.repository.history(entity_type, entity_id):
            if row["version"] == version:
                return row
        raise NotFoundError(f"{entity_type}/{entity_id} 版本 {version} 不存在")

    def _freeze_credentials(self, credential_ids: Iterable[str], *, require_effective: bool) -> list[dict]:
        refs = []
        for credential_id in dict.fromkeys(credential_ids):
            credential = self.repository.get("credentials", credential_id)
            if require_effective and credential["state"] != "effective":
                raise ConflictError(f"证件 {credential_id} 状态为 {credential['state']}，不能用于本环节")
            if require_effective and credential.get("valid_to") and parse_instant(credential["valid_to"]) <= parse_instant(self.clock.now()):
                raise ConflictError(f"证件 {credential_id} 已过期")
            refs.append({"credential_id": credential_id, "version": credential["version"],
                         "credential_type": credential.get("credential_type"),
                         "state": credential["state"], "valid_to": credential.get("valid_to")})
        return refs

    def _match_requirements(self, job: dict, app: dict, credential_refs: list[dict]) -> list[dict]:
        report = []
        candidate_skills = {item["name"]: int(item.get("level", 0)) for item in app.get("skills", []) if isinstance(item, dict) and "name" in item}
        for required in job.get("skills", []):
            level = int(required.get("level", 0))
            report.append({"code": f"skill:{required['name']}", "kind": "skill", "name": required["name"],
                           "required_level": level, "actual_level": candidate_skills.get(required["name"]),
                           "satisfied": candidate_skills.get(required["name"], -1) >= level})
        candidate_langs = {item["language"]: int(item.get("level", 0)) for item in app.get("languages", []) if isinstance(item, dict) and "language" in item}
        for required in job.get("languages", []):
            level = int(required.get("level", 0))
            report.append({"code": f"lang:{required['language']}", "kind": "language", "name": required["language"],
                           "required_level": level, "actual_level": candidate_langs.get(required["language"]),
                           "satisfied": candidate_langs.get(required["language"], -1) >= level})
        visa = job.get("visa") or {}
        if visa.get("required"):
            permit_type = visa.get("permit_type")
            satisfied = any(ref.get("credential_type") == permit_type and ref.get("state") == "effective" for ref in credential_refs)
            report.append({"code": "visa", "kind": "visa", "name": permit_type,
                           "required_level": None, "actual_level": None, "satisfied": satisfied})
        return report

    def _assert_within_budget(self, job: dict, compensation: Mapping[str, object], application_id: str) -> None:
        budget = job.get("budget") or {}
        amount = int(compensation["salary_minor"])
        if compensation["currency"] != budget.get("currency"):
            raise ConflictError("薪酬币种与岗位预算不一致")
        within = int(budget.get("min", 0)) <= amount <= int(budget.get("max", 10**18))
        if within:
            return
        exceptions = self._approved_exceptions(application_id)
        if "budget" not in exceptions:
            raise ConflictError("薪酬超出岗位预算，且没有已批准的预算例外")

    def _assert_not_employer(self, context: AccessContext, employer_org: str) -> None:
        for membership in self.repository.list("memberships", state="active", limit=500):
            if membership.get("organization_id") == employer_org and membership.get("person_ref") == context.actor_id:
                raise PermissionDenied("企业不能批准自己岗位的资格例外")

    def _require_active_consent(self, app: dict) -> None:
        consent = app.get("consent", {})
        if consent.get("state") != "granted":
            raise ConflictError("候选人授权已撤回，不能继续该环节")
        if parse_instant(consent["valid_to"]) <= parse_instant(self.clock.now()):
            raise ConflictError("候选人授权已过期")

    def _cascade_cutoff(self, job_id: str, *, reason: str, actor: str, application_id: str | None = None,
                        keep_offer_confirmation: bool, app_state: str | None = None) -> None:
        """截断事件的统一传播：只终止尚未完成的环节，已完成环节与录用承诺保留。"""
        apps = []
        if application_id:
            apps = [self._application(application_id)]
        else:
            apps = [a for a in self.repository.list(APPLICATION_TYPE, limit=500)
                    if a["job_id"] == job_id and a["state"] in CUTOFF_STATES]
        for app in apps:
            for interview in self.repository.list(INTERVIEW_TYPE, limit=500):
                if interview["application_id"] == app["entity_id"] and interview["state"] == "scheduled":
                    self.repository.update(INTERVIEW_TYPE, interview["entity_id"],
                                           {"state": "cancelled", "cancel_reason": reason, "cancelled_at": self.clock.now()},
                                           actor=actor, expected_version=interview["version"],
                                           request_key=f"cutoff:{reason}:{interview['entity_id']}")
            kinds = list(CUTOFF_CANCELLABLE_TODOS)
            if not keep_offer_confirmation:
                kinds.append("offer_confirmation")
            self._cancel_todos(app["entity_id"], kinds=tuple(kinds))
            changes: dict = {"cutoff_reason": reason, "cutoff_at": self.clock.now()}
            if app_state:
                changes["state"] = app_state
                changes["next_owner"] = "employer"
                changes["next_action"] = "resolve_offer_by_rescission_or_amendment"
            elif app["state"] in ("referred", "interviewing"):
                # 尚无承诺：未完成环节终止，流程关闭。
                changes["state"] = "closed"
                changes["next_owner"] = "center_operator"
                changes["next_action"] = f"stopped:{reason}"
            else:
                # offered/accepted：既有录用承诺保留，只能走撤回记录或补充协议。
                changes["next_owner"] = "employer"
                changes["next_action"] = "resolve_offer_by_rescission_or_amendment"
            self.repository.update(APPLICATION_TYPE, app["entity_id"], changes, actor=actor,
                                   expected_version=app["version"], request_key=f"cutoff:{reason}:{app['entity_id']}")

    # -- 待办与定时任务（确定性标识，崩溃/重启后重放也不会重复） --

    def _todo_id(self, application_id: str, kind: str, suffix: str) -> str:
        return f"todo:{application_id}:{kind}:{suffix}"

    def _ensure_todo(self, application_id: str, kind: str, suffix: object = "", *, due_at: str, **detail) -> str:
        require_safe(kind, "待办类型")
        suffix = str(suffix or "0")
        todo_id = self._todo_id(application_id, kind, suffix)
        now = self.clock.now()
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO recruitment_todos(todo_id,application_id,todo_kind,due_at,status,detail_json,created_at,updated_at) VALUES(?,?,?,?, 'open',?,?,?)",
                (todo_id, application_id, kind, canonical_instant(due_at), canonical_json(detail or {}), now, now),
            )
        return todo_id

    def _complete_todo(self, application_id: str, kind: str, **detail) -> None:
        with self.database.transaction() as connection:
            rows = connection.execute("SELECT todo_id,detail_json FROM recruitment_todos WHERE application_id=? AND todo_kind=? AND status='open'", (application_id, kind)).fetchall()
            for row in rows:
                existing = json.loads(row["detail_json"])
                if all(existing.get(key) == value for key, value in detail.items()):
                    connection.execute("UPDATE recruitment_todos SET status='done',updated_at=? WHERE todo_id=?", (self.clock.now(), row["todo_id"]))

    def _cancel_todo_if_credential(self, application_id: str, credential_id: str) -> None:
        with self.database.transaction() as connection:
            rows = connection.execute("SELECT todo_id,detail_json FROM recruitment_todos WHERE application_id=? AND status='open'", (application_id,)).fetchall()
            for row in rows:
                if json.loads(row["detail_json"]).get("credential_id") == credential_id:
                    connection.execute("UPDATE recruitment_todos SET status='cancelled',updated_at=? WHERE todo_id=?", (self.clock.now(), row["todo_id"]))

    def _cancel_todos(self, application_id: str, *, kinds: Iterable[str]) -> None:
        kinds = tuple(kinds)
        if not kinds:
            return
        placeholders = ",".join("?" for _ in kinds)
        with self.database.transaction() as connection:
            connection.execute(
                f"UPDATE recruitment_todos SET status='cancelled',updated_at=? WHERE application_id=? AND status='open' AND todo_kind IN ({placeholders})",
                (self.clock.now(), application_id, *kinds),
            )

    def _ensure_job(self, application_id: str, kind: str, suffix: object, *, run_at: str) -> str:
        job_id = f"job:{application_id}:{kind}:{suffix}"
        self.jobs.schedule_known(job_id=job_id, job_type=f"rec:{kind}", subject_id=application_id,
                                 run_at=run_at, payload={"kind": kind, "application_id": application_id, "ref": suffix})
        return job_id

    # -- 联系方式可见性 --

    def _can_reveal_contact(self, context: AccessContext, app: Mapping[str, object]) -> bool:
        if context.reveal_sensitive:
            return True
        if not context.allows("contact:recruitment"):
            return False
        if context.has_scope("*") or context.has_scope(f"rec_app:{app['entity_id']}") or context.has_scope(f"rec_job:{app['job_id']}"):
            return True
        # 获授权人员：候选人授权仍有效，且访问者是招聘企业的在岗成员。
        if app.get("consent", {}).get("state") != "granted":
            return False
        employer_org = self._job(app["job_id"]).get("employer_org")
        for membership in self.repository.list("memberships", state="active", limit=500):
            if membership.get("organization_id") == employer_org and membership.get("person_ref") == context.actor_id:
                return True
        return False

    def _redact(self, app: dict, context: AccessContext) -> dict:
        result = dict(app)
        if not self._can_reveal_contact(context, app):
            for field_name in CONTACT_FIELDS:
                if field_name in result:
                    result[field_name] = "***"
        return result

    def _redact_exception(self, row: dict) -> dict:
        return {key: row[key] for key in ("entity_id", "state", "requirement_code", "reason", "requested_by", "reviewer", "decided_at", "decision_detail", "employer_org") if key in row}

    # -- 事务内辅助 --

    def _insert_entity(self, connection, entity_type: str, entity_id: str, payload: dict, *, actor: str, request_key: str) -> None:
        now = self.clock.now()
        state = str(payload.get("state", "draft"))
        connection.execute(
            "INSERT INTO entities(entity_type,entity_id,version,state,payload_json,created_at,updated_at,created_by,updated_by) VALUES(?,?,?,?,?,?,?,?,?)",
            (entity_type, entity_id, 1, state, canonical_json(payload), now, now, actor, actor),
        )
        connection.execute(
            "INSERT INTO entity_versions(entity_type,entity_id,version,state,payload_json,valid_from,actor_id,request_key) VALUES(?,?,?,?,?,?,?,?)",
            (entity_type, entity_id, 1, state, canonical_json(payload), now, actor, request_key),
        )
        self._audit(connection, actor=actor, action="create", entity_type=entity_type, entity_id=entity_id, version=1, detail=payload)

    def _update_entity_in_txn(self, connection, entity_type: str, entity_id: str, changes: dict, *, expected_version: int, actor: str, request_key: str) -> None:
        """与 EntityRepository.update 等价，但复用调用方已打开的事务连接。"""
        row = connection.execute("SELECT * FROM entities WHERE entity_type=? AND entity_id=?", (entity_type, entity_id)).fetchone()
        if not row:
            raise NotFoundError(f"{entity_type}/{entity_id} 不存在")
        if row["version"] != expected_version:
            raise ConflictError(f"版本冲突，当前为 {row['version']}")
        payload = json.loads(row["payload_json"])
        payload.update(changes)
        version = expected_version + 1
        state = str(payload.get("state", row["state"]))
        now = self.clock.now()
        changed = connection.execute(
            "UPDATE entities SET version=?,state=?,payload_json=?,updated_at=?,updated_by=? WHERE entity_type=? AND entity_id=? AND version=?",
            (version, state, canonical_json(payload), now, actor, entity_type, entity_id, expected_version),
        ).rowcount
        if changed != 1:
            raise ConflictError("并发修改导致版本变化")
        connection.execute(
            "INSERT INTO entity_versions(entity_type,entity_id,version,state,payload_json,valid_from,actor_id,request_key) VALUES(?,?,?,?,?,?,?,?)",
            (entity_type, entity_id, version, state, canonical_json(payload), now, actor, request_key),
        )
        self._audit(connection, actor=actor, action="update", entity_type=entity_type, entity_id=entity_id, version=version, detail=changes)

    def _audit(self, connection, *, actor: str, action: str, entity_type: str, entity_id: str, version: int, detail: dict) -> None:
        AuditLog(self.clock).append(connection, actor_id=actor, action=action, entity_type=entity_type,
                                    entity_id=entity_id, version=version, detail=detail)

    # -- 校验 --

    def _validate_job(self, values: Mapping[str, object], *, partial: bool) -> dict:
        fields = {"employer_org", "title", "skills", "languages", "visa", "budget"}
        unknown = set(values) - fields
        if unknown:
            raise ValidationError("未知字段: " + ", ".join(sorted(unknown)))
        if not partial and not fields.issubset(values):
            raise ValidationError("缺少字段: " + ", ".join(sorted(fields - set(values))))
        payload: dict = {}
        if "employer_org" in values:
            payload["employer_org"] = require_safe(str(values["employer_org"]), "企业组织")
        if "title" in values:
            title = str(values["title"]).strip()
            if not title:
                raise ValidationError("岗位名称不能为空")
            payload["title"] = title
        if "skills" in values:
            payload["skills"] = _validate_requirement_list(values["skills"], "name", "技能")
        if "languages" in values:
            payload["languages"] = _validate_requirement_list(values["languages"], "language", "语言")
        if "visa" in values:
            payload["visa"] = self._validate_visa(values["visa"])
        if "budget" in values:
            payload["budget"] = self._validate_budget(values["budget"])
        return payload

    @staticmethod
    def _validate_visa(value: object) -> dict:
        if not isinstance(value, dict) or not isinstance(value.get("required"), bool):
            raise ValidationError("签证要求必须包含 required 布尔值")
        visa = {"required": value["required"], "permit_type": None}
        if value["required"]:
            permit = value.get("permit_type")
            if not isinstance(permit, str) or not permit.strip():
                raise ValidationError("必须指明签证/工作许可类型")
            visa["permit_type"] = permit.strip()
        return visa

    @staticmethod
    def _validate_budget(value: object) -> dict:
        if not isinstance(value, dict):
            raise ValidationError("预算必须是对象")
        currency = str(value.get("currency", "")).strip()
        if not currency:
            raise ValidationError("预算币种不能为空")
        try:
            low = int(value["min"]); high = int(value["max"])
        except (KeyError, TypeError, ValueError):
            raise ValidationError("预算上下限必须是整数（最小货币单位）")
        if low < 0 or high < low:
            raise ValidationError("预算区间不合法")
        return {"currency": currency, "min": low, "max": high}

    @staticmethod
    def _validate_compensation(value: object) -> dict:
        if not isinstance(value, dict):
            raise ValidationError("薪酬必须是对象")
        currency = str(value.get("currency", "")).strip()
        if not currency:
            raise ValidationError("薪酬币种不能为空")
        try:
            amount = int(value["salary_minor"])
        except (KeyError, TypeError, ValueError):
            raise ValidationError("salary_minor 必须是整数")
        if amount < 0:
            raise ValidationError("薪酬不能为负")
        return {"currency": currency, "salary_minor": amount}


def _validate_requirement_list(value: object, key: str, label: str) -> list[dict]:
    if not isinstance(value, list):
        raise ValidationError(f"{label}要求必须是列表")
    result = []
    for item in value:
        if not isinstance(item, dict) or not str(item.get(key, "")).strip():
            raise ValidationError(f"每项{label}要求必须包含 {key}")
        try:
            level = int(item.get("level", 0))
        except (TypeError, ValueError):
            raise ValidationError(f"{label}等级必须是整数")
        entry = {key: str(item[key]).strip(), "level": level}
        result.append(entry)
    return result


def _reason(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("必须说明原因")
    return value.strip()


def _require_actor(value: object, label: str = "标识") -> str:
    """人员/要求项标识允许邮箱等平台安全字符集之外的形式（如 @、中文名），但不允许空白。"""
    text = str(value).strip()
    if not text or len(text) > 128 or any(ch.isspace() for ch in text):
        raise ValidationError(f"{label}不合法")
    return text


_require_code = _require_actor
