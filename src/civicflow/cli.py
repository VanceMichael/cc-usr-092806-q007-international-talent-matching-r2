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
from .recruiting import RecruitingService
from .security import AccessContext


def emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def recruiting_demo(app: CivicFlow) -> dict:
    operator = AccessContext.system("talent-center")
    org_service = OrganizationService(app.repository)
    memberships = MembershipService(app.repository)
    credentials = CredentialService(app.repository)
    recruiting = RecruitingService(app.repository, app.jobs, app.outbox)

    employer = org_service.create(operator, {"name": "杭州数智科技", "kind": "enterprise", "jurisdiction": "CN", "owner_id": "person:hr-lead"}, request_key="org-1")
    recruiter = AccessContext(actor_id="person:recruiter-a", permissions=frozenset({
        "write:recruiting_jobs", "transition:recruiting_jobs", "read:recruiting_jobs",
        "history:recruiting_jobs", "write:candidates", "write:candidate_consents",
        "write:referrals", "read:referrals", "write:interviews", "write:offers",
        "write:eligibility_exceptions", "read:offers",
    }))
    membership = memberships.create(operator, {"organization_id": employer["entity_id"], "person_ref": "person:recruiter-a", "role": "recruiter", "valid_from": app.clock.now(), "valid_to": "2027-12-31T00:00:00Z"}, request_key="m-1")
    memberships.transition(operator, membership["entity_id"], "active", expected_version=1, reason="入职", request_key="m-1:active")

    job = recruiting.create_job(recruiter, {
        "employer_org": employer["entity_id"], "title": "高级算法工程师",
        "skills": ["python", "ml"],
        "languages": [{"code": "en", "min_level": "b2"}],
        "work_permit": {"required_types": ["work-permit-z"], "jurisdiction": "CN"},
        "salary_band": {"amount": 600000, "currency": "CNY", "min": 400000, "max": 800000},
        "budget": {"amount": 1200000, "currency": "CNY", "openings": 2},
    }, request_key="job-1")
    recruiting.transition_job(recruiter, job["entity_id"], "open", expected_version=1, reason="大会后开放", request_key="job-open")

    candidate = recruiting.register_candidate(recruiter, {"full_name": "李明", "contact": "liming@example.com"}, request_key="cand-1")
    consent = recruiting.grant_consent(recruiter, candidate["entity_id"], {"audience": ["person:recruiter-a"], "scopes": ["contact", "referral"], "valid_until": "2027-06-30T00:00:00Z", "granted_by": candidate["entity_id"]}, request_key="consent-1")

    def make_credential(credential_type: str, valid_to: str, key: str) -> str:
        row = credentials.create(operator, {"holder_type": "candidate", "holder_id": candidate["entity_id"], "credential_type": credential_type, "issuer": "issuer:gov", "valid_from": app.clock.now(), "valid_to": valid_to}, request_key=key)
        row = credentials.transition(operator, row["entity_id"], "verified", expected_version=1, reason="核验", request_key=key + ":v")
        row = credentials.transition(operator, row["entity_id"], "effective", expected_version=2, reason="生效", request_key=key + ":e")
        return row["entity_id"]

    skill_python = make_credential("skill:python", "2027-01-01T00:00:00Z", "cred-py")
    skill_ml = make_credential("skill:ml", "2027-01-01T00:00:00Z", "cred-ml")
    lang_en = make_credential("lang:en/c1", "2027-01-01T00:00:00Z", "cred-en")
    permit = make_credential("work-permit-z", "2027-12-31T00:00:00Z", "cred-permit")

    resume = recruiting.receive_resume(recruiter, receipt_key="receipt-001", candidate_id=candidate["entity_id"], materials={"resume": "v1", "skills": ["python", "ml"]}, request_key="recv-1")
    referral = recruiting.recommend(recruiter, {"job_id": job["entity_id"], "candidate_id": candidate["entity_id"], "credential_ids": [skill_python, skill_ml, lang_en, permit], "receipt_key": resume["receipt_key"]}, request_key="ref-1")
    interview = recruiting.schedule_interview(recruiter, {"referral_id": referral["entity_id"], "round": 1, "scheduled_at": "2026-10-05T10:00:00+08:00", "panel": ["person:interviewer-1"], "location": "线上"}, request_key="int-1")
    recruiting.complete_interview(recruiter, interview["entity_id"], result="通过", request_key="int-1-done")
    offer = recruiting.issue_offer(recruiter, {"referral_id": referral["entity_id"], "terms": {"compensation": 650000, "currency": "CNY", "position": "高级算法工程师"}, "valid_until": "2026-10-20T00:00:00+08:00"}, request_key="offer-1")
    with app.database.connect() as connection:
        waiting_jobs = connection.execute("SELECT COUNT(*) AS n FROM scheduled_jobs WHERE status='waiting'").fetchone()["n"]
    return {"employer": employer["entity_id"], "job_version": referral["job_version"], "referral": referral["entity_id"], "interview": interview["entity_id"], "offer": offer["entity_id"], "consent": consent["entity_id"], "waiting_jobs": waiting_jobs}


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="协同事务平台")
    parser.add_argument("--db", default=os.getenv("CIVICFLOW_DB", "civicflow.sqlite3"))
    parser.add_argument("--now", default=None, help="测试或演示使用的固定时间")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("demo")
    commands.add_parser("recruiting-demo")
    commands.add_parser("verify")
    commands.add_parser("list-cases")
    explain = commands.add_parser("explain-referral")
    explain.add_argument("referral_id")
    args = parser.parse_args(argv)
    app = CivicFlow.open(Path(args.db), fixed_now=args.now)
    if args.command == "demo": emit(demo(app))
    elif args.command == "recruiting-demo": emit(recruiting_demo(app))
    elif args.command == "verify": emit(app.verify())
    elif args.command == "list-cases": emit(CaseService(app.repository).list_current(AccessContext.system("cli")))
    elif args.command == "explain-referral":
        emit(RecruitingService(app.repository, app.jobs, app.outbox).explain_referral(AccessContext.system("cli"), args.referral_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
