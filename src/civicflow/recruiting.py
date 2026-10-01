"""可追溯的国际人才招聘编排。

在协同事务平台已有的版本化实体仓库、审计链、幂等键、定时任务、
发件箱和消息接入能力之上，把岗位、技能与语言要求、签证/工作许可、
候选人授权、面试轮次和录用承诺期限串成一条可追溯流程：

* 岗位（recruiting_jobs）逐版本留存；每次推荐和面试安排都冻结当时的
  岗位版本、资格要求摘要、证件版本和候选人授权版本。
* 候选人撤回授权、证件到期、岗位关闭或预算变化只终止尚未完成的环节；
  已发出的录用承诺不会被悄悄覆盖，只能通过撤回记录或补充协议衔接。
* 企业可以维护本方岗位，但资格例外必须由非本企业人员审批。
* 候选人联系方式只对授权范围内的人员开放。
* 重复简历回执保留原进度；标识相同而材料不同进入人工核验队列。
* 证件到期、面试协调、承诺确认期限全部落持久化任务队列，重启不丢。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .jsonutil import digest_json
from .repository import EntityRepository
from .security import AccessContext, assert_distinct, redact_record
from .timeutil import canonical_instant, parse_instant
from .jobs import JobQueue
from .outbox import Outbox


JOB_TYPE = "recruiting_jobs"
CANDIDATE_TYPE = "candidates"
CONSENT_TYPE = "candidate_consents"
REFERRAL_TYPE = "referrals"
INTERVIEW_TYPE = "interviews"
OFFER_TYPE = "offers"
AMENDMENT_TYPE = "offer_amendments"
EXCEPTION_TYPE = "eligibility_exceptions"

CONTACT_FIELDS = ("contact",)

JOB_STATES = ("draft", "open", "suspended", "closed")
JOB_TRANSITIONS = {
    "draft": {"open"},
    "open": {"suspended", "closed"},
    "suspended": {"open", "closed"},
    "closed": set(),
}
CANDIDATE_STATES = ("active", "archived")
CONSENT_STATES = ("granted", "withdrawn", "expired")
REFERRAL_STATES = (
    "recommended",        # 已推荐，等待安排
    "interviewing",       # 面试轮次进行中
    "offered",            # 已发出录用承诺
    "hired",              # 候选人确认、流程完成
    "declined",           # 候选人拒绝
    "cancelled",          # 因撤回/到期/关闭/预算变化终止未完成环节
)
UNFINISHED_REFERRAL_STATES = ("recommended", "interviewing")
# 承诺已发出但尚未确认的阶段，承诺本身受保护，但其协调环节仍可被终止
STATES_WITH_OPEN_STEPS = ("recommended", "interviewing", "offered")
INTERVIEW_STATES = ("scheduled", "completed", "cancelled")
OFFER_STATES = ("offered", "confirmed", "withdrawn", "expired")
AMENDMENT_STATES = ("proposed", "accepted", "rejected")
EXCEPTION_STATES = ("pending", "approved", "rejected", "cancelled")

LANGUAGE_LEVELS = {"a1": 1, "a2": 2, "b1": 3, "b2": 4, "c1": 5, "c2": 6}

# 定时任务类型
JOB_CREDENTIAL_EXPIRY = "recruiting.credential_expiry"
JOB_INTERVIEW_REMINDER = "recruiting.interview_reminder"
JOB_OFFER_DEADLINE = "recruiting.offer_deadline"


def _clean_str(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label}不能为空")
    return value.strip()


@dataclass(frozen=True)
class RecruitingService:
    """招聘流程编排服务。"""

    repository: EntityRepository
    jobs: JobQueue
    outbox: Outbox

    # ------------------------------------------------------------------ #
    # 岗位维护：企业可维护本方岗位，所有改动逐版本留存
    # ------------------------------------------------------------------ #

    def create_job(self, context: AccessContext, values: Mapping[str, object], *, request_key: str) -> dict:
        context.require("write:recruiting_jobs")
        payload = self._validate_job(values, partial=False)
        self._require_org_maintainer(context, payload["employer_org"])
        payload["state"] = "draft"
        return self.repository.create(JOB_TYPE, payload, actor=context.actor_id, request_key=request_key)

    def revise_job(self, context: AccessContext, job_id: str, values: Mapping[str, object], *, expected_version: int, request_key: str) -> dict:
        context.require("write:recruiting_jobs")
        current = self.repository.get(JOB_TYPE, job_id)
        if current["state"] == "closed":
            raise ConflictError("岗位已关闭，不能修改；请新建岗位版本")
        self._require_org_maintainer(context, current["employer_org"])
        payload = self._validate_job(values, partial=True)
        payload.pop("state", None)
        if "employer_org" in payload and payload["employer_org"] != current.get("employer_org"):
            raise ValidationError("岗位归属企业不可变更")
        payload.pop("employer_org", None)
        updated = self.repository.update(JOB_TYPE, job_id, payload, actor=context.actor_id, expected_version=expected_version, request_key=request_key)
        # 预算或薪酬变化只终止尚未完成的环节；要求变化仅产生新版本，不影响已冻结的推荐
        if self._budget_changed(current, updated) and current["state"] == "open":
            self._cancel_unfinished_for_job(job_id, reason="budget_changed", actor=context.actor_id)
        return updated

    def transition_job(self, context: AccessContext, job_id: str, target: str, *, expected_version: int, reason: str, request_key: str) -> dict:
        context.require("transition:recruiting_jobs")
        current = self.repository.get(JOB_TYPE, job_id)
        self._require_org_maintainer(context, current["employer_org"])
        if target not in JOB_TRANSITIONS.get(str(current["state"]), set()):
            raise ConflictError(f"不允许从 {current['state']} 转到 {target}")
        if not reason.strip():
            raise ValidationError("状态变更必须说明原因")
        updated = self.repository.update(JOB_TYPE, job_id, {"state": target, "transition_reason": reason.strip()}, actor=context.actor_id, expected_version=expected_version, request_key=request_key)
        if target == "closed":
            self._cancel_unfinished_for_job(job_id, reason="job_closed", actor=context.actor_id)
        return updated

    def get_job(self, context: AccessContext, job_id: str) -> dict:
        context.require("read:recruiting_jobs")
        return self.repository.get(JOB_TYPE, job_id)

    def list_jobs(self, context: AccessContext, *, state: str | None = None) -> list[dict]:
        context.require("read:recruiting_jobs")
        if state is not None and state not in JOB_STATES:
            raise ValidationError("未知岗位状态")
        return self.repository.list(JOB_TYPE, state=state)

    def job_history(self, context: AccessContext, job_id: str) -> list[dict]:
        context.require("history:recruiting_jobs")
        rows = self.repository.history(JOB_TYPE, job_id)
        if not rows:
            raise NotFoundError(f"{JOB_TYPE}/{job_id} 不存在")
        return rows

    # ------------------------------------------------------------------ #
    # 候选人与联系方式授权
    # ------------------------------------------------------------------ #

    def register_candidate(self, context: AccessContext, values: Mapping[str, object], *, request_key: str) -> dict:
        context.require("write:candidates")
        full_name = _clean_str(values.get("full_name"), "候选人姓名")
        contact = _clean_str(values.get("contact"), "联系方式")
        payload = {"full_name": full_name, "contact": contact, "state": "active"}
        return self.repository.create(CANDIDATE_TYPE, payload, actor=context.actor_id, request_key=request_key)

    def grant_consent(self, context: AccessContext, candidate_id: str, values: Mapping[str, object], *, request_key: str) -> dict:
        context.require("write:candidate_consents")
        self.repository.get(CANDIDATE_TYPE, candidate_id)
        audience = values.get("audience", ["*"])
        if not isinstance(audience, list) or not audience or not all(isinstance(a, str) and a.strip() for a in audience):
            raise ValidationError("授权对象必须是非空人员标识列表")
        scopes = values.get("scopes", ["contact", "referral"])
        if not isinstance(scopes, list) or not scopes:
            raise ValidationError("授权范围不合法")
        valid_until = canonical_instant(_clean_str(values.get("valid_until"), "授权截止时间"))
        payload = {
            "candidate_id": candidate_id,
            "audience": sorted(a.strip() for a in audience),
            "scopes": sorted(scopes),
            "valid_until": valid_until,
            "granted_by": _clean_str(values.get("granted_by", context.actor_id), "授权确认人"),
            "state": "granted",
        }
        return self.repository.create(CONSENT_TYPE, payload, actor=context.actor_id, request_key=request_key)

    def withdraw_consent(self, context: AccessContext, consent_id: str, *, reason: str, request_key: str) -> dict:
        """候选人撤回授权：只终止尚未完成的环节，录用承诺保留。"""
        context.require("withdraw:candidate_consents")
        if not reason.strip():
            raise ValidationError("撤回授权必须说明原因")
        current = self.repository.get(CONSENT_TYPE, consent_id)
        if current["state"] != "granted":
            raise ConflictError(f"授权状态为 {current['state']}，不能撤回")
        updated = self.repository.update(
            CONSENT_TYPE, consent_id,
            {"state": "withdrawn", "withdrawn_reason": reason.strip(), "withdrawn_at": self._now()},
            actor=context.actor_id, expected_version=current["version"], request_key=request_key,
        )
        self._cancel_unfinished_for_candidate(current["candidate_id"], reason="consent_withdrawn", actor=context.actor_id)
        self.outbox.enqueue(topic="candidate.consent_withdrawn", aggregate_id=current["candidate_id"],
                            payload={"candidate_id": current["candidate_id"], "consent_id": consent_id})
        return updated

    def get_candidate(self, context: AccessContext, candidate_id: str) -> dict:
        context.require("read:candidates")
        record = self.repository.get(CANDIDATE_TYPE, candidate_id)
        if not self._can_see_contact(context, candidate_id):
            record = redact_record(record, CONTACT_FIELDS, context)
        return record

    def _can_see_contact(self, context: AccessContext, candidate_id: str) -> bool:
        if context.reveal_sensitive:
            return True
        for consent in self.repository.list(CONSENT_TYPE, state="granted", limit=500):
            if consent["candidate_id"] != candidate_id:
                continue
            if parse_instant(consent["valid_until"]) <= parse_instant(self._now()):
                continue
            audience = consent.get("audience", [])
            if "*" in audience or context.actor_id in audience:
                return True
        return False

    # ------------------------------------------------------------------ #
    # 简历回执：重复保留原进度；同标识不同材料进入人工核验
    # ------------------------------------------------------------------ #

    def receive_resume(self, context: AccessContext, *, receipt_key: str, candidate_id: str, materials: Mapping[str, object], request_key: str) -> dict:
        context.require("write:referrals")
        _clean_str(receipt_key, "回执标识")
        self.repository.get(CANDIDATE_TYPE, candidate_id)
        if not isinstance(materials, Mapping) or not materials:
            raise ValidationError("资格材料不能为空")
        materials_digest = digest_json(materials)
        now = self._now()
        with self.repository.database.transaction() as connection:
            row = connection.execute("SELECT * FROM resume_receipts WHERE receipt_key=?", (receipt_key,)).fetchone()
            if row:
                if row["materials_digest"] == materials_digest:
                    # 同一份简历重复回执：原进度原样保留
                    referral = self._referral_brief(row["referral_id"]) if row["referral_id"] else None
                    connection.execute("UPDATE resume_receipts SET last_seen_at=? WHERE receipt_key=?", (now, receipt_key))
                    return {"status": "duplicate", "receipt_key": receipt_key, "progress_kept": True, "referral": referral}
                # 标识相同、材料不同：登记人工核验（事务提交后再抛错，避免核验记录被回滚）
                connection.execute(
                    "INSERT INTO resume_review_queue(receipt_key,candidate_id,existing_digest,incoming_digest,status,detected_at) VALUES(?,?,?,?,?,?)",
                    (receipt_key, candidate_id, row["materials_digest"], materials_digest, "pending", now),
                )
            else:
                # 同一份材料换了回执标识再次提交，同样视为重复，保留原进度
                prior = connection.execute("SELECT * FROM resume_receipts WHERE candidate_id=? AND materials_digest=?", (candidate_id, materials_digest)).fetchone()
                if prior:
                    referral = self._referral_brief(prior["referral_id"]) if prior["referral_id"] else None
                    return {"status": "duplicate", "receipt_key": prior["receipt_key"], "progress_kept": True, "referral": referral}
                connection.execute(
                    "INSERT INTO resume_receipts(receipt_key,candidate_id,materials_digest,status,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?)",
                    (receipt_key, candidate_id, materials_digest, "accepted", now, now),
                )
                return {"status": "accepted", "receipt_key": receipt_key, "materials_digest": materials_digest}
        raise ConflictError("简历标识相同但资格材料不同，已转入人工核验")

    def list_manual_reviews(self, context: AccessContext, *, status: str = "pending") -> list[dict]:
        context.require("recruiting:operate")
        with self.repository.database.connect() as connection:
            rows = connection.execute("SELECT * FROM resume_review_queue WHERE status=? ORDER BY detected_at", (status,)).fetchall()
            return [dict(row) for row in rows]

    def resolve_manual_review(self, context: AccessContext, review_id: int, *, resolution: str, note: str, request_key: str) -> dict:
        """人工核验结论：keep=维持原进度；accept_new=以新材料重新推荐（原进度仍可追溯）。"""
        context.require("recruiting:operate")
        if resolution not in ("keep", "accept_new"):
            raise ValidationError("核验结论必须是 keep 或 accept_new")
        now = self._now()
        with self.repository.database.transaction() as connection:
            row = connection.execute("SELECT * FROM resume_review_queue WHERE review_id=?", (review_id,)).fetchone()
            if not row:
                raise NotFoundError("核验任务不存在")
            if row["status"] != "pending":
                raise ConflictError("核验任务已处理")
            if resolution == "accept_new":
                connection.execute(
                    "UPDATE resume_receipts SET materials_digest=?,last_seen_at=? WHERE receipt_key=?",
                    (row["incoming_digest"], now, row["receipt_key"]),
                )
            connection.execute(
                "UPDATE resume_review_queue SET status='resolved',resolved_at=?,resolution=? WHERE review_id=?",
                (now, f"{resolution}:{note.strip()}"[:500], review_id),
            )
            return {"review_id": review_id, "resolution": resolution, "receipt_key": row["receipt_key"]}

    # ------------------------------------------------------------------ #
    # 推荐：冻结岗位版本、证件版本、授权版本及当时的资格判定
    # ------------------------------------------------------------------ #

    def recommend(self, context: AccessContext, values: Mapping[str, object], *, request_key: str) -> dict:
        context.require("write:referrals")
        job_id = _clean_str(values.get("job_id"), "岗位")
        candidate_id = _clean_str(values.get("candidate_id"), "候选人")
        job = self.repository.get(JOB_TYPE, job_id)
        if job["state"] != "open":
            raise ConflictError("岗位未处于开放状态，不能推荐")
        self.repository.get(CANDIDATE_TYPE, candidate_id)

        consent = self._require_active_consent(candidate_id)
        credentials = self._freeze_credentials(values.get("credential_ids", []))
        evaluation = self._evaluate(job, credentials)
        receipt_key = str(values["receipt_key"]).strip() if values.get("receipt_key") else None
        if receipt_key:
            with self.repository.database.connect() as connection:
                row = connection.execute("SELECT candidate_id FROM resume_receipts WHERE receipt_key=?", (receipt_key,)).fetchone()
            if not row:
                raise NotFoundError("简历回执不存在，请先接收简历")
            if row["candidate_id"] != candidate_id:
                raise ValidationError("简历回执与候选人不一致")

        payload = {
            "job_id": job_id,
            "candidate_id": candidate_id,
            "job_version": job["version"],
            "frozen_at": self._now(),
            "requirements_digest": self._requirements_digest(job),
            "job_snapshot": {
                "title": job["title"],
                "skills": job["skills"],
                "languages": job["languages"],
                "work_permit": job["work_permit"],
                "salary_band": job["salary_band"],
            },
            "credential_freeze": credentials,
            "consent_freeze": {
                "consent_id": consent["entity_id"],
                "version": consent["version"],
                "valid_until": consent["valid_until"],
                "audience": consent["audience"],
            },
            "eligibility": evaluation,
            "exception_id": None,
            "receipt_key": receipt_key,
            "current_owner": context.actor_id,
            "cancel_reason": None,
            "state": "recommended",
        }
        referral = self.repository.create(REFERRAL_TYPE, payload, actor=context.actor_id, request_key=request_key)
        if receipt_key:
            self._link_receipt(receipt_key, referral["entity_id"])
        # 为每个证件安排到期检查，重启后仍在持久化队列中
        for credential in credentials:
            if credential.get("valid_to"):
                self.jobs.schedule(job_type=JOB_CREDENTIAL_EXPIRY, subject_id=referral["entity_id"],
                                   run_at=credential["valid_to"],
                                   payload={"referral_id": referral["entity_id"], "credential_id": credential["credential_id"]})
        self.outbox.enqueue(topic="referral.created", aggregate_id=referral["entity_id"],
                            payload={"referral_id": referral["entity_id"], "job_id": job_id, "job_version": job["version"]})
        return referral

    def request_exception(self, context: AccessContext, referral_id: str, values: Mapping[str, object], *, request_key: str) -> dict:
        """企业招聘人员为未满足的资格项申请例外；自己不能批准。"""
        context.require("write:eligibility_exceptions")
        referral = self.repository.get(REFERRAL_TYPE, referral_id)
        missing = {item["requirement"] for item in referral["eligibility"].get("missing", [])}
        requirement = _clean_str(values.get("requirement"), "资格项")
        if requirement not in missing:
            raise ValidationError("只能对推荐时判定未满足的资格项申请例外")
        payload = {
            "referral_id": referral_id,
            "job_id": referral["job_id"],
            "requirement": requirement,
            "justification": _clean_str(values.get("justification"), "例外理由"),
            "requested_by": context.actor_id,
            "reviewer": None,
            "decision_note": None,
            "decided_at": None,
            "state": "pending",
        }
        exception = self.repository.create(EXCEPTION_TYPE, payload, actor=context.actor_id, request_key=request_key)
        self.repository.update(REFERRAL_TYPE, referral_id, {"exception_id": exception["entity_id"]},
                               actor=context.actor_id, expected_version=referral["version"],
                               request_key=request_key + ":link")
        return exception

    def decide_exception(self, context: AccessContext, exception_id: str, *, approve: bool, note: str, expected_version: int, request_key: str) -> dict:
        """资格例外由人才服务中心（非用人企业）审批。"""
        context.require("approve:eligibility_exceptions")
        if not note.strip():
            raise ValidationError("例外审批必须填写意见")
        exception = self.repository.get(EXCEPTION_TYPE, exception_id)
        if exception["state"] != "pending":
            raise ConflictError("例外申请已处理")
        # 申请人不能审批自己的申请
        assert_distinct(exception["requested_by"], context.actor_id)
        # 审批人不能属于用人企业
        job = self.repository.get(JOB_TYPE, exception["job_id"])
        if self._is_member_of(context.actor_id, job["employer_org"]):
            raise PermissionDenied("企业不能批准自己的资格例外")
        changes = {
            "state": "approved" if approve else "rejected",
            "reviewer": context.actor_id,
            "decision_note": note.strip(),
            "decided_at": self._now(),
        }
        return self.repository.update(EXCEPTION_TYPE, exception_id, changes, actor=context.actor_id,
                                      expected_version=expected_version, request_key=request_key)

    # ------------------------------------------------------------------ #
    # 面试轮次：安排时再次冻结岗位与证件版本
    # ------------------------------------------------------------------ #

    def schedule_interview(self, context: AccessContext, values: Mapping[str, object], *, request_key: str) -> dict:
        context.require("write:interviews")
        referral_id = _clean_str(values.get("referral_id"), "推荐记录")
        referral = self.repository.get(REFERRAL_TYPE, referral_id)
        if referral["state"] not in STATES_WITH_OPEN_STEPS:
            raise ConflictError(f"推荐记录状态为 {referral['state']}，不能安排面试")
        # 存在未满足项且没有已批准例外时，不能进入面试
        self._require_requirements_resolved(referral)
        self._require_active_consent(referral["candidate_id"])

        round_no = values.get("round")
        if not isinstance(round_no, int) or round_no < 1:
            raise ValidationError("面试轮次必须是正整数")
        scheduled_at = canonical_instant(_clean_str(values.get("scheduled_at"), "面试时间"))
        panel = values.get("panel", [])
        if not isinstance(panel, list) or not panel:
            raise ValidationError("面试 panel 不能为空")

        # 再次冻结当时的岗位版本与证件版本：即使岗位后来改版，本轮面试仍锚定旧版
        job = self.repository.get(JOB_TYPE, referral["job_id"])
        credential_freeze = self._refreeze_credentials(referral["credential_freeze"])
        now = parse_instant(self._now())
        for frozen in credential_freeze:
            if frozen["state"] not in ("effective", "verified"):
                raise ConflictError(f"证件 {frozen['credential_id']} 状态为 {frozen['state']}，不能安排面试")
            if frozen.get("valid_to") and parse_instant(frozen["valid_to"]) <= now:
                raise ConflictError(f"证件 {frozen['credential_id']} 已到期，不能安排面试")
        payload = {
            "referral_id": referral_id,
            "round": round_no,
            "scheduled_at": scheduled_at,
            "panel": list(panel),
            "scheduled_by": context.actor_id,
            "job_version": job["version"],
            "requirements_digest": self._requirements_digest(job),
            "credential_freeze": credential_freeze,
            "location": _clean_str(values.get("location", "待定"), "面试地点"),
            "result": None,
            "state": "scheduled",
            "cancel_reason": None,
        }
        interview = self.repository.create(INTERVIEW_TYPE, payload, actor=context.actor_id, request_key=request_key)
        if referral["state"] == "recommended":
            self.repository.update(REFERRAL_TYPE, referral_id, {"state": "interviewing", "current_owner": context.actor_id},
                                   actor=context.actor_id, expected_version=referral["version"],
                                   request_key=request_key + ":stage")
        # 面试协调提醒落持久化队列
        self.jobs.schedule(job_type=JOB_INTERVIEW_REMINDER, subject_id=interview["entity_id"],
                           run_at=scheduled_at, payload={"interview_id": interview["entity_id"], "referral_id": referral_id})
        self.outbox.enqueue(topic="interview.scheduled", aggregate_id=interview["entity_id"],
                            payload={"interview_id": interview["entity_id"], "scheduled_at": scheduled_at})
        return interview

    def complete_interview(self, context: AccessContext, interview_id: str, *, result: str, request_key: str) -> dict:
        context.require("write:interviews")
        current = self.repository.get(INTERVIEW_TYPE, interview_id)
        if current["state"] != "scheduled":
            raise ConflictError("面试不在待举行状态")
        result = _clean_str(result, "面试结论")
        updated = self.repository.update(INTERVIEW_TYPE, interview_id, {"state": "completed", "result": result},
                                         actor=context.actor_id, expected_version=current["version"], request_key=request_key)
        referral = self.repository.get(REFERRAL_TYPE, current["referral_id"])
        self.repository.update(REFERRAL_TYPE, referral["entity_id"], {"current_owner": context.actor_id},
                               actor=context.actor_id, expected_version=referral["version"],
                               request_key=request_key + ":owner")
        return updated

    # ------------------------------------------------------------------ #
    # 录用承诺：发出后不可覆盖，只能撤回或补充协议衔接
    # ------------------------------------------------------------------ #

    def issue_offer(self, context: AccessContext, values: Mapping[str, object], *, request_key: str) -> dict:
        context.require("write:offers")
        referral_id = _clean_str(values.get("referral_id"), "推荐记录")
        referral = self.repository.get(REFERRAL_TYPE, referral_id)
        if referral["state"] not in ("interviewing", "recommended"):
            raise ConflictError(f"推荐记录状态为 {referral['state']}，不能发出承诺")
        self._require_requirements_resolved(referral)
        job = self.repository.get(JOB_TYPE, referral["job_id"])
        if job["state"] != "open":
            raise ConflictError("岗位已关闭，不能新发出录用承诺")

        terms = values.get("terms")
        if not isinstance(terms, Mapping) or not terms:
            raise ValidationError("承诺条款不能为空")
        band = job["salary_band"]
        compensation = terms.get("compensation")
        if compensation is None:
            raise ValidationError("承诺必须包含薪酬条款")
        if isinstance(compensation, (int, float)) and "max" in band and compensation > band["max"]:
            raise ConflictError("承诺薪酬高于岗位区间上限，需通过补充协议或例外流程")
        valid_until = canonical_instant(_clean_str(values.get("valid_until"), "承诺确认期限"))
        if parse_instant(valid_until) <= parse_instant(self._now()):
            raise ValidationError("承诺确认期限必须晚于当前时间")

        payload = {
            "referral_id": referral_id,
            "candidate_id": referral["candidate_id"],
            "job_id": referral["job_id"],
            "job_version": job["version"],
            "requirements_digest": self._requirements_digest(job),
            "terms": dict(terms),
            "committed_by": context.actor_id,
            "committed_at": self._now(),
            "valid_until": valid_until,
            "accepted_amendment_ids": [],
            "withdrawal": None,
            "state": "offered",
        }
        offer = self.repository.create(OFFER_TYPE, payload, actor=context.actor_id, request_key=request_key)
        self.repository.update(REFERRAL_TYPE, referral_id, {"state": "offered", "current_owner": referral["candidate_id"]},
                               actor=context.actor_id, expected_version=referral["version"],
                               request_key=request_key + ":stage")
        # 承诺确认期限落持久化队列
        self.jobs.schedule(job_type=JOB_OFFER_DEADLINE, subject_id=offer["entity_id"],
                           run_at=valid_until, payload={"offer_id": offer["entity_id"]})
        self.outbox.enqueue(topic="offer.issued", aggregate_id=offer["entity_id"],
                            payload={"offer_id": offer["entity_id"], "valid_until": valid_until})
        return offer

    def confirm_offer(self, context: AccessContext, offer_id: str, *, request_key: str) -> dict:
        """候选人确认录用承诺。"""
        context.require("confirm:offers")
        offer = self.repository.get(OFFER_TYPE, offer_id)
        if offer["state"] != "offered":
            raise ConflictError(f"承诺状态为 {offer['state']}，不能确认")
        updated = self.repository.update(OFFER_TYPE, offer_id, {"state": "confirmed", "confirmed_at": self._now()},
                                         actor=context.actor_id, expected_version=offer["version"], request_key=request_key)
        referral = self.repository.get(REFERRAL_TYPE, offer["referral_id"])
        self.repository.update(REFERRAL_TYPE, referral["entity_id"], {"state": "hired", "current_owner": offer["job_id"]},
                               actor=context.actor_id, expected_version=referral["version"],
                               request_key=request_key + ":stage")
        return updated

    def withdraw_offer(self, context: AccessContext, offer_id: str, *, reason: str, request_key: str) -> dict:
        """撤回已发出的承诺：必须留下撤回记录，不能静默覆盖。"""
        context.require("withdraw:offers")
        if not reason.strip():
            raise ValidationError("撤回承诺必须说明原因")
        offer = self.repository.get(OFFER_TYPE, offer_id)
        if offer["state"] not in ("offered", "confirmed"):
            raise ConflictError(f"承诺状态为 {offer['state']}，不能撤回")
        withdrawal = {
            "withdrawn_by": context.actor_id,
            "reason": reason.strip(),
            "withdrawn_at": self._now(),
            "prior_terms_digest": digest_json(offer["terms"]),
        }
        updated = self.repository.update(OFFER_TYPE, offer_id, {"state": "withdrawn", "withdrawal": withdrawal},
                                         actor=context.actor_id, expected_version=offer["version"], request_key=request_key)
        self.outbox.enqueue(topic="offer.withdrawn", aggregate_id=offer_id, payload={"offer_id": offer_id, "reason": reason.strip()})
        return updated

    def propose_amendment(self, context: AccessContext, offer_id: str, values: Mapping[str, object], *, request_key: str) -> dict:
        """补充协议：承诺变化只能以补充协议衔接，原条款保留可追溯。"""
        context.require("write:offer_amendments")
        offer = self.repository.get(OFFER_TYPE, offer_id)
        if offer["state"] not in ("offered", "confirmed"):
            raise ConflictError("只有生效中的承诺可以提出补充协议")
        changes = values.get("changes")
        if not isinstance(changes, Mapping) or not changes:
            raise ValidationError("补充协议必须包含条款变化")
        payload = {
            "offer_id": offer_id,
            "changes": dict(changes),
            "proposed_by": context.actor_id,
            "reason": _clean_str(values.get("reason"), "补充原因"),
            "valid_until": canonical_instant(_clean_str(values.get("valid_until"), "补充协议期限")),
            "decided_by": None,
            "state": "proposed",
        }
        return self.repository.create(AMENDMENT_TYPE, payload, actor=context.actor_id, request_key=request_key)

    def decide_amendment(self, context: AccessContext, amendment_id: str, *, accept: bool, request_key: str) -> dict:
        """候选人一方接受或拒绝补充协议；企业不能替候选人接受。"""
        context.require("decide:offer_amendments")
        amendment = self.repository.get(AMENDMENT_TYPE, amendment_id)
        if amendment["state"] != "proposed":
            raise ConflictError("补充协议已处理")
        assert_distinct(amendment["proposed_by"], context.actor_id)
        if accept:
            offer = self.repository.get(OFFER_TYPE, amendment["offer_id"])
            if offer["state"] not in ("offered", "confirmed"):
                raise ConflictError("原承诺已不在生效状态")
            accepted = list(offer.get("accepted_amendment_ids", [])) + [amendment_id]
            self.repository.update(OFFER_TYPE, offer["entity_id"], {"accepted_amendment_ids": accepted},
                                   actor=context.actor_id, expected_version=offer["version"],
                                   request_key=request_key + ":link")
        return self.repository.update(AMENDMENT_TYPE, amendment_id,
                                      {"state": "accepted" if accept else "rejected", "decided_by": context.actor_id, "decided_at": self._now()},
                                      actor=context.actor_id, expected_version=amendment["version"], request_key=request_key)

    def get_offer(self, context: AccessContext, offer_id: str) -> dict:
        context.require("read:offers")
        return self.repository.get(OFFER_TYPE, offer_id)

    # ------------------------------------------------------------------ #
    # 持久化定时任务：重启后继续处理证件到期、面试协调、承诺确认
    # ------------------------------------------------------------------ #

    def run_due_jobs(self, context: AccessContext, *, limit: int = 20) -> list[dict]:
        context.require("recruiting:operate")
        results: list[dict] = []
        for job in self.jobs.claim_due(limit=limit, job_types=(JOB_CREDENTIAL_EXPIRY, JOB_INTERVIEW_REMINDER, JOB_OFFER_DEADLINE)):
            outcome = self._dispatch_job(context, job)
            self.jobs.finish(job["job_id"])
            results.append({"job_id": job["job_id"], "job_type": job["job_type"], **outcome})
        return results

    def _dispatch_job(self, context: AccessContext, job: Mapping[str, object]) -> dict:
        import json as _json
        payload = _json.loads(job["payload_json"])
        if job["job_type"] == JOB_CREDENTIAL_EXPIRY:
            referral_id = payload["referral_id"]
            referral = self.repository.get(REFERRAL_TYPE, referral_id)
            if referral["state"] in STATES_WITH_OPEN_STEPS and self._frozen_credential_expired(referral, payload.get("credential_id")):
                self._cancel_unfinished(referral, reason="credential_expired", actor="system")
                return {"action": "cancelled", "referral_id": referral_id}
            return {"action": "noop", "referral_id": referral_id}
        if job["job_type"] == JOB_INTERVIEW_REMINDER:
            interview_id = payload["interview_id"]
            interview = self.repository.get(INTERVIEW_TYPE, interview_id)
            if interview["state"] == "scheduled" and not interview.get("reminded_at"):
                # 先发件箱再落标记：崩溃最坏导致重复提醒，不会漏发
                self.outbox.enqueue(topic="interview.reminder", aggregate_id=interview_id,
                                    payload={"interview_id": interview_id, "scheduled_at": interview["scheduled_at"]})
                self.repository.update(INTERVIEW_TYPE, interview_id, {"reminded_at": self._now()},
                                       actor="system", expected_version=interview["version"],
                                       request_key=f"interview-reminded:{interview_id}")
                return {"action": "reminded", "interview_id": interview_id}
            return {"action": "noop", "interview_id": interview_id}
        if job["job_type"] == JOB_OFFER_DEADLINE:
            offer_id = payload["offer_id"]
            offer = self.repository.get(OFFER_TYPE, offer_id)
            if offer["state"] == "offered":
                self.repository.update(OFFER_TYPE, offer_id, {"state": "expired", "expired_at": self._now()},
                                       actor="system", expected_version=offer["version"],
                                       request_key=f"offer-deadline:{offer_id}")
                self.outbox.enqueue(topic="offer.expired", aggregate_id=offer_id, payload={"offer_id": offer_id})
                return {"action": "expired", "offer_id": offer_id}
            return {"action": "noop", "offer_id": offer_id}
        return {"action": "unknown"}

    # ------------------------------------------------------------------ #
    # 可追溯解释：为何推荐、满足哪版要求、谁批的例外、下一步谁处理
    # ------------------------------------------------------------------ #

    def explain_referral(self, context: AccessContext, referral_id: str) -> dict:
        context.require("read:referrals")
        referral = self.repository.get(REFERRAL_TYPE, referral_id)
        job_versions = self.repository.history(JOB_TYPE, referral["job_id"])
        frozen_job = next((v for v in job_versions if v["version"] == referral["job_version"]), None)
        exception = None
        exceptions = [
            {
                "exception_id": row["entity_id"],
                "requirement": row["requirement"],
                "state": row["state"],
                "requested_by": row["requested_by"],
                "reviewer": row.get("reviewer"),
                "decision_note": row.get("decision_note"),
                "decided_at": row.get("decided_at"),
            }
            for row in self.repository.list(EXCEPTION_TYPE, limit=500)
            if row["referral_id"] == referral_id
        ]
        if exceptions:
            exception = next((item for item in exceptions if item["state"] == "approved"), exceptions[0])
        interviews = [
            {
                "interview_id": row["entity_id"],
                "round": row["round"],
                "state": row["state"],
                "job_version": row["job_version"],
                "scheduled_at": row["scheduled_at"],
                "scheduled_by": row["scheduled_by"],
            }
            for row in self.repository.list(INTERVIEW_TYPE, limit=500)
            if row["referral_id"] == referral_id
        ]
        interviews.sort(key=lambda item: item["round"])
        offer = None
        offers = [row for row in self.repository.list(OFFER_TYPE, limit=500) if row["referral_id"] == referral_id]
        if offers:
            row = offers[0]
            amendments = [
                self.repository.get(AMENDMENT_TYPE, amendment_id)
                for amendment_id in row.get("accepted_amendment_ids", [])
            ]
            offer = {
                "offer_id": row["entity_id"],
                "state": row["state"],
                "job_version": row["job_version"],
                "committed_by": row["committed_by"],
                "committed_at": row["committed_at"],
                "valid_until": row["valid_until"],
                "terms": row["terms"],
                "withdrawal": row.get("withdrawal"),
                "amendments": [{"amendment_id": a["entity_id"], "changes": a["changes"], "decided_by": a.get("decided_by")} for a in amendments],
            }
        return {
            "referral_id": referral_id,
            "candidate_id": referral["candidate_id"],
            "recommended_by": referral["created_by"],
            "frozen_at": referral["frozen_at"],
            "why": {
                "job_id": referral["job_id"],
                "job_version": referral["job_version"],
                "requirements_digest": referral["requirements_digest"],
                "frozen_requirements": referral["job_snapshot"],
                "job_version_published_at": frozen_job["valid_from"] if frozen_job else None,
                "eligibility": referral["eligibility"],
                "exception": exception,
                "exceptions": exceptions,
            },
            "credential_freeze": referral["credential_freeze"],
            "consent_freeze": referral["consent_freeze"],
            "current_state": referral["state"],
            "cancel_reason": referral.get("cancel_reason"),
            "next_owner": referral.get("current_owner"),
            "interviews": interviews,
            "offer": offer,
            "timeline": [
                {"version": v["version"], "state": v["state"], "at": v["valid_from"], "actor": v["actor_id"], "request_key": v["request_key"]}
                for v in self.repository.history(REFERRAL_TYPE, referral_id)
            ],
        }

    def list_referrals(self, context: AccessContext, *, state: str | None = None) -> list[dict]:
        context.require("read:referrals")
        if state is not None and state not in REFERRAL_STATES:
            raise ValidationError("未知推荐状态")
        return self.repository.list(REFERRAL_TYPE, state=state)

    # ------------------------------------------------------------------ #
    # 内部辅助
    # ------------------------------------------------------------------ #

    def _now(self) -> str:
        return self.repository.clock.now()

    @staticmethod
    def _requirements_digest(job: Mapping[str, object]) -> str:
        return digest_json({
            "skills": job["skills"],
            "languages": job["languages"],
            "work_permit": job["work_permit"],
        })

    def _validate_job(self, values: Mapping[str, object], *, partial: bool) -> dict:
        required = ("employer_org", "title", "skills", "languages", "work_permit", "salary_band", "budget")
        if not partial:
            missing = [field for field in required if field not in values]
            if missing:
                raise ValidationError("缺少字段: " + ", ".join(missing))
        payload: dict = {}
        if "employer_org" in values:
            payload["employer_org"] = _clean_str(values["employer_org"], "用人企业")
        if "title" in values:
            payload["title"] = _clean_str(values["title"], "岗位名称")
        if "skills" in values:
            skills = values["skills"]
            if not isinstance(skills, list) or not skills or not all(isinstance(s, str) and s.strip() for s in skills):
                raise ValidationError("技能要求必须是非空字符串列表")
            payload["skills"] = sorted(s.strip() for s in skills)
        if "languages" in values:
            languages = values["languages"]
            if not isinstance(languages, list) or not languages:
                raise ValidationError("语言要求必须是非空列表")
            normalized = []
            for item in languages:
                if not isinstance(item, Mapping) or "code" not in item or "min_level" not in item:
                    raise ValidationError("语言要求需包含 code 和 min_level")
                code = _clean_str(item["code"], "语言代码").lower()
                level = _clean_str(item["min_level"], "语言等级").lower()
                if level not in LANGUAGE_LEVELS:
                    raise ValidationError("语言等级必须是 a1..c2")
                normalized.append({"code": code, "min_level": level})
            payload["languages"] = sorted(normalized, key=lambda item: item["code"])
        if "work_permit" in values:
            permit = values["work_permit"]
            if not isinstance(permit, Mapping) or "required_types" not in permit:
                raise ValidationError("签证/工作许可要求需包含 required_types")
            required_types = permit["required_types"]
            if not isinstance(required_types, list) or not all(isinstance(t, str) and t.strip() for t in required_types):
                raise ValidationError("required_types 必须是非空字符串列表")
            payload["work_permit"] = {"required_types": sorted(t.strip() for t in required_types),
                                      "jurisdiction": str(permit.get("jurisdiction", "CN")).strip()}
        if "salary_band" in values:
            payload["salary_band"] = self._validate_money(values["salary_band"], "薪酬区间")
        if "budget" in values:
            budget = values["budget"]
            if not isinstance(budget, Mapping):
                raise ValidationError("预算格式不合法")
            payload["budget"] = {**self._validate_money(budget, "预算"), "openings": self._validate_openings(budget.get("openings"))}
        unknown = set(values) - set(required) - {"state", "transition_reason"}
        if unknown:
            raise ValidationError("未知字段: " + ", ".join(sorted(unknown)))
        return payload

    @staticmethod
    def _validate_money(value: object, label: str) -> dict:
        if not isinstance(value, Mapping):
            raise ValidationError(f"{label}格式不合法")
        amount = value.get("amount")
        currency = value.get("currency", "CNY")
        if amount is None:
            raise ValidationError(f"{label}缺少 amount")
        try:
            amount_int = int(amount)
        except (TypeError, ValueError):
            raise ValidationError(f"{label}金额必须是整数（最小货币单位）")
        if amount_int < 0:
            raise ValidationError(f"{label}金额不能为负")
        if not isinstance(currency, str) or not currency.strip():
            raise ValidationError(f"{label}币种不合法")
        result = {"amount": amount_int, "currency": currency.strip()}
        if "min" in value or "max" in value:
            band: dict = {}
            for edge in ("min", "max"):
                if edge in value:
                    try:
                        band[edge] = int(value[edge])
                    except (TypeError, ValueError):
                        raise ValidationError(f"{label}{edge} 必须是整数")
            if "min" in band and "max" in band and band["min"] > band["max"]:
                raise ValidationError(f"{label}下限不能高于上限")
            result.update(band)
        return result

    @staticmethod
    def _validate_openings(value: object) -> int:
        if not isinstance(value, int) or value < 1:
            raise ValidationError("招聘人数必须是正整数")
        return value

    @staticmethod
    def _budget_changed(before: Mapping[str, object], after: Mapping[str, object]) -> bool:
        return before.get("budget") != after.get("budget") or before.get("salary_band") != after.get("salary_band")

    def _require_active_consent(self, candidate_id: str) -> dict:
        consents = [row for row in self.repository.list(CONSENT_TYPE, state="granted", limit=500)
                    if row["candidate_id"] == candidate_id]
        now = parse_instant(self._now())
        valid = [row for row in consents if parse_instant(row["valid_until"]) > now]
        if not valid:
            raise ConflictError("候选人没有有效授权，不能继续推荐")
        return valid[0]

    def _freeze_credentials(self, credential_ids: object) -> list[dict]:
        if not isinstance(credential_ids, list):
            raise ValidationError("credential_ids 必须是列表")
        freeze: list[dict] = []
        for credential_id in dict.fromkeys(credential_ids):
            row = self.repository.get("credentials", str(credential_id))
            freeze.append({
                "credential_id": row["entity_id"],
                "version": row["version"],
                "state": row["state"],
                "credential_type": row["credential_type"],
                "issuer": row.get("issuer"),
                "valid_from": row.get("valid_from"),
                "valid_to": row.get("valid_to"),
                "digest": digest_json(row),
            })
        return freeze

    def _refreeze_credentials(self, prior_freeze: list[dict]) -> list[dict]:
        """安排面试时读取证件当前版本，同时保留推荐时版本对照。"""
        refreshed = []
        for frozen in prior_freeze:
            row = self.repository.get("credentials", frozen["credential_id"])
            refreshed.append({
                "referral_version": frozen["version"],
                "credential_id": row["entity_id"],
                "version": row["version"],
                "state": row["state"],
                "credential_type": row["credential_type"],
                "valid_to": row.get("valid_to"),
                "digest": digest_json(row),
            })
        return refreshed

    def _evaluate(self, job: Mapping[str, object], credentials: list[dict]) -> dict:
        missing: list[dict] = []
        have_skills: set[str] = set()
        language_levels: dict[str, int] = {}
        permit_types: set[str] = set()
        now = parse_instant(self._now())
        for credential in credentials:
            # 任何已过期/撤销/草稿证件都不计入资格
            if credential["state"] not in ("effective", "verified"):
                continue
            if credential.get("valid_to") and parse_instant(credential["valid_to"]) <= now:
                continue
            ctype = credential["credential_type"]
            if ctype.startswith("skill:"):
                have_skills.add(ctype.split(":", 1)[1])
            elif ctype.startswith("lang:"):
                code = ctype.split(":", 1)[1].split("/")[0].lower()
                level = ctype.split("/")[1].lower() if "/" in ctype else "a1"
                language_levels[code] = max(language_levels.get(code, 0), LANGUAGE_LEVELS.get(level, 0))
            else:
                permit_types.add(ctype)
        for skill in job["skills"]:
            if skill not in have_skills:
                missing.append({"requirement": f"skill:{skill}", "kind": "skill", "expected": skill})
        for language in job["languages"]:
            required_level = LANGUAGE_LEVELS[language["min_level"]]
            if language_levels.get(language["code"], 0) < required_level:
                missing.append({"requirement": f"lang:{language['code']}/{language['min_level']}",
                                "kind": "language", "expected": language})
        for permit in job["work_permit"]["required_types"]:
            if permit not in permit_types:
                missing.append({"requirement": f"permit:{permit}", "kind": "work_permit", "expected": permit})
        return {"eligible": not missing, "missing": missing, "evaluated_at": self._now()}

    def _require_requirements_resolved(self, referral: Mapping[str, object]) -> None:
        missing = referral["eligibility"].get("missing", [])
        if not missing:
            return
        # 每一项未满足资格都必须有独立的、已批准的例外
        exceptions = [row for row in self.repository.list(EXCEPTION_TYPE, limit=500)
                      if row["referral_id"] == referral["entity_id"]]
        approved = {row["requirement"]: row for row in exceptions if row["state"] == "approved"}
        unresolved = [item["requirement"] for item in missing if item["requirement"] not in approved]
        if unresolved:
            raise ConflictError("资格未满足且例外未全部批准: " + ", ".join(unresolved))

    def _frozen_credential_expired(self, referral: Mapping[str, object], credential_id: str | None) -> bool:
        now = parse_instant(self._now())
        for frozen in referral["credential_freeze"]:
            if credential_id and frozen["credential_id"] != credential_id:
                continue
            row = self.repository.get("credentials", frozen["credential_id"])
            if row["state"] == "expired" or (row.get("valid_to") and parse_instant(row["valid_to"]) <= now):
                return True
        return False

    def _is_member_of(self, actor_id: str, organization_id: str) -> bool:
        for membership in self.repository.search("memberships", "person_ref", actor_id, limit=100):
            if membership["organization_id"] == organization_id and membership["state"] == "active":
                return True
        return False

    def _require_org_maintainer(self, context: AccessContext, organization_id: str) -> None:
        """企业只能维护本方岗位；人才服务中心系统角色可代管。"""
        if context.reveal_sensitive:
            return
        if not self._is_member_of(context.actor_id, organization_id):
            raise PermissionDenied("企业只能维护本企业的岗位")

    def _cancel_unfinished_for_job(self, job_id: str, *, reason: str, actor: str) -> None:
        for referral in self.repository.list(REFERRAL_TYPE, limit=500):
            if referral["job_id"] == job_id and referral["state"] in STATES_WITH_OPEN_STEPS:
                self._cancel_unfinished(referral, reason=reason, actor=actor)

    def _cancel_unfinished_for_candidate(self, candidate_id: str, *, reason: str, actor: str) -> None:
        for referral in self.repository.list(REFERRAL_TYPE, limit=500):
            if referral["candidate_id"] == candidate_id and referral["state"] in STATES_WITH_OPEN_STEPS:
                self._cancel_unfinished(referral, reason=reason, actor=actor)

    def _cancel_unfinished(self, referral: Mapping[str, object], *, reason: str, actor: str) -> None:
        """终止未完成环节：取消未举行的面试和推荐；已发出的承诺一律不动。"""
        for interview in self.repository.list(INTERVIEW_TYPE, state="scheduled", limit=500):
            if interview["referral_id"] == referral["entity_id"]:
                self.repository.update(INTERVIEW_TYPE, interview["entity_id"],
                                       {"state": "cancelled", "cancel_reason": reason},
                                       actor=actor, expected_version=interview["version"],
                                       request_key=f"cancel:{interview['entity_id']}:{reason}")
        for exception in self.repository.list(EXCEPTION_TYPE, state="pending", limit=500):
            if exception["referral_id"] == referral["entity_id"]:
                self.repository.update(EXCEPTION_TYPE, exception["entity_id"], {"state": "cancelled"},
                                       actor=actor, expected_version=exception["version"],
                                       request_key=f"cancel:{exception['entity_id']}:{reason}")
        current = self.repository.get(REFERRAL_TYPE, referral["entity_id"])
        if current["state"] in UNFINISHED_REFERRAL_STATES:
            self.repository.update(REFERRAL_TYPE, current["entity_id"],
                                   {"state": "cancelled", "cancel_reason": reason, "current_owner": None},
                                   actor=actor, expected_version=current["version"],
                                   request_key=f"cancel:{current['entity_id']}:{reason}")
        elif current["state"] == "offered":
            # 承诺阶段只终止协调环节，承诺保留；记录终止原因但下一步仍等候选人。
            # 多个到期任务可能重复触发，只在原因变化时追加记录，保证幂等。
            if current.get("cancel_reason") != reason:
                self.repository.update(REFERRAL_TYPE, current["entity_id"],
                                       {"cancel_reason": reason},
                                       actor=actor, expected_version=current["version"],
                                       request_key=f"note:{current['entity_id']}:{reason}")

    def _link_receipt(self, receipt_key: str, referral_id: str) -> None:
        with self.repository.database.transaction() as connection:
            connection.execute("UPDATE resume_receipts SET referral_id=? WHERE receipt_key=?", (referral_id, receipt_key))

    def _referral_brief(self, referral_id: str | None) -> dict | None:
        if not referral_id:
            return None
        row = self.repository.get(REFERRAL_TYPE, referral_id)
        return {"referral_id": row["entity_id"], "state": row["state"], "current_owner": row.get("current_owner")}
