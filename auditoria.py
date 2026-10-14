"""
Auditoría de integridad del Google Sheet y de la configuración (SOLO LECTURA, no cambia nada).

  py auditoria.py

Revisa que los datos tengan sentido: cabeceras, vinos, repostajes, depósitos, piensos, partes y configuración.
Muestra por gravedad (ERROR / AVISO / INFO) lo que encuentra.
"""

import sys
from collections import Counter, defaultdict
from datetime import timedelta

import gasoil as G
import piensos as PI
import vinos as V
import workers as W
from extractor import CATEGORIAS, DIAS_SEMANA
from sheets import _get_spreadsheet
from zona import hoy

hallazgos: list[tuple[str, str, str]] = []   # (gravedad, area, texto)


def anotar(grav: str, area: str, texto: str) -> None:
    hallazgos.append((grav, area, texto))


def rellenar(f: list[str], n: int) -> list[str]:
    return f + [""] * (n - len(f))


def num(x):
    try:
        return float(str(x).replace(",", ".").strip())
    except ValueError:
        return None


def fecha(x):
    return G._fecha(x)


def revisar_cabeceras(sh) -> None:
    esperadas = {
        "Registros": ["Día semana", "Fecha", "Horas", "Categoría", "Trabajador", "Notas", "Timestamp", "Revisar"],
        V.HOJA_VINOS: V.CAB_VINOS, V.HOJA_MOV: V.CAB_MOV, G.HOJA_VEH: G.CAB_VEH, G.HOJA_REP: G.CAB_REP,
        G.HOJA_DEP: G.CAB_DEP, G.HOJA_GMOV: G.CAB_GMOV, PI.HOJA_COM: PI.CAB_COM, PI.HOJA_REL: PI.CAB_REL,
    }
    existentes = {w.title: w for w in sh.worksheets()}
    for nombre, cab in esperadas.items():
        if nombre not in existentes:
            anotar("ERROR", "estructura", f"Falta la pestaña {nombre}")
            continue
        real = [c.strip() for c in existentes[nombre].row_values(1)]
        if real[:len(cab)] != cab:
            anotar("ERROR", "estructura", f"{nombre}: las cabeceras no coinciden con las que espera el bot: {real[:len(cab)]}")
    if "Reportes" not in existentes:
        anotar("INFO", "estructura", "La pestaña Reportes aún no existe (se crea al generar el primer informe)")


def revisar_vinos(sh) -> None:
    vinos = V.leer_vinos()
    ids = [v["id"] for v in vinos]
    for i, c in Counter(ids).items():
        if c > 1:
            anotar("ERROR", "vinos", f"ID de vino repetido: {i}")
    for v in vinos:
        if v["dudoso"]:
            anotar("ERROR", "vinos", f"ID {v['id']} {v['nombre']}: stock inicial o formato no numérico (se cuenta como 0)")
        if v["stock"] < 0:
            anotar("AVISO", "vinos", f"ID {v['id']} {V.descripcion(v)} ({v['ubicacion']}): stock negativo ({v['stock']})")
        if not v["ubicacion"].strip():
            anotar("INFO", "vinos", f"ID {v['id']} {v['nombre']}: sin ubicación")
        if not v["tipo"].strip():
            anotar("INFO", "vinos", f"ID {v['id']} {v['nombre']}: sin tipo")
    claves = defaultdict(list)
    for v in vinos:
        claves[(V.normalizar(v["nombre"]).strip(), v["anyo"], v["formato"], V.normalizar(v["ubicacion"]).strip())].append(v["id"])
    for k, lst in claves.items():
        if len(lst) > 1:
            anotar("AVISO", "vinos", f"Posibles filas duplicadas (mismo vino, año, formato y ubicación): IDs {', '.join(lst)} -> {k[0]}")
    por_id = {v["id"]: v for v in vinos}
    for f in sh.worksheet(V.HOJA_VINOS).get("A2:J500"):
        f = rellenar(f, 10)
        v = por_id.get(f[0].strip())
        if v and num(f[9]) is not None and int(num(f[9])) != v["stock"]:
            anotar("ERROR", "vinos", f"ID {v['id']}: 'Stock actual' de la hoja ({f[9]}) no coincide con el cálculo del bot ({v['stock']})")
    anotar("INFO", "vinos", f"{len(vinos)} referencias, {sum(v['stock'] for v in vinos)} botellas en stock actual")

    movs = sh.worksheet(V.HOJA_MOV).get_all_values()[1:]
    n = 0
    for i, f in enumerate(movs, start=2):
        f = rellenar(f, 9)
        if not any(f[:8]):
            continue
        n += 1
        if f[7].strip() not in por_id:
            anotar("ERROR", "vinos-mov", f"Fila {i}: apunta al vino ID '{f[7]}' que no existe (no cuenta en ningún stock)")
        c = num(f[2])
        if c is None or c == 0 or c != int(c):
            anotar("ERROR", "vinos-mov", f"Fila {i}: cantidad '{f[2]}' no válida")
        if fecha(f[0]) is None:
            anotar("AVISO", "vinos-mov", f"Fila {i}: fecha '{f[0]}' no reconocida")
    anotar("INFO", "vinos-mov", f"{n} movimientos registrados")


