# -*- coding: utf-8 -*-
"""Tests de instalacion_ia (logica pura; sin red ni descargas)."""
from pathlib import Path

from jobpulse_v15.instalacion_ia import (
    BYTES_TOTALES_MODELO,
    ArchivoModelo,
    estado_llama,
    estado_modelo,
    peso_legible,
    verificar_integridad,
)

FAKE = (
    ArchivoModelo("a.gguf", "a.gguf", 10, "0" * 64),
    ArchivoModelo("MTP/b.gguf", "b.gguf", 20, "f" * 64),
)


def test_peso_legible() -> None:
    assert peso_legible(500) == "500 B"
    assert peso_legible(2048) == "2.00 KiB"
    assert peso_legible(3 * 1024**2) == "3.00 MiB"
    assert "GiB" in peso_legible(BYTES_TOTALES_MODELO)


def test_bytes_totales() -> None:
    # 2.62 GB + 987 MB + 59 MB (verificado contra el repo de Unsloth)
    assert BYTES_TOTALES_MODELO == 2_620_370_976 + 986_833_728 + 59_235_648


def test_estado_modelo_vacio(tmp_path: Path) -> None:
    presentes, faltantes = estado_modelo(tmp_path, FAKE)
    assert presentes == []
    assert [a.nombre_local for a in faltantes] == ["a.gguf", "b.gguf"]


def test_estado_modelo_por_tamano(tmp_path: Path) -> None:
    (tmp_path / "a.gguf").write_bytes(b"x" * 10)  # tamano correcto
    (tmp_path / "b.gguf").write_bytes(b"x" * 5)   # corrupto (tamano corto)
    presentes, faltantes = estado_modelo(tmp_path, FAKE)
    assert [a.nombre_local for a in presentes] == ["a.gguf"]
    assert [a.nombre_local for a in faltantes] == ["b.gguf"]


def test_verificar_integridad_detecta_hash_malo(tmp_path: Path) -> None:
    (tmp_path / "a.gguf").write_bytes(b"x" * 10)
    problemas = verificar_integridad(tmp_path, (FAKE[0],), log=lambda *a: None)
    assert len(problemas) == 1
    assert "a.gguf" in problemas[0]
    assert "SHA256" in problemas[0]


def test_verificar_integridad_ignora_ausentes(tmp_path: Path) -> None:
    assert verificar_integridad(tmp_path, FAKE, log=lambda *a: None) == []


def test_estado_llama(tmp_path: Path) -> None:
    exe = tmp_path / "llama-server.exe"
    assert estado_llama(exe) is False
    exe.write_bytes(b"MZ")
    assert estado_llama(exe) is True
