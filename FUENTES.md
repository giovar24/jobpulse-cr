# FUENTES.md — Portales de vacantes y su aporte real

Referencia de los portales que entregan vacantes a JobPulse CR. Aporte medido desde la base el **2026-10-22**
(884 crudas · 131 candidatas aprobadas sin duplicados).

> Regenerable: `python tools/fuentes_md.py` (lee `data/jobs.db`).

## Portales con resultados

| Portal | URL | Grupo | Crudo | Candidatas |
|---|---|---|---:|---:|
| [ANE / Brete (MTSS)](https://www.ane.cr/) | https://www.ane.cr/ | Estatal | 242 | 9 |
| [CINDE Jobs](https://cindejobs.com/) | https://cindejobs.com/ | Estatal | 18 | 1 |
| [Procomer Talento (Zona Franca)](https://talento.procomer.com/) | https://talento.procomer.com/ | Estatal | 1 | 1 |
| [LinkedIn Jobs](https://www.linkedin.com/jobs/) | https://www.linkedin.com/jobs/ | Job board | 212 | 73 |
| [Indeed Costa Rica](https://cr.indeed.com/) | https://cr.indeed.com/ | Job board | 112 | 27 |
| [Workday](https://www.myworkdayjobs.com/) — tableros por empresa | https://www.myworkdayjobs.com/ | ATS directo | 189 | 14 |
| [Ashby / Lever / Recruitee / Workable](https://jobs.ashbyhq.com/) — tableros por empresa | https://jobs.ashbyhq.com/ | ATS directo | 66 | 4 |
| [SAP SuccessFactors](https://www.successfactors.com/) — tableros por empresa | https://www.successfactors.com/ | ATS directo | 15 | 2 |
| [Rippling](https://ats.rippling.com/) — tableros por empresa | https://ats.rippling.com/ | ATS directo | 13 | 0 |
| [Greenhouse](https://job-boards.greenhouse.io/) — tableros por empresa | https://job-boards.greenhouse.io/ | ATS directo | 8 | 0 |
| [Phenom](https://www.phenompeople.com/) — tableros por empresa | https://www.phenompeople.com/ | ATS directo | 5 | 0 |
| [SmartRecruiters](https://careers.smartrecruiters.com/) — tableros por empresa | https://careers.smartrecruiters.com/ | ATS directo | 3 | 0 |

## Configurados, sin vacantes aún

- **iCIMS** (https://www.icims.com/) — boards TP/DocuSign archivados o sin CR activo.
- **Login sites (navegador del usuario)** (por empresa) — sitios con login: requieren sesion manual.
- **TP (Teleperformance)** (https://www.tp.com/) — board TP/DocuSign CR sin vacantes activas por ahora.

## Fuentes por-empresa

Varios ATS no tienen una unica pagina: el scraper lleva un catalogo local de tableros confirmados con presencia Costa Rica (regenerables, ver README):

- **Workday**: `data/empresas/workday_cr.json`
- **Phenom**: `data/empresas/phenom_cr.json`
- **Greenhouse**: `data/empresas/otros_ats_cr.json`
- **iCIMS**: `data/empresas/icims_boards.json`
- **SmartRecruiters**: `data/empresas/smartrecruiters_companies.json`
- **SAP SuccessFactors**: `data/empresas/successfactors_sites.json`
- **Rippling**: `data/empresas/rippling_boards.json`
- **Ashby / Lever / Recruitee / Workable**: `data/empresas/otros_ats_cr.json`
- **Login sites (navegador del usuario)**: `data/empresas/login_sites.json`

## Notas

- **Candidatas** = aprobadas por el gate (fecha + relevancia + dominio), sin duplicados marcados (`dup_of`).
- **Crudo** = todo lo que entrego el scraping a `jobs_raw` (incluye descartadas por el gate y excluidas por perfil).
- Los **job boards** (LinkedIn, Indeed) y los portales estatales republican puestos que ya estan en el ATS de la empresa: el dedupe conserva la version directa cuando ambas se ven.
