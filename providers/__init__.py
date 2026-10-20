"""providers: raiz del paquete.

Estructura (razon: los job boards republican los puestos del ATS propio de
la empresa -> hay que poder diferenciarlos y deduplicarlos):

- providers/base.py      — BaseProvider/JobOffer (contrato comun).
- providers/boards/      — Job boards y portales locales (agregadores que
                           PUBLICAN el mismo puesto que la empresa ya publica
                           en su ATS: SoundAndMusic LinkedIn, Indeed, ANE,
                           Procomer, CINDE). "primeros density".
- providers/directos/    — Providers directos contra el ATS de la empresa
                           (Workday, Phenom, Greenhouse, Rippling, iCIMS...):
                           fuente canonica del puesto (id/req, fecha, desc).
"""

from providers.source_groups import SOURCE_GROUP, source_group  # noqa: F401
