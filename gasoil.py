"""
Repostajes de vehículos y depósitos de gasoil sobre Google Sheets.

Pestañas:
  Vehiculos            lista de vehículos (alias y tipo de contador) + resumen con fórmulas
  Repostajes           cada repostaje aceptado; tramo y consumo se calculan con fórmulas
  Gasoil_Depositos     lista de depósitos (capacidad, alias) + panel "última lectura" con fórmulas
  Gasoil_Movimientos   entradas y lecturas de depósitos aceptadas

Las funciones analizar_* calculan y detectan problemas SIN escribir (para enseñar la ficha antes de apuntar);
las registrar_* escriben. Para corregir algo ya apuntado se edita o se borra la fila en el Sheet.
"""

import re
import threading
import time
from datetime import date, datetime

import gspread

from sheets import _get_spreadsheet
from vinos import _a_int, _sep, _token_coincide, normalizar
from zona import ahora, hoy

HOJA_VEH = "Vehiculos"
HOJA_REP = "Repostajes"
HOJA_DEP = "Gasoil_Depositos"
HOJA_GMOV = "Gasoil_Movimientos"

CAB_VEH = ["Vehículo", "Alias", "Contador (km/horas)", "Último contador", "Último repostaje",
           "Consumo acumulado (L/100 km o L/h)"]
CAB_REP = ["Fecha", "Vehículo", "Litros", "Contador", "Lleno", "Tramo (km u horas)",
           "Consumo del tramo (L/100 km o L/h)", "Importe (€)", "Registrado por",
           "Mensaje original", "Timestamp", "Revisar"]
CAB_DEP = ["Depósito", "Capacidad (L)", "¿Se mide?", "Última lectura (L)", "Fecha última lectura",
           "Entradas este año (L)", "Alias"]
CAB_GMOV = ["Fecha", "Depósito", "Tipo", "Litros", "Registrado por", "Mensaje original", "Timestamp", "Revisar"]

VEHICULOS_INICIALES = [
    ("TOYOTA HILUX 6644LBS", "hilux, toyota hilux, 6644", "km"),
    ("TOYOTA LAND CRUISER LPVA 6157GSW", "land cruiser, toyota land cruiser, lpva, 6157", "km"),
    ("TOYOTA LAND CRUISER 2504BDZ", "land cruiser, toyota land cruiser, 2504", "km"),
    ("FORD RANGER 2462GCK", "ranger, ford ranger, 2462", "km"),
    ("VOLVO XC90 9930JRM", "volvo, xc90, 9930", "km"),
    ("ISUZU 0679GTK", "isuzu, 0679", "km"),
    ("TRACTOR DEUTZ (VERDE)", "deutz, tractor verde, verde", "horas"),
    ("TRACTOR JOHN DEERE CASE (ROJO) IH MAXX", "john deere, case, maxx, tractor rojo, rojo", "horas"),
]
DEPOSITOS_INICIALES = [
    ("Casa principal - Depósito 1", 1500, "SI", "casa grande, casa principal, grande, principal"),
    ("Casa principal - Depósito 2", 1500, "SI", "casa grande, casa principal, grande, principal"),
    ("Casa labor - Depósito 1", 750, "SI", "casa labor, labor"),
    ("Casa labor - Depósito 2", 750, "SI", "casa labor, labor"),
    ("Tractor", 3000, "NO", "tractor, deposito del tractor"),
    ("Casa pequeña (soterrado)", "", "NO", "casa pequeña, pequeña, soterrado"),
]

LIMITE_TRAMO = {"km": 3000, "horas": 300}
LIMITE_LITROS = {"km": 150, "horas": 400}

_lock = threading.Lock()
_hojas: dict[str, gspread.Worksheet] = {}
_cache_config: dict[str, tuple[float, list]] = {}
TTL_CONFIG = 60   # segundos: la lista de vehículos y depósitos casi no cambia
_VACIAS = {"de", "del", "la", "el", "los", "las", "un", "una", "y", "coche", "vehiculo", "deposito"}


