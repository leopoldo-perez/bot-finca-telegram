"""
Importa el inventario de vinos desde el Excel de Eva a la hoja "Vinos" de Google Sheets.

  py importar_vinos.py "C:\\ruta\\Inventario Vinos EEDD.xlsx"            # simulación, no escribe nada
  py importar_vinos.py "C:\\ruta\\Inventario Vinos EEDD.xlsx" --aplicar  # escribe en Google Sheets

Requiere: pip install openpyxl (solo para este script, el bot no lo necesita).
Usa la columna "No. Botellas" como STOCK INICIAL. Las columnas de salidas históricas se ignoran.
"""

import argparse
import sys

import openpyxl

COL = {"id": 0, "tipo": 1, "nombre": 2, "do": 3, "anyo": 4, "formato": 5,
       "ubicacion": 6, "notas": 7, "botellas": 8}


def _limpio(valor) -> str:
    return "" if valor is None else str(valor).strip()


def leer_excel(ruta: str, hoja: str | None = None) -> tuple[list[dict], list[str]]:
    wb = openpyxl.load_workbook(ruta, data_only=True)
    ws = wb[hoja] if hoja else wb.worksheets[0]
    vinos, avisos = [], []
    for n, fila in enumerate(ws.iter_rows(min_row=1, values_only=True), start=1):
        if not isinstance(fila[COL["id"]], (int, float)):
            continue
        v = {k: fila[i] if i < len(fila) else None for k, i in COL.items()}
        vid = int(v["id"])

        anyo = v["anyo"] if isinstance(v["anyo"], (int, float)) else None
        if v["anyo"] not in (None, "", "-") and anyo is None:
            avisos.append(f"Fila {n} (ID {vid}): año no numérico {v['anyo']!r}, se deja vacío")
        formato = v["formato"] if isinstance(v["formato"], (int, float)) else None
        if formato is None:
            avisos.append(f"Fila {n} (ID {vid}) {_limpio(v['nombre'])}: sin formato (litros)")

        botellas = v["botellas"]
        if isinstance(botellas, (int, float)):
            stock = int(botellas)
        else:
            stock = 0
            avisos.append(f"Fila {n} (ID {vid}) {_limpio(v['nombre'])}: botellas = {botellas!r} "
                          f"-> stock inicial 0 (revisar)")

        vinos.append({
            "id": vid, "tipo": _limpio(v["tipo"]), "nombre": _limpio(v["nombre"]),
            "do": _limpio(v["do"]), "anyo": int(anyo) if anyo else "",
            "formato": formato if formato is not None else "",
            "ubicacion": _limpio(v["ubicacion"]), "notas": _limpio(v["notas"]),
            "inicial": stock,
        })

    ids = [v["id"] for v in vinos]
    if len(ids) != len(set(ids)):
        avisos.append("Hay IDs repetidos en el Excel")
    return vinos, avisos


def escribir(vinos: list[dict]) -> None:
    from vinos import CAB_VINOS, _formula_stock, hoja_movimientos, hoja_vinos

    hoja_movimientos()  # crea la hoja de movimientos con su formato si no existe
    ws = hoja_vinos()
    if len(ws.get_all_values()) > 1:
        sys.exit("La hoja 'Vinos' ya tiene datos. No se sobrescribe. Borra su contenido a mano si quieres reimportar.")

    filas = [CAB_VINOS]
    for i, v in enumerate(vinos, start=2):
        filas.append([v["id"], v["tipo"], v["nombre"], v["do"], v["anyo"], v["formato"],
                      v["ubicacion"], v["notas"], v["inicial"], _formula_stock(i)])
    ws.update(values=filas, range_name="A1", value_input_option="USER_ENTERED")
    print(f"Escritos {len(vinos)} vinos en la hoja 'Vinos'.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("excel")
    ap.add_argument("--hoja", help="Nombre de la hoja del Excel (por defecto, la primera)")
    ap.add_argument("--aplicar", action="store_true", help="Escribe en Google Sheets")
    args = ap.parse_args()

    vinos, avisos = leer_excel(args.excel, args.hoja)
    print(f"Vinos leídos: {len(vinos)}  |  botellas totales: {sum(v['inicial'] for v in vinos)}")
    for a in avisos:
        print("AVISO:", a)

    if args.aplicar:
        escribir(vinos)
    else:
        print("\nSimulación: no se ha escrito nada. Añade --aplicar para escribir.")
