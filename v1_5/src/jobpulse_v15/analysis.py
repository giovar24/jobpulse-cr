"""Extraccion determinista de requisitos explicitos en ingles y espanol."""

from dataclasses import dataclass
import re
from typing import Any


EXTRACTOR_VERSION = "rules-2"

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?;])\s+|\n+")
_LEVEL_REQUIRED = re.compile(
    r"\b(required|requires?|requirement|mandatory|must|must have|"
    r"minimum|at least|requisito|se requiere|requiere|requerid[oa]s?|"
    r"obligatori[oa]|indispensable|al menos|mínim[oa])\b",
    re.IGNORECASE,
)
_LEVEL_PREFERRED = re.compile(
    r"\b(preferred|preferable|desirable|nice to have|a plus|"
    r"deseable|preferible|desead[oa])\b",
    re.IGNORECASE,
)
_LANGUAGE_CUE = re.compile(
    r"\b(fluent|fluency|proficien(?:t|cy)|conversational|advanced|"
    r"intermediate|bilingual|speak|spoken|communicat|language\s+skills?|"
    r"knowledge\s+of|idioma|nivel|dominio)\b",
    re.IGNORECASE,
)
_LANGUAGES = (
    (
        "en",
        "English",
        re.compile(r"\b(?:english|ingl[eé]s)\b", re.IGNORECASE),
    ),
    (
        "es",
        "Spanish",
        re.compile(r"\b(?:spanish|español|castellano)\b", re.IGNORECASE),
    ),
)
_OTHER_LANGUAGE = re.compile(
    r"\b(portugu[eê]s|french|franc[eé]s|german|alem[aá]n|"
    r"italian|italiano|dutch|neerland[eé]s|mandarin|chinese|"
    r"chino|japanese|japon[eé]s|korean|coreano|arabic|[aá]rabe)\b",
    re.IGNORECASE,
)
_LANGUAGE_MENTION = re.compile(
    r"\b(?:language|idioma|english|ingl[eé]s|spanish|español|castellano|"
    r"portugu[eê]s|french|franc[eé]s|german|alem[aá]n|"
    r"italian|italiano|dutch|neerland[eé]s|mandarin|chinese|"
    r"chino|japanese|japon[eé]s|korean|coreano|arabic|[aá]rabe)\b",
    re.IGNORECASE,
)
_QUALIFIER = re.compile(
    r"\b(native|fluent|advanced|intermediate|basic|"
    r"nativo|fluido|avanzado|intermedio|básico|basico|[abc][12])\b",
    re.IGNORECASE,
)
_EDUCATION = re.compile(
    r"\b(?:bachelor(?:'s)?(?:\s+degree)?|baccalaureate|"
    r"university\s+degree|college\s+degree|licenciatura|"
    r"bachillerato\s+universitario|grado\s+universitario|"
    r"high\s+school\s+diploma|diploma\s+de\s+secundaria)\b",
    re.IGNORECASE,
)
_EDUCATION_PROGRESS = re.compile(
    r"\b(in progress|currently pursuing|pursuing|en curso|cursando|"
    r"estudiando|incomplet[oa])\b",
    re.IGNORECASE,
)
_EDUCATION_COMPLETED = re.compile(
    r"\b(completed|graduated|concluded|completad[oa]|concluid[oa]|"
    r"graduad[oa]|finalizad[oa])\b",
    re.IGNORECASE,
)
_LICENSE = re.compile(
    r"\b(?:driver['’]s?\s+licen[cs]e|driving\s+licen[cs]e|"
    r"licencia\s+(?:de\s+conducir|para\s+conducir))\b",
    re.IGNORECASE,
)
_LICENSE_CODE = re.compile(
    r"\b(?:class|type|clase|tipo)\s*([A-D]\s?\d?)\b|"
    r"\blicencia\s+([A-D]\d?)\b",
    re.IGNORECASE,
)
_LICENSE_CONTEXT = re.compile(
    r"\b(?:driver['’]s?\s+licen[cs]e|driving\s+licen[cs]e|"
    r"licencia\s+(?:de\s+conducir|para\s+conducir)|"
    r"licencia\s+(?:B[1-4]|A[1-3]|C[1-3]))\b",
    re.IGNORECASE,
)
_LICENSE_MENTION = re.compile(
    r"\b(?:license|licence|licencia|permiso)\b",
    re.IGNORECASE,
)
_EXPERIENCE = re.compile(
    r"\b(?P<minimum>\d+)\s*(?P<plus>\+)?"
    r"(?:\s*(?:to|[-–])\s*(?P<maximum>\d+))?\s*"
    r"(?P<unit>years?|yrs?|años?)\s+(?:of\s+)?"
    r"(?:(?:relevant|professional|related)\s+)?experience\b",
    re.IGNORECASE,
)
_EXPERIENCE_SPANISH = re.compile(
    r"\bexperiencia\s+(?:mínima\s+)?(?:de\s+(?:al\s+menos\s+)?)?"
    r"(?P<minimum>\d+)\s*(?P<plus>\+)?"
    r"(?:\s*(?:a|[-–])\s*(?P<maximum>\d+))?\s*"
    r"(?P<unit>años?)\b",
    re.IGNORECASE,
)
_EXPERIENCE_SPANISH_SUFFIX = re.compile(
    r"\b(?P<minimum>\d+)\s*(?P<plus>\+)?"
    r"(?:\s*(?:a|[-–])\s*(?P<maximum>\d+))?\s*"
    r"(?P<unit>años?)\s+de\s+experiencia\b",
    re.IGNORECASE,
)
_EXPERIENCE_CUE = re.compile(
    r"\b(experience|experiencia|years?\s+of\s+experience|años?\s+de\s+experiencia)\b",
    re.IGNORECASE,
)
_SKILL_CONTEXT = re.compile(
    r"\b(skills?|technical skills?|competencies|competencias|"
    r"technologies|tecnolog[ií]as|tools|herramientas|"
    r"programming languages?|lenguajes de programaci[oó]n|"
    r"experience (?:with|in)|experiencia (?:con|en)|"
    r"knowledge of|conocimientos de|proficien(?:t|cy) in|"
    r"proficiencia en|familiar(?:ity)? with|familiaridad con|"
    r"expertise in|dominio de|hands-on experience with)\b",
    re.IGNORECASE,
)
_SKILLS = (
    (
        "python",
        "Python",
        re.compile(r"(?<!\w)python(?!\w)", re.IGNORECASE),
    ),
    (
        "sql",
        "SQL",
        re.compile(r"(?<!\w)sql(?!\w)", re.IGNORECASE),
    ),
    (
        "excel",
        "Microsoft Excel",
        re.compile(r"(?<!\w)(?:microsoft\s+)?excel(?!\w)", re.IGNORECASE),
    ),
    (
        "power_bi",
        "Power BI",
        re.compile(r"(?<!\w)power\s*bi(?!\w)", re.IGNORECASE),
    ),
    (
        "tableau",
        "Tableau",
        re.compile(r"(?<!\w)tableau(?!\w)", re.IGNORECASE),
    ),
    (
        "aws",
        "AWS",
        re.compile(r"(?<!\w)(?:amazon\s+web\s+services|aws)(?!\w)", re.IGNORECASE),
    ),
    (
        "azure",
        "Microsoft Azure",
        re.compile(r"(?<!\w)(?:microsoft\s+)?azure(?!\w)", re.IGNORECASE),
    ),
    (
        "gcp",
        "Google Cloud",
        re.compile(
            r"(?<!\w)(?:google cloud(?: platform)?|gcp)(?!\w)", re.IGNORECASE
        ),
    ),
    (
        "java",
        "Java",
        re.compile(r"(?<!\w)java(?!\w)", re.IGNORECASE),
    ),
    (
        "javascript",
        "JavaScript",
        re.compile(r"(?<!\w)javascript(?!\w)", re.IGNORECASE),
    ),
    (
        "typescript",
        "TypeScript",
        re.compile(r"(?<!\w)typescript(?!\w)", re.IGNORECASE),
    ),
    (
        "c++",
        "C++",
        re.compile(r"(?<!\w)c\+\+(?!\w)", re.IGNORECASE),
    ),
    (
        "c#",
        "C#",
        re.compile(r"(?<!\w)c#(?!\w)", re.IGNORECASE),
    ),
    (
        "linux",
        "Linux",
        re.compile(r"(?<!\w)linux(?!\w)", re.IGNORECASE),
    ),
    (
        "git",
        "Git",
        re.compile(r"(?<!\w)git(?!\w)", re.IGNORECASE),
    ),
    (
        "autocad",
        "AutoCAD",
        re.compile(r"(?<!\w)autocad(?!\w)", re.IGNORECASE),
    ),
    (
        "solidworks",
        "SolidWorks",
        re.compile(r"(?<!\w)solidworks(?!\w)", re.IGNORECASE),
    ),
    (
        "matlab",
        "MATLAB",
        re.compile(r"(?<!\w)matlab(?!\w)", re.IGNORECASE),
    ),
    (
        "sap",
        "SAP",
        re.compile(r"(?<!\w)sap(?!\w)", re.IGNORECASE),
    ),
    (
        "plc",
        "PLC",
        re.compile(
            r"(?<!\w)(?:plc|programmable logic controller)(?!\w)", re.IGNORECASE
        ),
    ),
    (
        "six_sigma",
        "Six Sigma",
        re.compile(r"(?<!\w)(?:lean\s+)?six\s+sigma(?!\w)", re.IGNORECASE),
    ),
)


