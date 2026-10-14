"""
Piensos: relleno semanal de comederos y consumo.

Los comederos se llenan a principio de semana; al volver a llenarlos, lo que ha hecho falta es lo que
se ha consumido desde el relleno anterior (como el depósito lleno a lleno de los vehículos).

Pestañas:
  Comederos           lista de comederos (pienso, alias) + panel con último relleno, kg/semana, media y puesto
  Piensos_Rellenos    cada relleno aceptado; días del ciclo y kg/día se calculan con fórmulas

analizar_relleno calcula y detecta problemas SIN escribir; registrar_relleno escribe.
"""

from datetime import date, datetime

import gspread

from gasoil import (_clasificar, _con_cache, _f, _fecha, _formato_numero, _lock, _obtener_o_crear,
                    _puntuar, _siguiente_fila, _toks)
from vinos import _sep
from zona import ahora, hoy

HOJA_COM = "Comederos"
HOJA_REL = "Piensos_Rellenos"

CAB_COM = ["Comedero", "Pienso habitual", "Alias", "Último relleno", "Kg del último relleno",
           "Kg/semana (último ciclo)", "Media kg/semana", "Puesto (más consumo = 1)"]
CAB_REL = ["Fecha", "Comedero", "Pienso", "Kg", "Cantidad dicha", "Días desde el relleno anterior",
           "Kg por día", "Registrado por", "Mensaje original", "Timestamp", "Revisar"]

KG_POR_CESTA = 24            # igual que en el Excel de la finca
PIENSOS = ["JABALIES", "CIERVOS", "CIERVOS TROFEO"]   # un silo por tipo (el de jabalíes también alimenta a los corzos)
LIMITE_KG = 3000             # un relleno mayor lleva aviso
FACTOR_ATIPICO = (0.3, 2.5)  # consumo fuera de este rango respecto a la media -> aviso

COMEDEROS_INICIALES = [
    ("Cuernico", "", "cuernico"),
    ("Charca Roja", "", "charca roja, charca"),
    ("Laguna", "", "laguna"),
    ("Halcones", "", "halcones"),
    ("Granja (criadero halcones)", "", "granja, criadero halcones, granja halcones"),
    ("Casa", "", "casa"),
    ("Corzos (Batanejo)", "JABALIES", "corzos, batanejo"),
    ("Nueva Granja (Majuelos)", "", "nueva granja, majuelos, granja nueva"),
    ("Comedero Venados", "", "venados, comedero de venados"),
]


# ── Lógica pura ───────────────────────────────────────────────

def pienso_canonico(texto) -> str | None:
    t = " ".join(_toks(str(texto or "")))
    if not t:
        return None
    if "trofeo" in t:
        return "CIERVOS TROFEO"
    if "ciervo" in t or "venado" in t or "cierv" in t:
        return "CIERVOS"
    if "jabal" in t or "corzo" in t:
        return "JABALIES"
    return None


def validar_item(m: dict) -> dict | None:
    """Normaliza un relleno devuelto por la IA. None si no es fiable."""
    try:
        comedero = str(m["comedero"]).strip()
        cantidad = _f(m["cantidad"])
    except (KeyError, TypeError):
        return None
    if not comedero or cantidad is None or cantidad <= 0 or cantidad > 100000:
        return None
    unidad = str(m.get("unidad") or "").strip().lower()
    avisos = []
    try:
        fecha = datetime.strptime(str(m.get("fecha")), "%Y-%m-%d").date()
    except ValueError:
        fecha = hoy()
    if fecha > hoy():
        fecha = hoy()
        avisos.append("la fecha salía en el futuro; he puesto hoy")
    item = {"comedero": comedero, "fecha": fecha, "pienso": pienso_canonico(m.get("pienso")), "avisos": avisos,
            "sacos": unidad.startswith("saco"), "sin_unidad": False, "kg": None, "dicha": ""}
    if item["sacos"]:
        item["dicha"] = f"{cantidad:g} sacos"
        return item
    if unidad.startswith("cest"):
        kg, dicha = cantidad * KG_POR_CESTA, f"{cantidad:g} cestas"
    elif unidad in ("kg", "kgs", "kilo", "kilos", "kilogramos"):
        kg, dicha = cantidad, f"{cantidad:g} kg"
    else:
        item["sin_unidad"] = True
        item["dicha"] = f"{cantidad:g}"
        return item
    item.update({"kg": int(kg) if float(kg).is_integer() else round(kg, 1), "dicha": dicha})
    return item


