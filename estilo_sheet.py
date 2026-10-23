"""
Da formato al Google Sheet: cabeceras, anchos, formatos de fecha y número, filas alternas (solo donde hay datos),
filtros, listas desplegables, columnas técnicas ocultas, fórmulas protegidas con aviso y un panel "Resumen".

  py estilo_sheet.py

Se puede ejecutar las veces que haga falta (es idempotente) y NO modifica datos, salvo que renombra
"Hoja 1" (si está vacía) a "Resumen" y escribe ahí el panel.
"""

import gasoil as G
import piensos as PI
import vinos as V
from extractor import CATEGORIAS
from sheets import _get_spreadsheet
from vinos import _sep

VERDE = "#1F4D3A"
VERDE_CLARO = "#E8F1EC"
FILA_PAR = "#F6F8F7"

COLOR_PESTANA = {
    "Resumen": "#1F4D3A", "Registros": "#2563EB", "Reportes": "#2563EB",
    "Vinos": "#7B1E3A", "Vinos_Movimientos": "#B5546F",
    "Vehiculos": "#6B7280", "Repostajes": "#C26A12",
    "Gasoil_Depositos": "#6B7280", "Gasoil_Movimientos": "#E0924A",
    "Comederos": "#6B7280", "Piensos_Rellenos": "#7A8F3C",
}
ORDEN = ["Resumen", "Registros", "Reportes", "Vinos", "Vinos_Movimientos", "Vehiculos", "Repostajes",
         "Gasoil_Depositos", "Gasoil_Movimientos", "Comederos", "Piensos_Rellenos"]