@dataclass(frozen=True)
class ExtractionResult:
    """Resultado listo para persistirse mediante Repository.save_analysis."""

    analysis_status: str
    field_statuses: dict[str, str]
    requirements: list[dict[str, Any]]


class RuleBasedExtractor:
    """Extrae solo requisitos textualmente identificables; no infiere ausencias."""

    version = EXTRACTOR_VERSION

    def extract(self, text: str) -> ExtractionResult:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("El texto de la vacante es obligatorio.")

        field_statuses = {
            category: "not_mentioned"
            for category in (
                "language",
                "education",
                "license",
                "experience",
                "skill",
            )
        }
        requirements: list[dict[str, Any]] = []
        uncertain_categories: set[str] = set()

        for sentence in self._sentences(text):
            language_hits = self._extract_languages(sentence)
            if language_hits:
                requirements.extend(language_hits)
                if any(
                    item["requirement_level"] == "ambiguous"
                    for item in language_hits
                ):
                    uncertain_categories.add("language")
            elif (
                _LANGUAGE_CUE.search(sentence)
                or _LANGUAGE_MENTION.search(sentence)
                or _OTHER_LANGUAGE.search(sentence)
            ):
                uncertain_categories.add("language")

            education_hits = self._extract_education(sentence)
            if education_hits:
                requirements.extend(education_hits)
            elif re.search(
                r"\b(education|degree|bachelor|university|college|"
                r"educación|estudios|universidad|licenciatura|bachillerato)\b",
                sentence,
                re.IGNORECASE,
            ):
                uncertain_categories.add("education")

            license_hits = self._extract_license(sentence)
            if license_hits:
                requirements.extend(license_hits)
            elif _LICENSE_MENTION.search(sentence):
                uncertain_categories.add("license")

            experience_hits = self._extract_experience(sentence)
            if experience_hits:
                requirements.extend(experience_hits)
            elif _EXPERIENCE_CUE.search(sentence):
                uncertain_categories.add("experience")

            skill_hits = self._extract_skills(sentence)
            if skill_hits:
                requirements.extend(skill_hits)
            elif _SKILL_CONTEXT.search(sentence):
                uncertain_categories.add("skill")

        for requirement in requirements:
            category = requirement["category"]
            if requirement["requirement_level"] == "ambiguous":
                uncertain_categories.add(category)

        for requirement in requirements:
            if requirement["category"] in uncertain_categories:
                requirement["requirement_level"] = "ambiguous"
                field_statuses[requirement["category"]] = "ambiguous"
            elif field_statuses[requirement["category"]] != "ambiguous":
                field_statuses[requirement["category"]] = "found"

        for category in uncertain_categories:
            if field_statuses[category] == "not_mentioned":
                field_statuses[category] = "unknown"

        analysis_status = (
            "partial"
            if any(
                status in {"unknown", "ambiguous"}
                for status in field_statuses.values()
            )
            else "complete"
        )
        return ExtractionResult(
            analysis_status=analysis_status,
            field_statuses=field_statuses,
            requirements=requirements,
        )

    @staticmethod
    def _sentences(text: str) -> list[str]:
        return [part.strip() for part in _SENTENCE_SPLIT.split(text) if part.strip()]

    @staticmethod
    def _requirement_level(sentence: str) -> str:
        required = bool(_LEVEL_REQUIRED.search(sentence))
        preferred = bool(_LEVEL_PREFERRED.search(sentence))
        if required and preferred:
            return "ambiguous"
        if required:
            return "required"
        if preferred:
            return "preferred"
        return "inferred"

    @classmethod
    def _extract_languages(cls, sentence: str) -> list[dict[str, Any]]:
        if not (
            _LANGUAGE_CUE.search(sentence)
            or _LEVEL_REQUIRED.search(sentence)
            or _LEVEL_PREFERRED.search(sentence)
        ):
            return []
        qualifier_match = _QUALIFIER.search(sentence)
        qualifier = qualifier_match.group(0).lower() if qualifier_match else None
        level = cls._requirement_level(sentence)
        requirements = []
        for code, value_text, pattern in _LANGUAGES:
            if pattern.search(sentence):
                requirements.append(
                    cls._requirement(
                        "language",
                        code,
                        value_text,
                        sentence,
                        level,
                        qualifier=qualifier,
                    )
                )
        return requirements

    @classmethod
    def _extract_education(cls, sentence: str) -> list[dict[str, Any]]:
        match = _EDUCATION.search(sentence)
        if match is None:
            return []
        text = match.group(0)
        lowered = text.lower()
        if "bachelor" in lowered or "baccalaureate" in lowered:
            code = "bachelor"
        elif "high school" in lowered or "secundaria" in lowered:
            code = "secondary"
        else:
            code = "university_degree"

        progress = bool(_EDUCATION_PROGRESS.search(sentence))
        completed = bool(_EDUCATION_COMPLETED.search(sentence))
        status = "unspecified"
        if progress and completed:
            level = "ambiguous"
        elif progress:
            status = "pursuing"
            level = cls._requirement_level(sentence)
        elif completed:
            status = "completed"
            level = cls._requirement_level(sentence)
        else:
            level = cls._requirement_level(sentence)
        return [
            cls._requirement(
                "education",
                code,
                text,
                sentence,
                level,
                education_status=status,
            )
        ]

    @classmethod
    def _extract_license(cls, sentence: str) -> list[dict[str, Any]]:
        match = _LICENSE.search(sentence)
        if match is None:
            if _LICENSE_CONTEXT.search(sentence):
                code_match = _LICENSE_CODE.search(sentence)
                license_code = None
                if code_match:
                    license_code = code_match.group(1) or code_match.group(2)
                if license_code:
                    license_code = re.sub(r"\s+", "", license_code).upper()
                value_text = "Driver's license"
                if license_code:
                    value_text = f"Driver's license {license_code}"
                return [
                    cls._requirement(
                        "license",
                        license_code,
                        value_text,
                        sentence,
                        cls._requirement_level(sentence),
                    )
                ]
            return []
        code_match = _LICENSE_CODE.search(sentence)
        license_code = None
        if code_match:
            license_code = code_match.group(1) or code_match.group(2)
        if license_code:
            license_code = re.sub(r"\s+", "", license_code).upper()
        value_text = "Driver's license"
        if license_code:
            value_text = f"Driver's license {license_code}"
        return [
            cls._requirement(
                "license",
                license_code,
                value_text,
                sentence,
                cls._requirement_level(sentence),
            )
        ]

    @classmethod
    def _extract_experience(cls, sentence: str) -> list[dict[str, Any]]:
        match = (
            _EXPERIENCE.search(sentence)
            or _EXPERIENCE_SPANISH.search(sentence)
            or _EXPERIENCE_SPANISH_SUFFIX.search(sentence)
        )
        if match is None:
            return []
        minimum = int(match.group("minimum"))
        maximum_text = match.group("maximum")
        maximum = int(maximum_text) if maximum_text else None
        unit = match.group("unit").lower()
        normalized_unit = (
            "years" if unit.startswith(("year", "yr")) or "año" in unit else unit
        )
        plus = bool(match.group("plus"))
        if plus:
            maximum = None
        value_text = f"{minimum}+ {normalized_unit} of experience" if plus else (
            f"{minimum}-{maximum} {normalized_unit} of experience"
            if maximum is not None
            else f"{minimum} {normalized_unit} of experience"
        )
        return [
            cls._requirement(
                "experience",
                "years_experience",
                value_text,
                sentence,
                cls._requirement_level(sentence),
                minimum=minimum,
                maximum=maximum,
                unit=normalized_unit,
            )
        ]

    @classmethod
    def _extract_skills(cls, sentence: str) -> list[dict[str, Any]]:
        if not (
            _SKILL_CONTEXT.search(sentence)
            or _LEVEL_REQUIRED.search(sentence)
            or _LEVEL_PREFERRED.search(sentence)
        ):
            return []
        level = cls._requirement_level(sentence)
        return [
            cls._requirement("skill", code, value_text, sentence, level)
            for code, value_text, pattern in _SKILLS
            if pattern.search(sentence)
        ]

    @staticmethod
    def _requirement(
        category: str,
        value_code: str | None,
        value_text: str,
        evidence: str,
        requirement_level: str,
        **extra: Any,
    ) -> dict[str, Any]:
        return {
            "category": category,
            "value_code": value_code,
            "value_text": value_text,
            "requirement_level": requirement_level,
            "evidence": evidence,
            "origin": "rule",
            "confidence": 0.9,
            **extra,
        }