# ── Utilidades (lógica pura) ──────────────────────────────────

def _f(valor):
    try:
        return float(str(valor).replace(",", ".").strip())
    except (ValueError, TypeError):
        return None


def _fecha(valor) -> date | None:
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(valor).strip()[:10], fmt).date()
        except ValueError:
            continue
    return None


def _toks(texto: str) -> list[str]:
    return [t for t in normalizar(texto).split() if t not in _VACIAS]


def _puntuar(consulta: list[str], pajar: list[str]) -> float:
    if not consulta or not pajar:
        return 0.0
    return sum(1 for q in consulta if _token_coincide(q, pajar)) / len(consulta)


def _clasificar(puntuaciones: list[tuple[float, dict]]):
    """(estado, candidatos): único / ambiguo / no_encontrado."""
    res = sorted([p for p in puntuaciones if p[0] >= 0.6], key=lambda x: -x[0])
    if not res:
        return "no_encontrado", []
    mejor = res[0][0]
    if mejor >= 0.85 and (len(res) == 1 or res[1][0] < mejor - 0.12):
        return "unico", [res[0][1]]
    return "ambiguo", [v for _, v in res[:4]]


def resolver_vehiculo(texto: str, vehiculos: list[dict]):
    consulta = _toks(texto)
    return _clasificar([(_puntuar(consulta, _toks(f"{v['nombre']} {v['alias']}")), v) for v in vehiculos])


def resolver_depositos(nombre: str, numero, es_total: bool, depositos: list[dict]):
    """Devuelve (estado, depósitos, mensaje). estado: 'ok' o 'error' (el mensaje explica qué escribir)."""
    consulta = _toks(nombre or "")
    punt = [(_puntuar(consulta, _toks(f"{d['grupo']} {d['alias']}")), d) for d in depositos]
    mejor = max((p for p, _ in punt), default=0)
    if mejor < 0.6:
        grupos = sorted({d["grupo"] for d in depositos})
        return "error", [], ("No reconozco ese depósito. Los que conozco son: " + ", ".join(grupos) + ".")
    grupos = {d["grupo"] for p, d in punt if p >= mejor - 0.001}
    if len(grupos) > 1:
        return "error", [], ("No sé a cuál te refieres (" + ", ".join(sorted(grupos)) +
                             "). Vuelve a escribirlo indicando solo uno.")
    grupo = grupos.pop()
    filas = [d for d in depositos if d["grupo"] == grupo]
    if len(filas) == 1:
        return "ok", filas, ""
    if es_total or numero in (None, ""):
        ej = f"{grupo} depósito 1: 900, depósito 2: 940"
        return "error", [], (f"{grupo} tiene {len(filas)} depósitos y no me ha quedado claro cuánto es de "
                             f"cada uno. Escríbelo indicando cada depósito, por ejemplo: «{ej}», "
                             "o «en cada depósito».")
    if str(numero).lower() in ("todos", "cada", "ambos"):
        return "ok", filas, ""
    elegidos = [d for d in filas if d["numero"] == _a_int(numero, -1)]
    if not elegidos:
        return "error", [], f"{grupo} no tiene un depósito {numero}."
    return "ok", elegidos, ""


def item_reconocible(item: dict, vehiculos: list[dict], depositos: list[dict]) -> bool:
    """¿El elemento habla de uno de nuestros vehículos o depósitos? (si no, no es gasoil de la finca)"""
    if item["tipo"] == "repostaje":
        return resolver_vehiculo(item["vehiculo"], vehiculos)[0] != "no_encontrado"
    consulta = _toks(item["deposito"])
    return max((_puntuar(consulta, _toks(f"{d['grupo']} {d['alias']}")) for d in depositos), default=0) >= 0.6


