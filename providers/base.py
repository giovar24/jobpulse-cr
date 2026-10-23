from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional
from dataclasses import dataclass

@dataclass
class JobOffer:
    source: str
    search_term: str
    job_id: str
    title: str
    company: str
    location: str
    link: str
    date_published: Optional[str] = None
    # Fecha de vencimiento/cierre que publica el portal (ISO). Vacio si el
    # portal no la expone (ej. CINDE) o si el scraper no la encontro.
    expires_at: Optional[str] = None
    description: Optional[str] = None
    requirements: Optional[str] = None
    salary: Optional[str] = None
    # Sector de la vacante: lo publica la fuente (ej. CINDE
    # positionCategoryNameEs) o lo infiere core/sectores por el titulo.
    sector: Optional[str] = None
    raw_data: Optional[Dict[str, Any]] = None
    # Trazabilidad del gate de relevancia (core/gate.py); None si no evaluado.
    gate_score: Optional[float] = None
    gate_razon: Optional[str] = None

class BaseProvider(ABC):
    def __init__(self, config: Dict[str, Any]):
        self.config = config

    @abstractmethod
    async def fetch(self, search_term: str, location: str, pages: int) -> List[JobOffer]:
        """Fetch jobs from the provider"""
        pass

    def clean_text(self, text: Optional[str]) -> str:
        from core.text import limpiar_texto
        return limpiar_texto(text)
