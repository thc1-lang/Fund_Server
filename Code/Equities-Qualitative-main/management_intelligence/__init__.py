"""Evidence-backed person and career intelligence for management and boards.

Component 6A deliberately stores facts and source evidence only.  It does not
calculate management quality, credibility, governance, or investment scores.
"""

from .models import (
    CareerEntry,
    ManagementPerson,
    ManagementRun,
    OfficialSource,
    BoardMembership,
    RoleAssertion,
    SourceProvenance,
    PersonCompanyRelationship,
    EducationEntry,
    InsiderIdentityLink,
    RoleChangeEvent,
)
from .source_registry import ManagementSourceRegistry
from .store import ManagementStore
from .identity_resolution import IdentityResolution, ManagementIdentityResolver
from .insider_links import link_management_to_insiders
from .audit import audit_store
from .track_record_models import (
    Attribution, CapitalAllocationEvent, FinancialFact, GuidanceCommitment,
    GuidanceOutcome, ManagementTenure, OperatingOutcome, PersonTrackRecordSnapshot,
    StrategicCommitment, StrategicOutcome, TrackRecordRun, GuidanceCoverageDiagnostic,
)
from .track_record_store import TrackRecordStore
from .tenure import build_tenures
from .board_governance_models import (
    GovernanceBoardMembership, BoardCommittee, BoardTenure, BoardRefreshment,
    BoardExpertise, OverboardingRecord, RelatedPartyRelationship, VotingClass,
    VotingControlSnapshot, ShareholderRights, GovernanceEvent, GovernanceCoverage,
    GovernanceSnapshot, GovernanceRun,
)
from .governance_store import GovernanceStore
from .governance_sources import GovernanceSourceCatalog, CachedGovernanceSource
from .governance_audit import DirectorAuditFinding, audit_current_board, render_audit_report

__all__ = [
    "ManagementPerson",
    "CareerEntry",
    "BoardMembership",
    "RoleAssertion",
    "SourceProvenance",
    "PersonCompanyRelationship",
    "EducationEntry",
    "InsiderIdentityLink",
    "RoleChangeEvent",
    "OfficialSource",
    "ManagementRun",
    "ManagementSourceRegistry",
    "ManagementStore",
    "IdentityResolution",
    "ManagementIdentityResolver",
    "link_management_to_insiders",
    "audit_store",
    "Attribution",
    "ManagementTenure",
    "FinancialFact",
    "OperatingOutcome",
    "GuidanceCommitment",
    "GuidanceOutcome",
    "StrategicCommitment",
    "StrategicOutcome",
    "CapitalAllocationEvent",
    "PersonTrackRecordSnapshot",
    "TrackRecordRun",
    "GuidanceCoverageDiagnostic",
    "TrackRecordStore",
    "build_tenures",
    "GovernanceBoardMembership",
    "BoardCommittee",
    "BoardTenure",
    "BoardRefreshment",
    "BoardExpertise",
    "OverboardingRecord",
    "RelatedPartyRelationship",
    "VotingClass",
    "VotingControlSnapshot",
    "ShareholderRights",
    "GovernanceEvent",
    "GovernanceCoverage",
    "GovernanceSnapshot",
    "GovernanceRun",
    "GovernanceStore",
    "GovernanceSourceCatalog",
    "CachedGovernanceSource",
    "DirectorAuditFinding",
    "audit_current_board",
    "render_audit_report",
]