def validar_item(m: dict) -> dict | None:
    """Normaliza y valida un elemento devuelto por el LLM. None si no es fiable."""
    try:
        tipo = str(m["tipo"]).lower()
        litros = _f(m["litros"])
    except (KeyError, TypeError):
        return None
    if tipo not in ("repostaje", "entrada", "lectura") or litros is None or litros < 0 or litros > 20000:
        return None
    if tipo != "lectura" and litros == 0:
        return None
    try:
        fecha = datetime.strptime(str(m.get("fecha")), "%Y-%m-%d").date()
    except ValueError:
        fecha = hoy()
    item = {"tipo": tipo, "litros": litros if litros % 1 else int(litros), "fecha": fecha}
    if tipo == "repostaje":
        veh = str(m.get("vehiculo") or "").strip()
        contador = _f(m.get("contador"))
        if not veh:
            return None
        lleno = m.get("lleno")
        importe = _f(m.get("importe"))
        item.update({"vehiculo": veh, "contador": (int(contador) if contador and contador % 1 == 0 else contador),
                     "lleno": False if lleno is False else True, "importe": importe})
    else:
        dep = str(m.get("deposito") or "").strip()
        if not dep:
            return None
        item.update({"deposito": dep, "numero_deposito": m.get("numero_deposito"),
                     "es_total": bool(m.get("es_total"))})
    return item


# ── Hojas de Google Sheets ────────────────────────────────────

def _formato_numero(sh, ws, col: int, tipo: str, patron: str) -> None:
    sh.batch_update({"requests": [{"repeatCell": {
        "range": {"sheetId": ws.id, "startRowIndex": 1, "endRowIndex": ws.row_count,
                  "startColumnIndex": col, "endColumnIndex": col + 1},
        "cell": {"userEnteredFormat": {"numberFormat": {"type": tipo, "pattern": patron}}},
        "fields": "userEnteredFormat.numberFormat"}}]})


def _obtener_o_crear(nombre: str, cab: list[str], filas: int, construir) -> gspread.Worksheet:
    sh = _get_spreadsheet()
    if nombre in _hojas:
        return _hojas[nombre]
    try:
        _hojas[nombre] = sh.worksheet(nombre)
        return _hojas[nombre]
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=nombre, rows=filas, cols=len(cab))
        try:
            ws.append_row(cab, value_input_option="RAW")
            ws.freeze(rows=1)
            construir(sh, ws)
        except Exception:
            sh.del_worksheet(ws)
            raise
        _hojas[nombre] = ws
        return ws


def _siguiente_fila(ws) -> int:
    fila = len(ws.col_values(1)) + 1
    if fila > ws.row_count:
        ws.add_rows(500)
    return fila


def hoja_repostajes() -> gspread.Worksheet:
    def construir(sh, ws):
        _formato_numero(sh, ws, 5, "NUMBER", "0")
        _formato_numero(sh, ws, 6, "NUMBER", "0.0")
    return _obtener_o_crear(HOJA_REP, CAB_REP, 2000, construir)


def hoja_gasoil_mov() -> gspread.Worksheet:
    return _obtener_o_crear(HOJA_GMOV, CAB_GMOV, 2000, lambda sh, ws: None)


def formulas_vehiculo(r: int) -> list[str]:
    """Fórmulas D (último contador), E (último repostaje) y F (consumo acumulado) de la fila r de Vehiculos."""
    s = _sep()
    R = HOJA_REP
    act = f'{R}!$B:$B{s}A{r}'
    minimo = f'MINIFS({R}!$D:$D{s}{act})'
    d = f'=IF(COUNTIFS({act})=0{s}""{s}MAXIFS({R}!$D:$D{s}{act}))'
    e = f'=IF(COUNTIFS({act})=0{s}""{s}MAXIFS({R}!$A:$A{s}{act}))'
    litros = f'(SUMIFS({R}!$C:$C{s}{act})-SUMIFS({R}!$C:$C{s}{act}{s}{R}!$D:$D{s}{minimo}))'
    factor = f'IF(C{r}="horas"{s}1{s}100)'
    f = f'=IF(COUNTIFS({act})<2{s}""{s}IFERROR({litros}/(D{r}-{minimo})*{factor}{s}""))'
    return [d, e, f]


