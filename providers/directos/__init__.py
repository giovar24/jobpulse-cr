"""providers.directos — providers que apican directos al ATS del employer.

Estas fuentes consultan la ATS (Workday, Phenom, Greenhouse, Rippling,
SuccessFactors/RSS, SmartRecruiters, Ashby/Lever/Recruitee/Workable,
tp-Umbraco, iCIMS) -> fuente canonica del job posting (req id, fecha,
descripcion sin mediacion, URL del ATS).

En el dedupe cross-source tienen prioridad sobre providers.boards: si el
mismo puesto aparece tambien en LinkedIn/Indeed/ANE, se conserva la fuente
del ATS (ver core.dedupe) y la instancia de agregador se marca duplicada.
"""

from providers.directos.workday import WorkdayProvider
from providers.directos.phenom import PhenomProvider
from providers.directos.tp import TpProvider
from providers.directos.greenhouse import GreenhouseProvider
from providers.directos.icims import IcimsProvider
from providers.directos.smartrecruiters import SmartRecruitersProvider
from providers.directos.successfactors import SuccessFactorsProvider
from providers.directos.rippling import RipplingProvider
from providers.directos.ats_ligeros import AtsLigerosProvider

__all__ = [
    "WorkdayProvider", "PhenomProvider", "TpProvider",
    "GreenhouseProvider", "IcimsProvider",
    "SmartRecruitersProvider", "SuccessFactorsProvider",
    "RipplingProvider", "AtsLigerosProvider",
]
