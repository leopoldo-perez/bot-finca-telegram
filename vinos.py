"""
Inventario de vinos sobre Google Sheets.

Hoja "Vinos":             una fila por vino con su STOCK INICIAL; "Stock actual" se calcula con una fórmula.
Hoja "Vinos_Movimientos": cada entrada (+) o salida (-) que el usuario ha aceptado.
Stock actual = stock inicial + suma de movimientos. Para corregir algo ya apuntado se edita o se borra la fila.
"""

import difflib
import re
import threading
import unicodedata
from collections import defaultdict
from datetime import date, datetime

import gspread

from sheets import _get_spreadsheet
from zona import ahora

HOJA_VINOS = "Vinos"
HOJA_MOV = "Vinos_Movimientos"

CAB_VINOS = ["ID", "Tipo", "Nombre", "D.O.", "Año", "Formato (L)", "Ubicación",
             "Notas", "Stock inicial (21/09/26)", "Stock actual"]
CAB_MOV = ["Fecha", "Vino", "Cantidad", "Motivo", "Quién / ocasión", "Registrado por",
           "Mensaje original", "ID vino", "Timestamp", "Revisar"]

BOTELLAS_POR_CAJA = 6
UMBRAL_GRANDE = 24            # movimientos mayores llevan un aviso en la ficha
_PALABRAS_VACIAS = {"vino", "vinos", "botella", "botellas", "caja", "cajas",
                    "de", "del", "la", "el", "los", "las", "y", "un", "una"}

_lock = threading.Lock()
_hojas: dict[str, gspread.Worksheet] = {}


class StockInsuficiente(Exception):
    def __init__(self, stock: int, pedido: int):
        super().__init__(f"Stock {stock}, se piden {-pedido}")
        self.stock = stock
        self.pedido = pedido


# ── Búsqueda difusa (lógica pura, sin Google) ─────────────────