# Por pestaña: anchos (px), columnas ocultas (técnicas: el bot las usa), formatos (tipo, patrón, alineación),
# listas desplegables, columnas con fórmula (aviso al editar) y columnas con texto largo que se recorta.
CONFIG = {
    "Registros": {
        "anchos": [110, 100, 70, 240, 150, 320, 160, 440], "ocultas": [6],
        "formatos": {0: (None, None, "CENTER"), 1: ("DATE", "dd/mm/yyyy", "CENTER"), 2: ("NUMBER", "0.0", "CENTER")},
        "listas": {3: CATEGORIAS}, "recortar": [7], "revisar": 7,
    },
    "Vinos": {
        "anchos": [50, 120, 280, 150, 60, 80, 150, 260, 120, 100], "ocultas": [],
        "formatos": {0: (None, None, "CENTER"), 4: ("NUMBER", "0", "CENTER"), 5: ("NUMBER", "0.00", "CENTER"),
                     8: ("NUMBER", "0", "CENTER"), 9: ("NUMBER", "0", "CENTER")},
        "formulas": [9],
    },
    "Vinos_Movimientos": {
        "anchos": [95, 280, 80, 170, 190, 130, 320, 70, 150, 440], "ocultas": [7, 8],
        "formatos": {0: ("DATE", "dd/mm/yyyy", "CENTER"), 2: ("NUMBER", "+0;[Red]-0;0", "CENTER")},
        "recortar": [6, 9], "revisar": 9,
    },
    "Vehiculos": {
        "anchos": [300, 280, 130, 130, 130, 190], "ocultas": [],
        "formatos": {2: (None, None, "CENTER"), 3: ("NUMBER", "#,##0", "CENTER"), 4: ("DATE", "dd/mm/yyyy", "CENTER"),
                     5: ("NUMBER", "0.0", "CENTER")},
        "listas": {2: ["km", "horas"]}, "formulas": [3, 4, 5],
    },
    "Repostajes": {
        "anchos": [95, 290, 70, 100, 65, 100, 130, 100, 130, 320, 150, 440], "ocultas": [10],
        "formatos": {0: ("DATE", "dd/mm/yyyy", "CENTER"), 2: ("NUMBER", "0.0#", "CENTER"), 3: ("NUMBER", "#,##0", "CENTER"),
                     4: (None, None, "CENTER"), 5: ("NUMBER", "#,##0", "CENTER"), 6: ("NUMBER", "0.0", "CENTER"),
                     7: ("NUMBER", '#,##0.00 "€"', "CENTER")},
        "listas": {4: ["SI", "NO"]}, "formulas": [5, 6], "recortar": [9, 11], "revisar": 11,
    },
    "Gasoil_Depositos": {
        "anchos": [260, 120, 100, 130, 140, 160, 280], "ocultas": [],
        "formatos": {1: ("NUMBER", "#,##0", "CENTER"), 2: (None, None, "CENTER"), 3: ("NUMBER", "#,##0", "CENTER"),
                     4: ("DATE", "dd/mm/yyyy", "CENTER"), 5: ("NUMBER", "#,##0", "CENTER")},
        "listas": {2: ["SI", "NO"]}, "formulas": [3, 4, 5],
    },
    "Gasoil_Movimientos": {
        "anchos": [95, 260, 90, 90, 130, 320, 150, 440], "ocultas": [6],
        "formatos": {0: ("DATE", "dd/mm/yyyy", "CENTER"), 2: (None, None, "CENTER"), 3: ("NUMBER", "#,##0", "CENTER")},
        "listas": {2: ["ENTRADA", "LECTURA"]}, "recortar": [5, 7], "revisar": 7,
    },
    "Comederos": {
        "anchos": [270, 150, 330, 120, 150, 170, 150, 150], "ocultas": [],
        "formatos": {1: (None, None, "CENTER"), 3: ("DATE", "dd/mm/yyyy", "CENTER"), 4: ("NUMBER", "#,##0", "CENTER"),
                     5: ("NUMBER", "#,##0", "CENTER"), 6: ("NUMBER", "#,##0", "CENTER"), 7: ("NUMBER", "0", "CENTER")},
        "listas": {1: PI.PIENSOS}, "formulas": [3, 4, 5, 6, 7],
    },
    "Piensos_Rellenos": {
        "anchos": [95, 250, 130, 80, 130, 120, 100, 130, 320, 150, 440], "ocultas": [9],
        "formatos": {0: ("DATE", "dd/mm/yyyy", "CENTER"), 2: (None, None, "CENTER"), 3: ("NUMBER", "#,##0", "CENTER"),
                     4: (None, None, "CENTER"), 5: ("NUMBER", "0", "CENTER"), 6: ("NUMBER", "0.0", "CENTER")},
        "listas": {2: PI.PIENSOS}, "formulas": [5, 6], "recortar": [8, 10], "revisar": 10,
    },
}


def rgb(hexa: str) -> dict:
    h = hexa.lstrip("#")
    return {"red": int(h[0:2], 16) / 255, "green": int(h[2:4], 16) / 255, "blue": int(h[4:6], 16) / 255}


def rango(sid, r0=None, r1=None, c0=None, c1=None) -> dict:
    d = {"sheetId": sid}
    for k, v in (("startRowIndex", r0), ("endRowIndex", r1), ("startColumnIndex", c0), ("endColumnIndex", c1)):
        if v is not None:
            d[k] = v
    return d


def preparar_resumen(sh) -> int:
    """Renombra la hoja vacía 'Hoja 1' a 'Resumen' (si procede) y devuelve su id."""
    if "Resumen" in [w.title for w in sh.worksheets()]:
        return sh.worksheet("Resumen").id
    ws = sh.worksheet("Hoja 1")
    assert not any(any(c.strip() for c in f) for f in ws.get_all_values()), "'Hoja 1' tiene contenido: no la toco"
    ws.update_title("Resumen")
    return ws.id


