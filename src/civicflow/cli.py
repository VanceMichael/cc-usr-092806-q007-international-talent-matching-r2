"""命令行入口。"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .application import CivicFlow
from .cases import CaseService
from .credentials import CredentialService
from .memberships import MembershipService
from .organizations import OrganizationService
from .security import AccessContext


def emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def demo(app: CivicFlow) -> dict:
    context = AccessContext.system("demo-operator")
    cases = CaseService(app.repository)
    created = cases.create(context, {"case_type": "协同事项", "subject": "示例联合处置", "owner_org": "org:demo", "priority": "high", "opened_at": app.clock.now()}, request_key="demo-case")
    accepted = app.inbox.receive(source="demo", source_key=created["entity_id"], sequence=1, payload={"kind": "opened"}, occurred_at=app.clock.now())
    reservation = app.reservations.reserve(resource_id="room:joint", subject_id=created["entity_id"], quantity=2, capacity=10, start_at="2026-09-28T10:00:00+08:00", end_at="2026-09-28T11:00:00+08:00", actor=context.actor_id)
    debit = app.ledger.post(journal_key="demo", account="coordination", currency="CNY", amount="12.34", direction="debit", reference="demo-debit", actor=context.actor_id)
    credit = app.ledger.post(journal_key="demo", account="coordination", currency="CNY", amount="12.34", direction="credit", reference="demo-credit", actor=context.actor_id)
    message = app.outbox.enqueue(topic="case.opened", aggregate_id=created["entity_id"], payload={"case_id": created["entity_id"]})
    return {"case": created, "inbox": accepted, "reservation": reservation, "entries": [debit, credit], "balance": app.ledger.balance("demo", currency="CNY"), "message_id": message, "verification": app.verify()}


def recruitment_demo(app: CivicFlow) -> dict:
    """人才大会招聘投诉整改后的端到端演示。"""
    recruiter = AccessContext(
        actor_id="recruiter@acme",
        permissions=frozenset({
            "write:rec_job", "write:rec_application", "write:rec_exception",
            "write:rec_interview", "write:rec_offer", "read:rec_job",
            "read:rec_application", "contact:recruitment",
        }),
        scopes=frozenset(),
    )
    center = AccessContext(
        actor_id="caseworker@center",
        permissions=frozenset({
            "write:rec_application", "approve:rec_exception", "read:rec_job",
            "read:rec_application", "read:rec_interview", "read:rec_offer",
            "history:rec_application", "contact:recruitment",
            "write:organizations", "write:memberships", "transition:memberships",
            "write:credentials", "transition:credentials",
        }),
        scopes=frozenset({"*"}),
    )
    rec = app.recruitment

    org = OrganizationService(app.repository).create(center, {"name": "杭州Acme科技", "kind": "employer", "jurisdiction": "CN", "owner_id": "recruiter@acme"}, request_key="org-acme")
    membership = MembershipService(app.repository).create(center, {"organization_id": org["entity_id"], "person_ref": "recruiter@acme", "role": "recruiter", "valid_from": app.clock.now(), "valid_to": "2027-12-31T23:59:59+08:00"}, request_key="mem-acme")
    MembershipService(app.repository).transition(center, membership["entity_id"], "active", expected_version=1, reason="招聘资质生效", request_key="mem-acme-active")

    job = rec.create_job(recruiter, {
        "employer_org": org["entity_id"],
        "title": "高级算法工程师",
        "skills": [{"name": "Python", "level": 4}],
        "languages": [{"language": "英语", "level": 3}],
        "visa": {"required": True, "permit_type": "work_permit_z"},
        "budget": {"currency": "CNY", "min": 30000000, "max": 60000000},
    }, request_key="job-acme-1")
    job = rec.publish_job(recruiter, job["entity_id"], expected_version=1, request_key="job-pub-1")

    permit = CredentialService(app.repository).create(center, {"holder_type": "person", "holder_id": "candidate:anna", "credential_type": "work_permit_z", "issuer": "外专局", "valid_from": app.clock.now(), "valid_to": "2027-09-30T23:59:59+08:00"}, request_key="permit-anna")
    for target in ("verified", "effective"):
        permit = CredentialService(app.repository).transition(center, permit["entity_id"], target, expected_version=permit["version"], reason="签证材料核验", request_key=f"permit-{target}")

    resume = {
        "job_id": job["entity_id"], "name": "Anna", "contact": "anna@example.com",
        "skills": [{"name": "Python", "level": 4}],
        "languages": [{"language": "英语", "level": 2}],
        "credential_ids": [permit["entity_id"]],
        "planned_rounds": 2,
    }
    receipt = rec.receive_resume(center, person_key="candidate:anna", resume=resume, request_key="resume-anna-1")
    duplicate = rec.receive_resume(center, person_key="candidate:anna", resume=resume, request_key="resume-anna-1")
    application_id = receipt["application_id"]

    app_row = rec.get_application(center, application_id)
    app_row = rec.grant_consent(center, application_id, scope=f"rec_app:{application_id}", valid_to="2026-12-31T23:59:59+08:00", expected_version=app_row["version"], request_key="consent-anna")

    # 英语只达到 2 级（岗位要求 3 级）：企业招聘人员申请例外，但不能自己批准。
    exception = rec.request_exception(recruiter, application_id, requirement_code="lang:英语", reason="现场技术沟通能力突出，语言可入职培训", request_key="exc-lang")
    exception = rec.decide_exception(center, exception["entity_id"], decision="approved", comment="人才大会专项政策，同意语言例外", expected_version=1, request_key="exc-lang-ok")

    referred = rec.refer(center, application_id, expected_version=app_row["version"], request_key="refer-anna")
    frozen_job_version = referred["frozen"]["job_version"]

    # 企业随后修改岗位条件形成新版本：已冻结的推荐与面试不受影响。
    job_v3 = rec.revise_job(recruiter, job["entity_id"], {"languages": [{"language": "英语", "level": 4}]}, expected_version=2, request_key="job-v3")

    interview1 = rec.schedule_interview(recruiter, application_id, round_no=1, scheduled_at="2026-10-05T10:00:00+08:00", panel=["recruiter@acme"], request_key="iv-1")
    interview1 = rec.complete_interview(recruiter, interview1["entity_id"], expected_version=1, result="通过", request_key="iv-1-done")
    interview2 = rec.schedule_interview(recruiter, application_id, round_no=2, scheduled_at="2026-10-08T14:00:00+08:00", panel=["recruiter@acme", "cto@acme"], request_key="iv-2")
    interview2 = rec.complete_interview(recruiter, interview2["entity_id"], expected_version=1, result="通过", request_key="iv-2-done")

    offer = rec.issue_offer(recruiter, application_id, compensation={"currency": "CNY", "salary_minor": 42000000}, promise_valid_until="2026-10-15T18:00:00+08:00", request_key="offer-1")
    amendment = rec.supplement_offer(recruiter, application_id, compensation={"currency": "CNY", "salary_minor": 44000000}, promise_valid_until="2026-10-20T18:00:00+08:00", reason="签字奖金以补充协议明确", request_key="offer-2")

    # 未获授权的招聘人员看不到联系方式。
    outsider = AccessContext(actor_id="recruiter2@other", permissions=frozenset({"read:rec_application"}))
    redacted = rec.get_application(outsider, application_id)

    explanation = rec.explain(center, application_id)
    todos = rec.open_todos(center)
    return {
        "org": org["entity_id"], "job_versions": [job["version"], job_v3["version"]],
        "receipt": receipt, "duplicate_receipt": duplicate,
        "exception_reviewer": exception["reviewer"],
        "frozen_job_version_at_referral": frozen_job_version,
        "interviews": [interview1["entity_id"], interview2["entity_id"]],
        "original_offer": offer["entity_id"], "amended_offer": amendment["entity_id"],
        "contact_visible_to_center": rec.get_application(center, application_id)["contact"],
        "contact_visible_to_outsider": redacted["contact"],
        "open_todos": todos,
        "next": {"owner": explanation["next_owner"], "action": explanation["next_action"]},
        "verification": app.verify(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="协同事务平台")
    parser.add_argument("--db", default=os.getenv("CIVICFLOW_DB", "civicflow.sqlite3"))
    parser.add_argument("--now", default=None, help="测试或演示使用的固定时间")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("demo")
    commands.add_parser("verify")
    commands.add_parser("list-cases")
    commands.add_parser("rec-demo")
    commands.add_parser("rec-todos")
    explain = commands.add_parser("rec-explain")
    explain.add_argument("application_id")
    args = parser.parse_args(argv)
    app = CivicFlow.open(Path(args.db), fixed_now=args.now)
    if args.command == "demo":
        emit(demo(app))
    elif args.command == "verify":
        emit(app.verify())
    elif args.command == "list-cases":
        emit(CaseService(app.repository).list_current(AccessContext.system("cli")))
    elif args.command == "rec-demo":
        emit(recruitment_demo(app))
    elif args.command == "rec-todos":
        emit(app.recruitment.open_todos(AccessContext.system("cli")))
    elif args.command == "rec-explain":
        emit(app.recruitment.explain(AccessContext.system("cli"), args.application_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