def hoja_vehiculos() -> gspread.Worksheet:
    hoja_repostajes()

    def construir(sh, ws):
        filas = []
        for i, (nombre, alias, cont) in enumerate(VEHICULOS_INICIALES, start=2):
            filas.append([nombre, alias, cont] + formulas_vehiculo(i))
        ws.update(values=filas, range_name=f"A2:F{len(filas) + 1}", value_input_option="USER_ENTERED")
        _formato_numero(sh, ws, 4, "DATE", "dd/mm/yyyy")
        _formato_numero(sh, ws, 5, "NUMBER", "0.0")
    return _obtener_o_crear(HOJA_VEH, CAB_VEH, 100, construir)


def formulas_deposito(r: int) -> list[str]:
    """Fórmulas D (última lectura), E (su fecha) y F (entradas del año) de la fila r de Gasoil_Depositos."""
    s = _sep()
    M = HOJA_GMOV
    base = f'{M}!$B:$B{s}A{r}{s}{M}!$C:$C{s}"LECTURA"'
    ent = f'{M}!$B:$B{s}A{r}{s}{M}!$C:$C{s}"ENTRADA"'
    e = f'=IF(C{r}<>"SI"{s}""{s}IF(COUNTIFS({base})=0{s}""{s}MAXIFS({M}!$A:$A{s}{base})))'
    d = f'=IF(E{r}=""{s}""{s}SUMIFS({M}!$D:$D{s}{base}{s}{M}!$A:$A{s}E{r}))'
    f = f'=SUMIFS({M}!$D:$D{s}{ent}{s}{M}!$A:$A{s}">="&DATE(YEAR(TODAY()){s}1{s}1))'
    return [d, e, f]


def hoja_depositos() -> gspread.Worksheet:
    hoja_gasoil_mov()

    def construir(sh, ws):
        filas = []
        for i, (nombre, cap, mide, alias) in enumerate(DEPOSITOS_INICIALES, start=2):
            d, e, f = formulas_deposito(i)
            filas.append([nombre, cap, mide, d, e, f, alias])
        ws.update(values=filas, range_name=f"A2:G{len(filas) + 1}", value_input_option="USER_ENTERED")
        _formato_numero(sh, ws, 4, "DATE", "dd/mm/yyyy")
    return _obtener_o_crear(HOJA_DEP, CAB_DEP, 100, construir)


def asegurar_hojas() -> None:
    hoja_vehiculos()
    hoja_depositos()


# ── Lectura de datos ──────────────────────────────────────────

def _con_cache(clave: str, leer) -> list[dict]:
    t, datos = _cache_config.get(clave, (0.0, []))
    if time.time() - t > TTL_CONFIG:
        datos = leer()
        _cache_config[clave] = (time.time(), datos)
    return [dict(d) for d in datos]


def _leer_vehiculos_sin_cache() -> list[dict]:
    filas = hoja_vehiculos().get_all_values()[1:]
    return [{"nombre": f[0].strip(), "alias": f[1] if len(f) > 1 else "",
             "contador": (f[2].strip().lower() if len(f) > 2 and f[2].strip() else "km")}
            for f in filas if f and f[0].strip()]


def _leer_depositos_sin_cache() -> list[dict]:
    salida = []
    for f in hoja_depositos().get_all_values()[1:]:
        f = f + [""] * (7 - len(f))
        if not f[0].strip():
            continue
        nombre = f[0].strip()
        m = re.match(r"^(.*?)\s*-\s*Dep[oó]sito\s*(\d+)\s*$", nombre, re.I)
        salida.append({"nombre": nombre, "grupo": m.group(1).strip() if m else nombre,
                       "numero": int(m.group(2)) if m else None,
                       "capacidad": _f(f[1]), "mide": f[2].strip().upper() == "SI", "alias": f[6]})
    return salida