def revisar_repostajes(sh) -> None:
    vehs = {v["nombre"]: v for v in G.leer_vehiculos()}
    por_veh = defaultdict(list)
    for i, f in enumerate(sh.worksheet(G.HOJA_REP).get_all_values()[1:], start=2):
        f = rellenar(f, 11)
        if not f[0].strip():
            continue
        if f[1] not in vehs:
            anotar("ERROR", "repostajes", f"Fila {i}: vehículo '{f[1]}' no está en la pestaña Vehiculos")
            continue
        l, c, d = num(f[2]), num(f[3]), fecha(f[0])
        if l is None or l <= 0 or c is None:
            anotar("ERROR", "repostajes", f"Fila {i}: litros o contador no numéricos ({f[2]} / {f[3]})")
            continue
        if d and d > hoy() + timedelta(days=1):
            anotar("AVISO", "repostajes", f"Fila {i}: fecha futura {f[0]}")
        por_veh[f[1]].append((d, c, i))
    for veh, lst in por_veh.items():
        lst.sort(key=lambda x: (x[0] or hoy(), x[2]))
        horas = vehs[veh]["contador"] == "horas"
        for a, b in zip(lst, lst[1:]):
            if b[1] < a[1]:
                anotar("ERROR", "repostajes", f"{veh}: el contador baja entre la fila {a[2]} ({a[1]:g}) y la {b[2]} ({b[1]:g})")
            elif b[1] == a[1]:
                anotar("AVISO", "repostajes", f"{veh}: mismo contador en las filas {a[2]} y {b[2]} (¿duplicado?)")
            elif b[1] - a[1] > G.LIMITE_TRAMO["horas" if horas else "km"]:
                anotar("AVISO", "repostajes", f"{veh}: tramo muy grande entre filas {a[2]} y {b[2]} ({b[1] - a[1]:g})")


def revisar_gasoil(sh) -> None:
    deps = {d["nombre"]: d for d in G.leer_depositos()}
    lecturas = Counter()
    for i, f in enumerate(sh.worksheet(G.HOJA_GMOV).get_all_values()[1:], start=2):
        f = rellenar(f, 7)
        if not f[0].strip():
            continue
        d = deps.get(f[1])
        if d is None:
            anotar("ERROR", "gasoil", f"Fila {i}: depósito '{f[1]}' no existe en Gasoil_Depositos (no cuenta en el panel)")
            continue
        tipo = f[2].strip().upper()
        if tipo not in ("ENTRADA", "LECTURA"):
            anotar("ERROR", "gasoil", f"Fila {i}: tipo '{f[2]}' no válido")
        l = num(f[3])
        if l is None or l < 0:
            anotar("ERROR", "gasoil", f"Fila {i}: litros '{f[3]}' no válidos")
            continue
        if tipo == "LECTURA":
            lecturas[(f[1], f[0])] += 1
            if not d["mide"]:
                anotar("AVISO", "gasoil", f"Fila {i}: lectura en {f[1]}, que está marcado como 'no se mide'")
            if d["capacidad"] and l > d["capacidad"] * 1.02:
                anotar("AVISO", "gasoil", f"Fila {i}: lectura {l:g} L supera la capacidad {d['capacidad']:g} L de {f[1]}")
    for (dep, dia), c in lecturas.items():
        if c > 1:
            anotar("ERROR", "gasoil", f"{dep}: {c} lecturas el {dia} (debería haber una)")


def revisar_piensos(sh) -> None:
    coms = {c["nombre"] for c in PI.leer_comederos()}
    for i, f in enumerate(sh.worksheet(PI.HOJA_REL).get_all_values()[1:], start=2):
        f = rellenar(f, 10)
        if not f[0].strip():
            continue
        if f[1] not in coms:
            anotar("ERROR", "piensos", f"Fila {i}: comedero '{f[1]}' no existe en la pestaña Comederos")
        kg = num(f[3])
        if kg is None or kg <= 0:
            anotar("ERROR", "piensos", f"Fila {i}: kilos '{f[3]}' no válidos")
        elif kg > PI.LIMITE_KG:
            anotar("AVISO", "piensos", f"Fila {i}: {kg:g} kg en un solo relleno")
        if f[2].strip() and f[2].strip() not in PI.PIENSOS:
            anotar("AVISO", "piensos", f"Fila {i}: pienso '{f[2]}' no es uno de {', '.join(PI.PIENSOS)}")