def resolver_comedero(texto: str, comederos: list[dict]):
    consulta = _toks(texto)
    return _clasificar([(_puntuar(consulta, _toks(f"{c['nombre']} {c['alias']}")), c) for c in comederos])


def item_reconocible(item: dict, comederos: list[dict]) -> bool:
    return resolver_comedero(item["comedero"], comederos)[0] != "no_encontrado"


# ── Hojas ─────────────────────────────────────────────────────

def hoja_rellenos() -> gspread.Worksheet:
    def construir(sh, ws):
        _formato_numero(sh, ws, 5, "NUMBER", "0")
        _formato_numero(sh, ws, 6, "NUMBER", "0.0")
    return _obtener_o_crear(HOJA_REL, CAB_REL, 2000, construir)


def formulas_comedero(r: int) -> list[str]:
    """Fórmulas D..H de la fila r de la pestaña Comederos."""
    s = _sep()
    R = HOJA_REL
    act = f'{R}!$B:$B{s}A{r}'
    d = f'=IF(COUNTIFS({act})=0{s}""{s}MAXIFS({R}!$A:$A{s}{act}))'
    e = f'=IF(D{r}=""{s}""{s}SUMIFS({R}!$D:$D{s}{act}{s}{R}!$A:$A{s}D{r}))'
    f = (f'=IF(D{r}=""{s}""{s}IF(SUMIFS({R}!$G:$G{s}{act}{s}{R}!$A:$A{s}D{r})=0{s}""{s}'
         f'SUMIFS({R}!$G:$G{s}{act}{s}{R}!$A:$A{s}D{r})*7))')
    g = f'=IFERROR(AVERAGEIFS({R}!$G:$G{s}{act}{s}{R}!$G:$G{s}">0")*7{s}"")'
    h = f'=IF(F{r}=""{s}""{s}RANK(F{r}{s}$F$2:$F$60))'
    return [d, e, f, g, h]


def hoja_comederos() -> gspread.Worksheet:
    hoja_rellenos()

    def construir(sh, ws):
        filas = []
        for i, (nombre, pienso, alias) in enumerate(COMEDEROS_INICIALES, start=2):
            filas.append([nombre, pienso, alias] + formulas_comedero(i))
        ws.update(values=filas, range_name=f"A2:H{len(filas) + 1}", value_input_option="USER_ENTERED")
        _formato_numero(sh, ws, 3, "DATE", "dd/mm/yyyy")
    return _obtener_o_crear(HOJA_COM, CAB_COM, 100, construir)


def asegurar_hojas() -> None:
    hoja_comederos()


def _leer_comederos_sin_cache() -> list[dict]:
    out = []
    for f in hoja_comederos().get_all_values()[1:]:
        f = f + [""] * (3 - len(f))
        if f[0].strip():
            out.append({"nombre": f[0].strip(), "pienso": pienso_canonico(f[1]) or "", "alias": f[2]})
    return out


def leer_comederos() -> list[dict]:
    return _con_cache("com", _leer_comederos_sin_cache)


# ── Análisis y registro ───────────────────────────────────────

def _rellenos(valores: list[list[str]]) -> list[dict]:
    out = []
    for f in valores[1:]:
        f = f + [""] * (10 - len(f))
        if f[0].strip():
            out.append({"fecha": _fecha(f[0]), "comedero": f[1], "kg": _f(f[3])})
    return out