def escribir_resumen(sh) -> dict:
    """Escribe el panel y devuelve en qué fila (base 0) está cada bloque, para darle formato."""
    s = _sep()
    ws = sh.worksheet("Resumen")
    ids = {w.title: w.id for w in sh.worksheets()}

    def enlace(nombre, texto):
        return f'=HYPERLINK("#gid={ids[nombre]}"{s}"{texto}")' if nombre in ids else texto

    filas: list[list[str]] = []
    pos: dict = {}

    def añadir(fila, clave=None):
        if clave:
            pos[clave] = len(filas)
        filas.append((fila + [""] * 5)[:5])

    añadir(["PANEL DE CONTROL - FINCA"], "titulo")
    añadir(["Actualizado:", "=NOW()"], "fecha")
    añadir([])
    añadir(["POR REVISAR (filas marcadas por el bot)", "Filas"], "h_rev")
    pos["rev_ini"] = len(filas)
    for etiqueta, hoja, col in (("Partes de trabajo", "Registros", "H"), ("Vinos", V.HOJA_MOV, "J"),
                                ("Repostajes", G.HOJA_REP, "L"), ("Depósitos de gasoil", G.HOJA_GMOV, "H"),
                                ("Piensos", PI.HOJA_REL, "K")):
        añadir([etiqueta, f'=COUNTA({hoja}!${col}$2:${col})'])
    pos["rev_fin"] = len(filas)
    añadir([])
    añadir(["DEPÓSITOS DE GASOIL", "Última lectura (L)", "Fecha", "Entradas este año (L)"], "h_dep")
    pos["dep_ini"] = len(filas)
    for i in range(2, 8):
        añadir([f'={G.HOJA_DEP}!A{i}', f'={G.HOJA_DEP}!D{i}', f'={G.HOJA_DEP}!E{i}', f'={G.HOJA_DEP}!F{i}'])
    pos["dep_fin"] = len(filas)
    añadir([])
    añadir(["PIENSOS: CONSUMO POR COMEDERO", "Kg última semana", "Media kg/semana", "Puesto"], "h_pie")
    pos["pie_ini"] = len(filas)
    for i in range(2, 11):
        añadir([f'={PI.HOJA_COM}!A{i}', f'={PI.HOJA_COM}!F{i}', f'={PI.HOJA_COM}!G{i}', f'={PI.HOJA_COM}!H{i}'])
    pos["pie_fin"] = len(filas)
    añadir([])
    añadir(["VINOS"], "h_vinos")
    pos["vin_ini"] = len(filas)
    añadir(["Referencias en inventario", f'=COUNTA({V.HOJA_VINOS}!$A$2:$A)'])
    añadir(["Botellas en total (stock actual)", f'=SUM({V.HOJA_VINOS}!$J$2:$J)'])
    pos["vin_fin"] = len(filas)
    añadir([])
    añadir(["IR A"], "h_ir")
    pos["links"] = len(filas)
    añadir([enlace("Registros", "Partes de trabajo"), enlace("Vinos", "Vinos (inventario)"),
            enlace("Vinos_Movimientos", "Movimientos de vinos"), enlace("Repostajes", "Repostajes"),
            enlace("Gasoil_Movimientos", "Movimientos de gasoil")])
    añadir([enlace("Gasoil_Depositos", "Depósitos de gasoil"), enlace("Comederos", "Comederos (piensos)"),
            enlace("Piensos_Rellenos", "Rellenos de piensos")])
    pos["links_fin"] = len(filas)
    ws.clear()
    ws.update(values=filas, range_name="A1", value_input_option="USER_ENTERED")
    return pos