def revisar_partes(sh) -> None:
    cats = set(CATEGORIAS)
    horas_dia = defaultdict(float)
    vistos = Counter()
    conocidos = {n.lower() for n in W.TRABAJADORES.values()}
    for i, f in enumerate(sh.worksheet("Registros").get_all_values()[1:], start=2):
        f = rellenar(f, 7)
        if not any(f):
            continue
        vistos[tuple(f)] += 1
        d, h = fecha(f[1]), num(f[2])
        if d is None:
            anotar("ERROR", "partes", f"Fila {i}: fecha '{f[1]}' no válida")
        elif f[0].strip().upper() != DIAS_SEMANA[d.weekday()]:
            anotar("ERROR", "partes", f"Fila {i}: el día '{f[0]}' no corresponde a la fecha {f[1]} ({DIAS_SEMANA[d.weekday()]})")
        if h is None or h < 0 or h > 16:
            anotar("ERROR" if h is None else "AVISO", "partes", f"Fila {i}: horas '{f[2]}' no válidas o excesivas")
        else:
            horas_dia[(f[4], f[1])] += h
        if f[3].strip() not in cats:
            anotar("ERROR", "partes", f"Fila {i}: categoría '{f[3]}' no coincide EXACTAMENTE con la lista oficial")
        if f[4].strip().lower() not in conocidos:
            anotar("AVISO", "partes", f"Fila {i}: trabajador '{f[4]}' no está en workers.py")
    for (trab, dia), tot in horas_dia.items():
        if tot > 16:
            anotar("AVISO", "partes", f"{trab} el {dia}: {tot:g} horas en total")
    for fila, c in vistos.items():
        if c > 1:
            anotar("AVISO", "partes", f"Fila duplicada {c} veces: {fila[:5]}")
    anotar("INFO", "partes", f"{sum(vistos.values())} filas de partes")


def revisar_marcadas(sh) -> None:
    """Filas que el bot marcó con un aviso en la columna Revisar y nadie ha limpiado todavía."""
    for hoja, col in (("Registros", 7), (V.HOJA_MOV, 9), (G.HOJA_REP, 11), (G.HOJA_GMOV, 7), (PI.HOJA_REL, 10)):
        filas = [i for i, f in enumerate(sh.worksheet(hoja).get_all_values()[1:], start=2)
                 if len(f) > col and f[col].strip()]
        if filas:
            anotar("AVISO", "revisar", f"{hoja}: {len(filas)} fila(s) marcadas para revisar (filas {', '.join(map(str, filas[:8]))})")


def revisar_configuracion() -> None:
    if not W.obtener_secretaria_id():
        anotar("ERROR", "config", "SECRETARIA_ID = 0: nada llega al chat de la secretaria (se obtiene con /miid desde su Telegram)")
    elif W.SECRETARIA_EN_PRUEBA:
        anotar("AVISO", "config", "Modo prueba de Eva activo: SECRETARIA_ID es el del admin")
    prov = [n for i, n in W.TRABAJADORES.items() if 123456000 <= i < 123457000]
    if prov:
        anotar("ERROR", "config", f"Trabajadores con ID provisional (no podrán usar el bot): {', '.join(prov)}")
    if W.ADMIN_ID not in W.TRABAJADORES:
        anotar("AVISO", "config", "El admin no está en TRABAJADORES")
    for n, c in Counter(n.lower() for n in W.TRABAJADORES.values()).items():
        if c > 1:
            anotar("ERROR", "config", f"Nombre de trabajador repetido: {n}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sh = _get_spreadsheet()
    for fn in (revisar_cabeceras, revisar_vinos, revisar_repostajes, revisar_gasoil, revisar_piensos, revisar_partes, revisar_marcadas):
        try:
            fn(sh)
        except Exception as e:
            anotar("ERROR", fn.__name__, f"No se pudo completar la revisión: {e}")
    revisar_configuracion()
    orden = {"ERROR": 0, "AVISO": 1, "INFO": 2}
    for grav, area, texto in sorted(hallazgos, key=lambda h: (orden[h[0]], h[1])):
        print(f"[{grav}] {area}: {texto}")
    c = Counter(h[0] for h in hallazgos)
    print(f"\nResumen: {c['ERROR']} errores, {c['AVISO']} avisos, {c['INFO']} informativos")
