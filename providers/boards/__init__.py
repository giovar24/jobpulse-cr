"""providers.boards — PROVEEDORES de job boards / portales (agregadores).

Estos son los agregadores (LinkedIn, Indeed, ANE, Procomer, CINDE) que
republican la oferta que la empresa publico en su propio ATS. En el dedupe
cross-source son *inferiores*: si el mismo puesto tambien viene directo del
ATS (providers.directos), se conserva la version del ATS.

Razon: LinkedIn/Indeed pueden republicar la misma vacante con clicks de
hosting propios (URLs canonicas distintas), un titulo levemente editado, y
fechas de republicacion — el ASO de la empresa es el registro de la verdad
(req id, fecha exacta, desc completa).
"""

from providers.boards.ane import AneProvider
from providers.boards.cinde import CindeProvider
from providers.boards.indeed import IndeedProvider
from providers.boards.linkedin import LinkedInProvider
from providers.boards.procomer import ProcomerProvider

__all__ = ["AneProvider", "CindeProvider", "IndeedProvider",
           "LinkedInProvider", "ProcomerProvider"]