def analizar_relleno(com: dict, item: dict, _valores=None) -> dict:
    """Calcula ciclo, consumo y avisos SIN escribir nada."""
    kg, fecha = item["kg"], item["fecha"]
    valores = _valores if _valores is not None else hoja_rellenos().get_all_values()
    previos = [a for a in _rellenos(valores) if a["comedero"] == com["nombre"] and a["fecha"]]
    avisos = list(item["avisos"])
    anteriores = [a for a in previos if a["fecha"] < fecha]
    prev = max(anteriores, key=lambda a: a["fecha"]) if anteriores else None
    dias = (fecha - prev["fecha"]).days if prev else None
    kgdia = (kg / dias) if dias else None

    if any(a["fecha"] == fecha and a["kg"] == kg for a in previos):
        avisos.append("ya hay un relleno igual de este comedero ese día (¿duplicado?)")
    elif any(a["fecha"] == fecha for a in previos):
        avisos.append("ya había otro relleno de este comedero ese mismo día")
    elif dias is not None and dias <= 2:
        avisos.append(f"este comedero se rellenó hace solo {dias} día(s)")
    if kg > LIMITE_KG:
        avisos.append(f"cantidad muy alta ({kg:g} kg)")
    if kgdia is not None:
        ciclos = []
        ordenados = sorted(previos, key=lambda a: a["fecha"])
        for a, b in zip(ordenados, ordenados[1:]):
            d = (b["fecha"] - a["fecha"]).days
            if d > 0 and b["kg"]:
                ciclos.append(b["kg"] / d)
        if len(ciclos) >= 3:
            media = sum(ciclos) / len(ciclos)
            if media > 0 and not (FACTOR_ATIPICO[0] * media <= kgdia <= FACTOR_ATIPICO[1] * media):
                avisos.append(f"consumo fuera de lo habitual ({kgdia:.0f} kg/día frente a una media de {media:.0f})")
    return {"dias": dias, "kgdia": kgdia, "prev": prev["fecha"].strftime("%d/%m/%Y") if prev else None,
            "primero": not previos, "avisos": avisos,
            "pienso": item["pienso"] or com["pienso"] or ""}


def registrar_relleno(com: dict, item: dict, usuario: str, mensaje: str, revisar: str = "") -> dict:
    s = _sep()
    with _lock:
        hoja_comederos()
        ws = hoja_rellenos()
        info = analizar_relleno(com, item, _valores=ws.get_all_values())
        fila = _siguiente_fila(ws)
        previo = f'MAXIFS($A:$A{s}$B:$B{s}B{fila}{s}$A:$A{s}"<"&A{fila})'
        dias_f = f'=IFERROR(IF({previo}=0{s}""{s}A{fila}-{previo}){s}"")'
        kgdia_f = f'=IF(F{fila}=""{s}""{s}D{fila}/F{fila})'
        ws.update(values=[[item["fecha"].strftime("%d/%m/%Y"), com["nombre"], info["pienso"], item["kg"],
                           item["dicha"], dias_f, kgdia_f, usuario, (mensaje or "")[:200],
                           ahora().isoformat(timespec="seconds"), revisar]],
                  range_name=f"A{fila}:K{fila}", value_input_option="USER_ENTERED")
    return info


# ── Consultas ─────────────────────────────────────────────────

def movimientos_enviados(dia: date) -> list[dict]:
    iso = dia.isoformat()
    out = []
    for f in hoja_rellenos().get_all_values()[1:]:
        f = f + [""] * (10 - len(f))
        if f[9].startswith(iso):
            out.append({"hora": f[9][11:16], "usuario": f[7], "comedero": f[1], "kg": f[3]})
    return out


def estado_comederos() -> list[dict]:
    out = []
    for f in hoja_comederos().get_all_values()[1:]:
        f = f + [""] * (8 - len(f))
        if f[0].strip():
            out.append({"nombre": f[0], "pienso": f[1], "fecha": f[3], "kg": f[4], "semana": f[5],
                        "media": f[6], "puesto": f[7]})
    return out
