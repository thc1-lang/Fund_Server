"""Deterministic analyst dossier generation from frozen intelligence artifacts."""

from .assembler import assemble_dossier
from .models import CompanyResearchDossier

__all__ = ["CompanyResearchDossier", "assemble_dossier"]
