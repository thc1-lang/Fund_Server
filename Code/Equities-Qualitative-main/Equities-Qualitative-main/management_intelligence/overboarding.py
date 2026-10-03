"""Factual current outside public-company board counts."""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from .board_governance_models import GovernanceBoardMembership, OverboardingRecord
from .models import BoardMembership, ManagementPerson, RoleAssertion


def _current(row: BoardMembership, as_of_date: str) -> bool:
    return bool(row.is_current and (not row.start_date or len(row.start_date) != 10 or row.start_date <= as_of_date) and (not row.end_date or row.end_date > as_of_date))


def build_overboarding(
    ticker: str,
    memberships: Iterable[GovernanceBoardMembership],
    all_boards: Iterable[BoardMembership],
    people: Iterable[ManagementPerson],
    roles: Iterable[RoleAssertion],
    *,
    as_of_date: str,
) -> list[OverboardingRecord]:
    ticker = ticker.upper()
    people_by_id = {person.person_id: person for person in people}
    current_by_person: dict[str, list[BoardMembership]] = defaultdict(list)
    unknown_by_person: defaultdict[str, int] = defaultdict(int)
    for board in all_boards:
        if not _current(board, as_of_date):
            continue
        if board.board_classification == "PUBLIC_COMPANY_CONFIRMED":
            if (board.board_ticker or board.ticker).upper() != ticker:
                current_by_person[board.person_id].append(board)
        elif board.person_id in people_by_id:
            unknown_by_person[board.person_id] += 1
    roles_by_person: dict[str, list[str]] = defaultdict(list)
    for role in roles:
        if role.is_current:
            roles_by_person[role.person_id].append(role.role)
    result: list[OverboardingRecord] = []
    for member in memberships:
        boards = current_by_person.get(member.person_id, [])
        roles_for_person = roles_by_person.get(member.person_id, [])
        ceo = next((role for role in roles_for_person if "chief executive" in role.lower()), None)
        board_roles = [{"ticker": (board.board_ticker or board.ticker).upper(), "company": board.board_company, "role": board.board_role, "person_id": board.person_id, "source_url": board.source_url} for board in boards]
        evidence_known = bool(boards) or unknown_by_person.get(member.person_id, 0) > 0
        result.append(OverboardingRecord(
            overboarding_id=f"governance-overboarding:{ticker}:{member.person_id}", ticker=ticker,
            person_id=member.person_id, current_public_company_board_count=len(boards) if evidence_known else None,
            current_public_board_roles=board_roles, current_executive_role=ceo or (roles_for_person[0] if roles_for_person else None),
            is_current_public_company_ceo=bool(ceo), outside_public_board_count=len(boards) if evidence_known else None,
            unknown_board_count=unknown_by_person.get(member.person_id, 0) if evidence_known else 1,
            source_ids=sorted({board.source_ids[0] for board in boards if board.source_ids}),
        ))
    return sorted(result, key=lambda item: item.person_id)


__all__ = ["build_overboarding"]