def normalizar(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", str(texto or ""))
    texto = "".join(c for c in texto if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9 ]+", " ", texto)


def _tokens(texto: str) -> list[str]:
    return [t for t in normalizar(texto).split() if t not in _PALABRAS_VACIAS]


def _token_coincide(q: str, candidatos: list[str]) -> bool:
    for t in candidatos:
        if q == t or (len(q) >= 4 and t.startswith(q)):
            return True
        if len(q) >= 4 and difflib.SequenceMatcher(None, q, t).ratio() >= 0.84:
            return True
    return False


def _puntuar(consulta: list[str], vino: dict) -> float:
    pajar = _tokens(f"{vino['nombre']} {vino['do']} {vino['tipo']}")
    nombre_tokens = _tokens(vino["nombre"])
    if not consulta or not pajar:
        return 0.0
    aciertos = sum(1 for q in consulta if _token_coincide(q, pajar))
    cobertura = aciertos / len(consulta)
    sobrantes = max(0, len(nombre_tokens) - len(consulta))
    return cobertura - 0.02 * sobrantes


def buscar_candidatos(nombre: str, anyo, formato, vinos: list[dict],
                      ubicacion: str | None = None) -> list[tuple[float, dict]]:
    """Devuelve [(puntuación, vino)] ordenado de mejor a peor, solo con puntuación >= 0.6."""
    consulta = _tokens(nombre)
    ubic = normalizar(ubicacion).strip() if ubicacion else ""
    resultados = []
    for v in vinos:
        if ubic and ubic not in normalizar(v["ubicacion"]):
            continue
        if anyo and v["anyo"] and int(v["anyo"]) != int(anyo):
            continue
        if formato and v["formato"] and abs(float(v["formato"]) - float(formato)) > 0.01:
            continue
        p = _puntuar(consulta, v)
        if p >= 0.6:
            resultados.append((p, v))
    resultados.sort(key=lambda x: -x[0])
    return resultados


def resolver(nombre: str, anyo, formato, vinos: list[dict], ubicacion: str | None = None):
    """Devuelve (estado, candidatos). estado: 'unico' | 'ambiguo' | 'no_encontrado'."""
    res = buscar_candidatos(nombre, anyo, formato, vinos, ubicacion)
    if not res:
        return "no_encontrado", []
    mejor = res[0][0]
    if mejor >= 0.85 and (len(res) == 1 or res[1][0] < mejor - 0.12):
        return "unico", [res[0][1]]
    return "ambiguo", [v for _, v in res[:4]]


def validar_movimiento(m: dict) -> dict | None:
    """Normaliza y valida un movimiento devuelto por el LLM. None si no es fiable."""
    try:
        cantidad = int(m["cantidad"])
        tipo = str(m["tipo"]).lower()
        nombre = str(m["vino"]).strip()
    except (KeyError, TypeError, ValueError):
        return None
    if tipo not in ("entrada", "salida") or cantidad <= 0 or cantidad > 600 or not nombre:
        return None
    unidad = str(m.get("unidad") or "botellas").lower()
    es_caja = unidad.startswith("caja")
    if es_caja:
        cantidad *= BOTELLAS_POR_CAJA
    anyo = m.get("anyo")
    try:
        anyo = int(anyo) if anyo else None
    except (TypeError, ValueError):
        anyo = None
    try:
        formato = float(m["formato"]) if m.get("formato") else None
    except (TypeError, ValueError):
        formato = None
    try:
        fecha = datetime.strptime(str(m.get("fecha")), "%Y-%m-%d")
    except ValueError:
        fecha = ahora()
    return {
        "vino": nombre, "anyo": anyo, "formato": formato,
        "ubicacion": (str(m.get("ubicacion") or "").strip() or None),
        "quien": (str(m.get("quien") or "").strip() or None),
        "cantidad": cantidad if tipo == "entrada" else -cantidad,
        "era_caja": es_caja, "motivo": (m.get("motivo") or "").strip() or None,
        "fecha": fecha.strftime("%d/%m/%Y"),
    }


# ── Acceso a Google Sheets ────────────────────────────────────

def _a_int(valor, defecto=0) -> int:
    try:
        return int(float(str(valor).replace(",", ".").strip()))
    except (ValueError, TypeError):
        return defecto


_sep_cache: str | None = None


def _sep() -> str:
    """Separador de argumentos de fórmula según el idioma del documento (es_ES -> ';')."""
    global _sep_cache
    if _sep_cache is None:
        locale = _get_spreadsheet().fetch_sheet_metadata()["properties"].get("locale", "en_US")
        _sep_cache = "," if locale.lower().startswith(("en", "ja", "zh", "ko", "he", "th")) else ";"
    return _sep_cache


def _formula_stock(fila: int) -> str:
    s = _sep()
    return f'=I{fila}+SUMIFS({HOJA_MOV}!$C:$C{s}{HOJA_MOV}!$H:$H{s}A{fila})'


def _hoja(nombre: str, cab: list[str], filas: int) -> gspread.Worksheet:
    sh = _get_spreadsheet()
    if nombre in _hojas:
        return _hojas[nombre]
    try:
        _hojas[nombre] = sh.worksheet(nombre)
        return _hojas[nombre]
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=nombre, rows=filas, cols=len(cab))
        ws.append_row(cab, value_input_option="RAW")
        ws.freeze(rows=1)
        return ws


def hoja_movimientos() -> gspread.Worksheet:
    return _hoja(HOJA_MOV, CAB_MOV, 2000)


def hoja_vinos() -> gspread.Worksheet:
    return _hoja(HOJA_VINOS, CAB_VINOS, 500)


