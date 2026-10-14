"""
Integración con Google Sheets.

Columnas de la hoja "Registros":
  A: Día semana  B: Fecha (DD/MM/YYYY)  C: Horas  D: Categoría
  E: Trabajador  F: Notas               G: Timestamp
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv
from zona import ahora, hoy

load_dotenv(Path(__file__).parent / ".env")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]
CABECERAS = ["Día semana", "Fecha", "Horas", "Categoría", "Trabajador", "Notas", "Timestamp", "Revisar"]

_spreadsheet: gspread.Spreadsheet | None = None
_hojas: dict[str, gspread.Worksheet] = {}   # pestañas ya localizadas: evita una petición a Google por uso


def _get_spreadsheet() -> gspread.Spreadsheet:
    """Devuelve el spreadsheet, reutilizando la conexión si ya existe.

    Soporta dos modos:
    - MODO NUBE:  variable GOOGLE_SERVICE_ACCOUNT_JSON con el contenido JSON completo
    - MODO LOCAL: variable GOOGLE_SERVICE_ACCOUNT_FILE con la ruta al archivo JSON
    """
    global _spreadsheet
    if _spreadsheet is None:
        json_str = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
        if json_str:
            # Modo nube: credenciales inyectadas como string JSON
            info = json.loads(json_str)
            creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        else:
            # Modo local: credenciales en archivo de disco
            creds = Credentials.from_service_account_file(
                os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"],
                scopes=SCOPES
            )
        gc = gspread.authorize(creds, http_client=gspread.BackOffHTTPClient)
        _spreadsheet = gc.open_by_key(os.environ["GOOGLE_SPREADSHEET_ID"])
    return _spreadsheet


def _get_hoja(nombre: str) -> gspread.Worksheet:
    """Devuelve la hoja indicada, creándola con cabeceras si no existe."""
    sh = _get_spreadsheet()
    if nombre in _hojas:
        return _hojas[nombre]
    try:
        _hojas[nombre] = sh.worksheet(nombre)
        return _hojas[nombre]
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=nombre, rows=2000, cols=10)
        if nombre == "Registros":
            ws.append_row(CABECERAS, value_input_option="RAW")
            ws.freeze(rows=1)
        elif nombre == "Reportes":
            ws.append_row(["Timestamp", "Título", "Contenido"], value_input_option="RAW")
            ws.freeze(rows=1)
        return ws


def _parte_a_fila(parte: dict) -> list:
    fecha = parte.get("fecha", "")
    try:
        if fecha and "-" in fecha:
            fecha = datetime.strptime(fecha, "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        pass

    return [
        parte.get("dia_semana", "").upper(),
        fecha,
        parte.get("horas") or 0,
        parte.get("categoria", "OTROS").upper(),
        parte.get("trabajador", ""),
        parte.get("notas") or "",
        parte.get("timestamp_registro", ""),
        "; ".join(parte.get("_avisos") or []),
    ]


def guardar_partes(partes: list[dict]) -> int:
    """Guarda los partes en la hoja Registros. Devuelve el número de filas escritas."""
    if not partes:
        return 0
    ws = _get_hoja("Registros")
    filas = [_parte_a_fila(p) for p in partes]
    ws.append_rows(filas, value_input_option="USER_ENTERED")
    return len(filas)


def borrar_partes_fecha(trabajador: str, fechas: list[str]) -> int:
    """Elimina todas las filas del trabajador para las fechas indicadas (DD/MM/YYYY).
    Borra por bloques contiguos (pocas llamadas a Google). Devuelve las filas eliminadas."""
    ws = _get_hoja("Registros")
    todos = ws.get_all_values()  # incluye cabecera en fila 0
    fechas_set = set(fechas)
    filas = [
        i + 1  # gspread usa índice 1-based
        for i, fila in enumerate(todos)
        if i > 0 and len(fila) > 4
        and fila[4].lower() == trabajador.lower()  # col E = Trabajador
        and fila[1] in fechas_set                  # col B = Fecha
    ]
    bloques: list[list[int]] = []
    for f in filas:
        if bloques and f == bloques[-1][-1] + 1:
            bloques[-1].append(f)
        else:
            bloques.append([f])
    for b in reversed(bloques):  # de abajo arriba para no desplazar índices
        ws.delete_rows(b[0], b[-1])
    return len(filas)


def fechas_ya_registradas(trabajador: str) -> set[str]:
    """Fechas (DD/MM/YYYY) que el trabajador ya tiene registradas. Si Google falla, el error se
    propaga: es mejor avisar que guardar un parte duplicado sin darse cuenta."""
    ws = _get_hoja("Registros")
    registros = ws.get_all_records()
    return {
        r["Fecha"] for r in registros
        if str(r.get("Trabajador", "")).lower() == trabajador.lower() and r.get("Fecha")
    }


def leer_registros(
    trabajador: Optional[str] = None,
    mes: Optional[int] = None,
    anyo: Optional[int] = None
) -> list[dict]:
    """Lee registros con filtros opcionales de trabajador, mes y año."""
    ws = _get_hoja("Registros")
    registros = ws.get_all_records()

    if trabajador:
        registros = [r for r in registros if r.get("Trabajador", "").lower() == trabajador.lower()]

    if mes or anyo:
        def coincide(r):
            try:
                f = datetime.strptime(r.get("Fecha", ""), "%d/%m/%Y")
                return (not mes or f.month == mes) and (not anyo or f.year == anyo)
            except ValueError:
                return False
        registros = [r for r in registros if coincide(r)]

    return registros


def leer_partes_enviados(dia) -> list[dict]:
    """Filas de Registros cuyo Timestamp (momento en que se envió el parte) cae en ese día."""
    iso = dia.isoformat()
    ws = _get_hoja("Registros")
    return [r for r in ws.get_all_records() if str(r.get("Timestamp", "")).startswith(iso)]


def guardar_reporte(titulo: str, contenido: str) -> None:
    """Guarda un reporte en la hoja Reportes."""
    ws = _get_hoja("Reportes")
    ws.append_row(
        [ahora().strftime("%d/%m/%Y %H:%M"), titulo, contenido],
        value_input_option="RAW"
    )