def reglas_extra(nombre: str, sid: int, filas: int, n: int) -> list[dict]:
    """Formato condicional de aspecto: filas alternas solo donde hay datos, y lecturas rápidas."""
    s = _sep()
    out = [{"ranges": [rango(sid, 1, filas, 0, n)], "booleanRule": {
        "condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": f'=AND($A2<>""{s}MOD(ROW(){s}2)=0)'}]},
        "format": {"backgroundColor": rgb(FILA_PAR)}}}]
    col_rev = CONFIG.get(nombre, {}).get("revisar")
    if col_rev is not None:
        letra = chr(ord("A") + col_rev)
        out.insert(0, {"ranges": [rango(sid, 1, filas, 0, n)], "booleanRule": {
            "condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": f'=LEN(${letra}2)>0'}]},
            "format": {"backgroundColor": rgb("#FAC8C8")}}})
    if nombre == "Vinos":
        col = rango(sid, 1, filas, 9, 10)
        out.insert(0, {"ranges": [col], "booleanRule": {
            "condition": {"type": "NUMBER_LESS", "values": [{"userEnteredValue": "0"}]},
            "format": {"textFormat": {"bold": True, "foregroundColor": rgb("#C62828")}}}})
        out.insert(1, {"ranges": [col], "booleanRule": {
            "condition": {"type": "NUMBER_EQ", "values": [{"userEnteredValue": "0"}]},
            "format": {"textFormat": {"foregroundColor": rgb("#9CA3AF")}}}})
    if nombre == "Comederos":
        out.insert(0, {"ranges": [rango(sid, 1, filas, 5, 7)], "gradientRule": {
            "minpoint": {"type": "MIN", "color": rgb("#FFFFFF")}, "maxpoint": {"type": "MAX", "color": rgb("#7BB899")}}})
    return out