def leer_vinos() -> list[dict]:
    """Lee el inventario y calcula el stock actual (inicial + movimientos)."""
    filas_v = hoja_vinos().get_all_values()[1:]
    filas_m = hoja_movimientos().get_all_values()[1:]

    mov_por_vino: dict[str, int] = defaultdict(int)
    for f in filas_m:
        if len(f) > 7 and f[7].strip():
            mov_por_vino[f[7].strip()] += _a_int(f[2])

    vinos = []
    for f in filas_v:
        f = f + [""] * (10 - len(f))
        if not str(f[0]).strip():
            continue
        vid = str(f[0]).strip()
        crudo = str(f[8]).strip()
        dudoso = bool(crudo) and not re.fullmatch(r"-?\d+(?:[.,]\d+)?", crudo)
        inicial = _a_int(crudo)
        try:
            formato = float(str(f[5]).replace(",", ".")) if str(f[5]).strip() else None
        except ValueError:
            formato, dudoso = None, True
        vinos.append({
            "id": vid, "tipo": f[1], "nombre": f[2], "do": f[3],
            "anyo": _a_int(f[4]) or None, "formato": formato,
            "ubicacion": f[6], "notas": f[7], "dudoso": dudoso,
            "inicial": inicial, "stock": inicial + mov_por_vino.get(vid, 0),
        })
    return vinos


def descripcion(v: dict) -> str:
    partes = [v["nombre"].strip()]
    if v["anyo"]:
        partes.append(str(v["anyo"]))
    if v["formato"] and abs(v["formato"] - 0.75) > 0.01:
        partes.append(f"{v['formato']:g} L")
    return " ".join(partes)


def registrar_movimiento(vino_id: str, cantidad: int, fecha: str, motivo: str | None,
                         usuario: str, mensaje: str, permitir_negativo: bool = True,
                         quien: str | None = None, revisar: str = "") -> dict:
    """Añade un movimiento ya aceptado. Relee el stock justo antes de escribir."""
    with _lock:
        vinos = {v["id"]: v for v in leer_vinos()}
        vino = vinos.get(str(vino_id))
        if vino is None:
            raise ValueError(f"Vino {vino_id} no existe")
        antes = vino["stock"]
        despues = antes + cantidad
        if despues < 0 and not permitir_negativo:
            raise StockInsuficiente(antes, cantidad)
        ws = hoja_movimientos()
        fila = len(ws.col_values(1)) + 1
        if fila > ws.row_count:
            ws.add_rows(500)
        ws.update(
            values=[[fecha, descripcion(vino), cantidad, motivo or "", quien or "", usuario,
                     (mensaje or "")[:200], int(vino["id"]) if vino["id"].isdigit() else vino["id"],
                     ahora().isoformat(timespec="seconds"), revisar]],
            range_name=f"A{fila}:J{fila}", value_input_option="USER_ENTERED")
    return {"vino": vino, "antes": antes, "despues": despues}


def movimientos_enviados(dia: date) -> list[dict]:
    """Movimientos registrados en ese día (para el resumen /hoy)."""
    iso = dia.isoformat()
    salida = []
    for f in hoja_movimientos().get_all_values()[1:]:
        f = f + [""] * (9 - len(f))
        if f[8].startswith(iso):
            salida.append({"hora": f[8][11:16], "usuario": f[5], "cantidad": _a_int(f[2]),
                           "vino": f[1], "quien": f[4]})
    return salida


def crear_vino(nombre: str, anyo, formato, usuario: str) -> dict:
    """Crea un vino nuevo con stock inicial 0 para poder registrar una entrada."""
    with _lock:
        ws = hoja_vinos()
        ids = [_a_int(x) for x in ws.col_values(1)[1:]]
        nuevo_id = max(ids, default=0) + 1
        fila = len(ws.col_values(1)) + 1
        ws.update(values=[[nuevo_id, "", nombre, "", anyo or "", formato or 0.75, "",
                           f"Creado desde el bot por {usuario}: completar datos", 0, _formula_stock(fila)]],
                  range_name=f"A{fila}:J{fila}", value_input_option="USER_ENTERED")
    return {"id": str(nuevo_id), "tipo": "", "nombre": nombre, "do": "", "anyo": anyo,
            "formato": formato or 0.75, "ubicacion": "", "notas": "", "inicial": 0, "stock": 0}