def leer_vehiculos() -> list[dict]:
    return _con_cache("veh", _leer_vehiculos_sin_cache)


def leer_depositos() -> list[dict]:
    return _con_cache("dep", _leer_depositos_sin_cache)


def _repostajes(valores: list[list[str]]) -> list[dict]:
    out = []
    for f in valores[1:]:
        f = f + [""] * (11 - len(f))
        if f[0].strip():
            out.append({"fecha": _fecha(f[0]), "vehiculo": f[1], "litros": _f(f[2]), "contador": _f(f[3])})
    return out


def _gasoil(valores: list[list[str]]) -> list[dict]:
    out = []
    for i, f in enumerate(valores[1:], start=2):
        f = f + [""] * (7 - len(f))
        if f[0].strip():
            out.append({"fila": i, "fecha": _fecha(f[0]), "deposito": f[1], "tipo": f[2].strip().upper(),
                        "litros": _f(f[3])})
    return out


# ── Repostajes ────────────────────────────────────────────────

def analizar_repostaje(veh: dict, litros, contador, lleno: bool, fecha: date, _valores=None) -> dict:
    """Calcula tramo, consumo y avisos SIN escribir nada."""
    horas = veh["contador"] == "horas"
    unidad = "horas" if horas else "km"
    valores = _valores if _valores is not None else hoja_repostajes().get_all_values()
    previos = [p for p in _repostajes(valores) if p["vehiculo"] == veh["nombre"] and p["contador"] is not None]
    avisos = []
    anteriores = [p for p in previos if p["contador"] < contador]
    prev = max(anteriores, key=lambda p: p["contador"]) if anteriores else None
    if any(p["contador"] == contador for p in previos):
        avisos.append("ya hay un repostaje con ese mismo contador (¿duplicado?)")
    else:
        mayores = [p for p in previos if p["contador"] > contador]
        if mayores:
            top = max(p["contador"] for p in mayores)
            avisos.append(f"el contador ({contador:g}) es menor que el de un repostaje anterior ({top:g})")
    tramo = (contador - prev["contador"]) if prev else None
    if tramo and tramo > LIMITE_TRAMO[unidad]:
        avisos.append(f"tramo muy grande ({tramo:g} {unidad})")
    if litros > LIMITE_LITROS[unidad]:
        avisos.append(f"litros muy altos ({litros:g})")
    consumo = litros / tramo * (1 if horas else 100) if (tramo and lleno) else None
    return {"tramo": tramo, "consumo": consumo, "unidad": unidad, "horas": horas, "avisos": avisos,
            "prev_fecha": prev["fecha"].strftime("%d/%m/%Y") if prev and prev["fecha"] else None,
            "primero": not previos}


def registrar_repostaje(veh: dict, litros, contador, lleno: bool, fecha: date, usuario: str,
                        mensaje: str, importe=None, revisar: str = "") -> dict:
    s = _sep()
    with _lock:
        hoja_vehiculos()
        ws = hoja_repostajes()
        info = analizar_repostaje(veh, litros, contador, lleno, fecha, _valores=ws.get_all_values())
        fila = _siguiente_fila(ws)
        previo = f'MAXIFS($D:$D{s}$B:$B{s}B{fila}{s}$D:$D{s}"<"&D{fila})'
        tramo_f = f'=IFERROR(IF({previo}=0{s}""{s}D{fila}-{previo}){s}"")'
        consumo_f = (f'=IF(OR(F{fila}=""{s}E{fila}<>"SI"){s}""{s}IFERROR(C{fila}/F{fila}*'
                     f'IF(VLOOKUP(B{fila}{s}{HOJA_VEH}!$A:$C{s}3{s}FALSE)="horas"{s}1{s}100){s}""))')
        ws.update(values=[[fecha.strftime("%d/%m/%Y"), veh["nombre"], litros, contador,
                           "SI" if lleno else "NO", tramo_f, consumo_f,
                           importe if importe is not None else "", usuario, (mensaje or "")[:200],
                           ahora().isoformat(timespec="seconds"), revisar]],
                  range_name=f"A{fila}:L{fila}", value_input_option="USER_ENTERED")
    return info