def construir_peticiones(sh, pos: dict | None = None) -> list[dict]:
    meta = sh.fetch_sheet_metadata()
    hojas = {x["properties"]["title"]: x for x in meta["sheets"]}
    req: list[dict] = []

    # 0) limpiar lo que este script añade (para poder repetirlo): bandas, protecciones y reglas de aspecto
    restantes: dict[str, int] = {}
    for x in meta["sheets"]:
        titulo, sid = x["properties"]["title"], x["properties"]["sheetId"]
        for b in x.get("bandedRanges", []):
            req.append({"deleteBanding": {"bandedRangeId": b["bandedRangeId"]}})
        for p in x.get("protectedRanges", []):
            if str(p.get("description", "")).startswith("Fórmula"):
                req.append({"deleteProtectedRange": {"protectedRangeId": p["protectedRangeId"]}})
        reglas = x.get("conditionalFormats", [])
        propias = []
        for i, r in enumerate(reglas):
            b = r.get("booleanRule")
            formula = (b or {}).get("condition", {}).get("values", [{}])[0].get("userEnteredValue", "") if b else ""
            tipo = (b or {}).get("condition", {}).get("type", "") if b else ""
            if titulo == "Resumen" or "gradientRule" in r or tipo.startswith("NUMBER_") or "MOD(ROW()" in formula or "LEN($" in formula:
                propias.append(i)
        for i in reversed(propias):
            req.append({"deleteConditionalFormatRule": {"sheetId": sid, "index": i}})
        restantes[titulo] = len(reglas) - len(propias)

    # 1) orden y color de pestañas
    indice = 0
    for nombre in ORDEN:
        if nombre in hojas:
            sid = hojas[nombre]["properties"]["sheetId"]
            req.append({"updateSheetProperties": {"properties": {
                "sheetId": sid, "index": indice, "tabColorStyle": {"rgbColor": rgb(COLOR_PESTANA[nombre])}},
                "fields": "index,tabColorStyle"}})
            indice += 1

    # 2) cada pestaña de datos
    for nombre, cfg in CONFIG.items():
        if nombre not in hojas:
            continue
        p = hojas[nombre]["properties"]
        sid, filas = p["sheetId"], p["gridProperties"]["rowCount"]
        n = len(cfg["anchos"])

        req.append({"updateSheetProperties": {"properties": {
            "sheetId": sid, "gridProperties": {"frozenRowCount": 1, "hideGridlines": False}},
            "fields": "gridProperties.frozenRowCount,gridProperties.hideGridlines"}})
        req.append({"repeatCell": {"range": rango(sid, 1, filas, 0, n), "cell": {"userEnteredFormat": {
            "verticalAlignment": "MIDDLE", "textFormat": {"fontSize": 10}}},
            "fields": "userEnteredFormat(verticalAlignment,textFormat)"}})
        req.append({"repeatCell": {"range": rango(sid, 0, 1, 0, n), "cell": {"userEnteredFormat": {
            "backgroundColor": rgb(VERDE), "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE",
            "wrapStrategy": "WRAP", "textFormat": {"bold": True, "fontSize": 10, "foregroundColor": rgb("#FFFFFF")}}},
            "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,verticalAlignment,wrapStrategy,textFormat)"}})
        for c in range(n):    # el título de cada columna se alinea igual que sus datos
            alin_cab = cfg["formatos"][c][2] if c in cfg["formatos"] else "LEFT"
            req.append({"repeatCell": {"range": rango(sid, 0, 1, c, c + 1),
                                       "cell": {"userEnteredFormat": {"horizontalAlignment": alin_cab}},
                                       "fields": "userEnteredFormat.horizontalAlignment"}})
        req.append({"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "ROWS", "startIndex": 0, "endIndex": 1},
                                                  "properties": {"pixelSize": 38}, "fields": "pixelSize"}})
        req.append({"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "ROWS", "startIndex": 1, "endIndex": filas},
                                                  "properties": {"pixelSize": 24}, "fields": "pixelSize"}})
        for c, px in enumerate(cfg["anchos"]):
            req.append({"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": c, "endIndex": c + 1},
                                                      "properties": {"pixelSize": px, "hiddenByUser": c in cfg["ocultas"]},
                                                      "fields": "pixelSize,hiddenByUser"}})
        for c, (tipo, patron, alin) in cfg["formatos"].items():
            fmt, campos = {"horizontalAlignment": alin}, ["horizontalAlignment"]
            if tipo:
                fmt["numberFormat"] = {"type": tipo, "pattern": patron}
                campos.append("numberFormat")
            req.append({"repeatCell": {"range": rango(sid, 1, filas, c, c + 1), "cell": {"userEnteredFormat": fmt},
                                       "fields": "userEnteredFormat(" + ",".join(campos) + ")"}})
        for c in cfg.get("recortar", []):
            req.append({"repeatCell": {"range": rango(sid, 1, filas, c, c + 1),
                                       "cell": {"userEnteredFormat": {"wrapStrategy": "CLIP"}},
                                       "fields": "userEnteredFormat.wrapStrategy"}})
        req.append({"setBasicFilter": {"filter": {"range": rango(sid, 0, filas, 0, n)}}})
        idx = restantes.get(nombre, 0)
        for regla in reglas_extra(nombre, sid, filas, n):
            req.append({"addConditionalFormatRule": {"index": idx, "rule": regla}})
            idx += 1
        for c, valores in cfg.get("listas", {}).items():
            req.append({"setDataValidation": {"range": rango(sid, 1, filas, c, c + 1), "rule": {
                "condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": v} for v in valores]},
                "showCustomUi": True, "strict": False}}})
        for c in cfg.get("formulas", []):
            req.append({"addProtectedRange": {"protectedRange": {
                "range": rango(sid, 1, filas, c, c + 1), "description": "Fórmula: se calcula sola, no editar",
                "warningOnly": True}}})

    # 3) panel Resumen
    if "Resumen" in hojas and pos:
        sid = hojas["Resumen"]["properties"]["sheetId"]
        total = pos["links_fin"]
        req.append({"unmergeCells": {"range": {"sheetId": sid}}})
        req.append({"repeatCell": {"range": {"sheetId": sid}, "cell": {}, "fields": "userEnteredFormat"}})
        for c, px in enumerate([270, 150, 150, 170, 190]):
            req.append({"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": c, "endIndex": c + 1},
                                                      "properties": {"pixelSize": px}, "fields": "pixelSize"}})
        req.append({"updateSheetProperties": {"properties": {"sheetId": sid, "gridProperties": {"hideGridlines": True, "frozenRowCount": 0}},
                                              "fields": "gridProperties.hideGridlines,gridProperties.frozenRowCount"}})
        req.append({"repeatCell": {"range": rango(sid, 0, total + 2, 0, 5), "cell": {"userEnteredFormat": {
            "textFormat": {"fontSize": 11}, "verticalAlignment": "MIDDLE"}},
            "fields": "userEnteredFormat(textFormat,verticalAlignment)"}})
        req.append({"mergeCells": {"range": rango(sid, 0, 1, 0, 5), "mergeType": "MERGE_ALL"}})
        req.append({"repeatCell": {"range": rango(sid, 0, 1, 0, 5), "cell": {"userEnteredFormat": {
            "backgroundColor": rgb(VERDE), "horizontalAlignment": "CENTER", "textFormat": {
                "bold": True, "fontSize": 16, "foregroundColor": rgb("#FFFFFF")}}},
            "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,textFormat)"}})
        req.append({"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "ROWS", "startIndex": 0, "endIndex": 1},
                                                  "properties": {"pixelSize": 48}, "fields": "pixelSize"}})
        req.append({"repeatCell": {"range": rango(sid, pos["fecha"], pos["fecha"] + 1, 1, 2), "cell": {"userEnteredFormat": {
            "numberFormat": {"type": "DATE_TIME", "pattern": "dd/mm/yyyy hh:mm"}, "horizontalAlignment": "LEFT"}},
            "fields": "userEnteredFormat(numberFormat,horizontalAlignment)"}})
        for clave in ("h_rev", "h_dep", "h_pie", "h_vinos", "h_ir"):
            req.append({"repeatCell": {"range": rango(sid, pos[clave], pos[clave] + 1, 0, 5), "cell": {"userEnteredFormat": {
                "backgroundColor": rgb(VERDE_CLARO), "textFormat": {"bold": True, "foregroundColor": rgb(VERDE)}}},
                "fields": "userEnteredFormat(backgroundColor,textFormat)"}})
        for ini, fin in (("rev_ini", "rev_fin"), ("dep_ini", "dep_fin"), ("pie_ini", "pie_fin"), ("vin_ini", "vin_fin")):
            req.append({"repeatCell": {"range": rango(sid, pos[ini], pos[fin], 1, 5), "cell": {"userEnteredFormat": {
                "horizontalAlignment": "CENTER", "numberFormat": {"type": "NUMBER", "pattern": "#,##0"}}},
                "fields": "userEnteredFormat(horizontalAlignment,numberFormat)"}})
        req.append({"repeatCell": {"range": rango(sid, pos["dep_ini"], pos["dep_fin"], 2, 3), "cell": {"userEnteredFormat": {
            "numberFormat": {"type": "DATE", "pattern": "dd/mm/yyyy"}}}, "fields": "userEnteredFormat.numberFormat"}})
        req.append({"addConditionalFormatRule": {"index": 0, "rule": {
            "ranges": [rango(sid, pos["rev_ini"], pos["rev_fin"], 1, 2)], "booleanRule": {
                "condition": {"type": "NUMBER_GREATER", "values": [{"userEnteredValue": "0"}]},
                "format": {"backgroundColor": rgb("#FAC8C8"), "textFormat": {"bold": True}}}}}})
        req.append({"addConditionalFormatRule": {"index": 1, "rule": {
            "ranges": [rango(sid, pos["pie_ini"], pos["pie_fin"], 1, 3)], "gradientRule": {
                "minpoint": {"type": "MIN", "color": rgb("#FFFFFF")}, "maxpoint": {"type": "MAX", "color": rgb("#7BB899")}}}}})
        req.append({"repeatCell": {"range": rango(sid, pos["links"], pos["links_fin"], 0, 5), "cell": {"userEnteredFormat": {
            "horizontalAlignment": "CENTER", "textFormat": {"foregroundColor": rgb("#2563EB"), "underline": True}}},
            "fields": "userEnteredFormat(horizontalAlignment,textFormat)"}})
    return req


if __name__ == "__main__":
    sh = _get_spreadsheet()
    G.asegurar_hojas()
    PI.asegurar_hojas()
    preparar_resumen(sh)
    pos = escribir_resumen(sh)
    peticiones = construir_peticiones(sh, pos)
    for i in range(0, len(peticiones), 100):
        sh.batch_update({"requests": peticiones[i:i + 100]})
    print(f"Formato aplicado ({len(peticiones)} cambios).")