# ── Depósitos ─────────────────────────────────────────────────

def analizar_gasoil(dep: dict, tipo: str, litros, fecha: date, _valores=None) -> dict:
    """Detecta problemas y si sustituirá una lectura del mismo día, SIN escribir."""
    tipo = tipo.upper()
    avisos, fila_existente = [], None
    if tipo == "LECTURA":
        if not dep["mide"]:
            raise ValueError(f"{dep['nombre']} no tiene medidor: solo se pueden apuntar entradas.")
        valores = _valores if _valores is not None else hoja_gasoil_mov().get_all_values()
        for a in _gasoil(valores):
            if a["deposito"] == dep["nombre"] and a["tipo"] == "LECTURA" and a["fecha"] == fecha:
                fila_existente = a["fila"]
        if dep["capacidad"] and litros > dep["capacidad"] * 1.02:
            avisos.append(f"la lectura ({litros:g} L) supera la capacidad ({dep['capacidad']:g} L)")
    elif dep["capacidad"] and litros > dep["capacidad"] * 1.05:
        avisos.append(f"la entrada ({litros:g} L) es mayor que la capacidad del depósito ({dep['capacidad']:g} L)")
    return {"avisos": avisos, "sustituye": fila_existente is not None, "fila_existente": fila_existente}


def registrar_gasoil(dep: dict, tipo: str, litros, fecha: date, usuario: str, mensaje: str,
                     revisar: str = "") -> dict:
    """Registra una ENTRADA o una LECTURA. Una lectura del mismo depósito y día sustituye a la anterior."""
    tipo = tipo.upper()
    with _lock:
        hoja_depositos()
        ws = hoja_gasoil_mov()
        info = analizar_gasoil(dep, tipo, litros, fecha, _valores=ws.get_all_values())
        fila = info["fila_existente"] or _siguiente_fila(ws)
        ws.update(values=[[fecha.strftime("%d/%m/%Y"), dep["nombre"], tipo, litros, usuario,
                           (mensaje or "")[:200], ahora().isoformat(timespec="seconds"), revisar]],
                  range_name=f"A{fila}:H{fila}", value_input_option="USER_ENTERED")
    return info


# ── Consultas ─────────────────────────────────────────────────

def movimientos_enviados(dia: date) -> dict:
    iso = dia.isoformat()
    rep, gas = [], []
    for f in hoja_repostajes().get_all_values()[1:]:
        f = f + [""] * (11 - len(f))
        if f[10].startswith(iso):
            rep.append({"hora": f[10][11:16], "usuario": f[8], "vehiculo": f[1], "litros": f[2], "contador": f[3]})
    for f in hoja_gasoil_mov().get_all_values()[1:]:
        f = f + [""] * (7 - len(f))
        if f[6].startswith(iso):
            gas.append({"hora": f[6][11:16], "usuario": f[4], "deposito": f[1], "tipo": f[2], "litros": f[3]})
    return {"repostajes": rep, "gasoil": gas}


def estado_depositos() -> list[dict]:
    """Panel de depósitos tal como lo calcula el Sheet: última lectura y entradas del año."""
    out = []
    for f in hoja_depositos().get_all_values()[1:]:
        f = f + [""] * (7 - len(f))
        if f[0].strip():
            out.append({"nombre": f[0], "capacidad": f[1], "mide": f[2].strip().upper() == "SI",
                        "lectura": f[3], "fecha": f[4], "entradas": f[5]})
    return out
