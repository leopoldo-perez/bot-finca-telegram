"""
Agente extractor: usa Groq (Llama) para parsear mensajes de Telegram (texto o imagen)
y convertirlos en registros estructurados de partes de trabajo.
"""

import base64
import difflib
import json
import os
import re
import unicodedata
from collections import defaultdict
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

from groq import Groq
from dotenv import load_dotenv
from zona import ahora, hoy

load_dotenv(Path(__file__).parent / ".env")

client = Groq(api_key=os.environ["GROQ_API_KEY"])

# Modelos de Groq. Se pueden cambiar con variables de entorno sin tocar el código.
MODELO_TEXTO = os.environ.get("GROQ_MODELO_TEXTO", "openai/gpt-oss-120b")
MODELO_VISION = os.environ.get("GROQ_MODELO_VISION", "qwen/qwen3.8-27b")

CATEGORIAS = [
    "LIBRANZA", "FORMACION", "OTROS", "REPARACIONES", "ATT. FAMILIA",
    "MEDICO", "LIMPIEZA NAVE", "PINTAR",
    "ABONAR ALMENDROS", "CARGAR Y RECOGER ALMENDRAS", "DESBROZAR ALMENDROS",
    "FUMIGAR ALMENDROS", "GRADAR ALMENDROS", "PLANTAR ALMENDROS",
    "PODAR ALMENDROS", "PONER PAJA ALMENDROS", "REVISIÓN ALMENDROS",
    "QUITAR PLASTICOS Y HIERROS", "RIEGOS Y POZO", "DESTOCONAR",
    "CASA - COCINAR", "CASA - LAVANDERIA", "CASA - LIMPIEZA",
    "CASA - CRISTALES", "CASA - PLANCHA", "CASA - REPONER LEÑA",
    "JARDIN - JARDINERIA", "JARDIN - REGAR HUERTA", "JARDIN - PODAR",
    "CAMPO- RECOGER PIÑAS", "CAMPO - CORTAR LEÑA", "CAMPO - DESBROZAR CORTADEROS",
    "VEHICULOS - LAVADO", "VEHICULOS - OTROS",
    "MONTERIA", "CAZA", "PIENSO ANIMALES", "RECOGER DESMOGUES", "REVISIÓN MALLA",
    "ARAR", "SEMBRAR", "PASAR CUCHILLA", "TAPAR SEMILLA",
]

SYSTEM_PROMPT = """Eres un asistente especializado en extraer datos de partes de trabajo de una finca de caza en España.

Analiza el mensaje y devuelve ÚNICAMENTE un JSON válido (sin markdown, sin explicaciones, sin ```).
El resultado es SIEMPRE un array. Devuelve UNA entrada por tarea realizada (no una por día).
Un mismo día puede generar varias entradas si hay varias tareas con horas distintas.

Esquema de cada objeto:
{
  "trabajador": "nombre del trabajador",
  "dia_semana": "LUNES/MARTES/MIÉRCOLES/JUEVES/VIERNES/SÁBADO/DOMINGO",
  "fecha": "YYYY-MM-DD",
  "horas": número decimal (ej: 3.5),
  "categoria": "categoría exacta de la lista",
  "notas": "detalle adicional o null"
}

Categorías válidas:
""" + "\n".join(f"- {c}" for c in CATEGORIAS) + """

Reglas:
- Una entrada por tarea (un día puede tener varias)
- "libre" o "LIBRANZA" → categoria="LIBRANZA", horas=0
- Si no encaja en ninguna categoría → "OTROS"
- Los trabajadores cuentan trabajo YA HECHO: un día de la semana sin más ("el sábado", "el lunes") es el más
  reciente que ya ha pasado (o hoy), NUNCA una fecha futura.
- No inventes datos. Devuelve SOLO el JSON sin markdown ni explicaciones."""


def _extra_modelo(modelo: str) -> dict:
    return {"reasoning_effort": "low"} if modelo.startswith("openai/gpt-oss") else {}


def extraer_partes(
    trabajador: str,
    texto: str = "",
    imagen_bytes: Optional[bytes] = None,
    fecha_referencia: Optional[date] = None
) -> list[dict]:

    if fecha_referencia is None:
        fecha_referencia = hoy()

    from datetime import timedelta
    dias_es = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
    hoy_nombre = dias_es[fecha_referencia.weekday()]
    ayer = fecha_referencia - timedelta(days=1)
    ayer_nombre = dias_es[ayer.weekday()]

    contexto = (
        f"Hoy es {hoy_nombre} {fecha_referencia.strftime('%d/%m/%Y')}.\n"
        f"Ayer fue {ayer_nombre} {ayer.strftime('%d/%m/%Y')}.\n"
        f"Trabajador: {trabajador}\n\n"
    )

    if texto:
        contexto += f"Mensaje:\n{texto}"
    elif imagen_bytes:
        contexto += "Extrae todos los días que aparecen en la imagen adjunta."
    else:
        raise ValueError("Se necesita al menos texto o imagen")

    if imagen_bytes:
        imagen_b64 = base64.standard_b64encode(imagen_bytes).decode("utf-8")
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": SYSTEM_PROMPT + "\n\n" + contexto},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{imagen_b64}"}}
            ]
        }]
        modelo = MODELO_VISION
    else:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": contexto}
        ]
        modelo = MODELO_TEXTO

    response = client.chat.completions.create(
        model=modelo,
        messages=messages,
        temperature=0.1,
        max_tokens=6000,
        **_extra_modelo(modelo),
    )

    raw = (response.choices[0].message.content or "").strip()
    if not raw:
        raise ValueError("La IA no ha devuelto respuesta (¿se quedó sin espacio?). Inténtalo de nuevo.")

    # Limpiar backticks si el modelo los añade
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    partes = json.loads(raw)
    if not isinstance(partes, list):
        raise ValueError("La IA no ha devuelto una lista de tareas.")

    return validar_partes([p for p in partes if isinstance(p, dict)], trabajador)


DIAS_SEMANA = ["LUNES", "MARTES", "MIÉRCOLES", "JUEVES", "VIERNES", "SÁBADO", "DOMINGO"]


def _norm(texto: str) -> str:
    t = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", t)


_CAT_NORM = {_norm(c): c for c in CATEGORIAS}


def validar_partes(partes: list[dict], trabajador: str) -> list[dict]:
    """Normaliza lo que devuelve la IA: la identidad del trabajador la da Telegram (no el texto),
    el día de la semana se calcula por código y categoría/horas/fecha se comprueban.
    Lo dudoso se anota en p['_avisos'] (el bot lo enseña en la ficha de confirmación)."""
    hoy_ = hoy()
    horas_dia: dict[date, float] = defaultdict(float)
    primera_fila: dict[date, dict] = {}
    for p in partes:
        avisos: list[str] = []

        try:
            f = datetime.strptime(str(p.get("fecha")), "%Y-%m-%d").date()
        except ValueError:
            f = None
        if f is None:
            f = hoy_
            avisos.append("fecha no reconocida (he puesto hoy)")
        elif f > hoy_ + timedelta(days=1):
            if f - timedelta(days=7) <= hoy_:
                f -= timedelta(days=7)
                avisos.append(f"la fecha salía en el futuro; la he cambiado a {f:%d/%m/%Y}")
            else:
                avisos.append(f"fecha futura ({f:%d/%m/%Y})")
        elif f < hoy_ - timedelta(days=45):
            avisos.append(f"fecha muy antigua ({f:%d/%m/%Y})")

        cat = str(p.get("categoria") or "").strip()
        c = _CAT_NORM.get(_norm(cat))
        if not c:
            cerca = difflib.get_close_matches(_norm(cat), list(_CAT_NORM), n=1, cutoff=0.8)
            c = _CAT_NORM[cerca[0]] if cerca else None
        if not c:
            avisos.append(f'categoría "{cat}" no reconocida (he puesto OTROS)')
            c = "OTROS"

        m_h = re.search(r"\d+(?:[.,]\d+)?", str(p.get("horas")))
        h = float(m_h.group().replace(",", ".")) if m_h else None
        if h is None or h < 0:
            avisos.append("horas no reconocidas (he puesto 0)")
            h = 0.0
        elif h > 16:
            avisos.append(f"{h:g} horas en una sola tarea")
        horas_dia[f] += h
        primera_fila.setdefault(f, p)

        notas = str(p.get("notas") or "").strip()
        p.update({
            "trabajador": trabajador,
            "fecha": f.isoformat(),
            "dia_semana": DIAS_SEMANA[f.weekday()],
            "categoria": c,
            "horas": int(h) if h == int(h) else h,
            "notas": notas or None,
            "timestamp_registro": ahora().isoformat(),
            "_avisos": avisos,
        })

    for f, total in horas_dia.items():
        if total > 16:
            primera_fila[f]["_avisos"].append(f"{total:g} horas en total el {f:%d/%m/%Y}")
    return partes


PROMPT_VINOS = """Extraes movimientos de inventario de vinos/bebidas de una casa de campo en España.
Devuelve ÚNICAMENTE un array JSON válido (sin markdown ni explicaciones). Una entrada por vino distinto.

Esquema de cada objeto:
{
  "vino": "nombre del vino tal como lo dice el usuario, SIN año ni cantidad",
  "anyo": año de cosecha (entero) o null,
  "formato": litros de la botella (0.75 normal, 1.5 magnum) o null si no se dice,
  "ubicacion": ubicación solo si se menciona (ej. "Armario 3", "Vinoteca"), si no null,
  "cantidad": entero > 0,
  "unidad": "botellas" o "cajas",
  "tipo": "salida" o "entrada",
  "motivo": breve motivo (cena, fin de semana, rotura...) o null,
  "quien": quién ha consumido/recibido/aportado el vino si se menciona (ej. "Tío Polo y 2 amigos", "familia"), si no null,
  "fecha": "YYYY-MM-DD"
}

Reglas:
- "salida": se bebió, se consumió, se gastó, se sacó, se llevaron, se regaló, se rompió.
- "entrada": llegan, entran, se recibe, pedido, compra, se repone.
- Si el mensaje no deja claro si es entrada o salida, o falta la cantidad, NO inventes: devuelve [].
- Si no hay fecha, usa la de hoy. No inventes datos."""


def extraer_movimientos_vino(texto: str, fecha_referencia: Optional[date] = None) -> list[dict]:
    """Extrae movimientos de vino de un mensaje. Devuelve la lista cruda del LLM."""
    from datetime import timedelta

    if fecha_referencia is None:
        fecha_referencia = hoy()
    ayer = fecha_referencia - timedelta(days=1)
    contexto = (
        f"Hoy es {fecha_referencia.strftime('%d/%m/%Y')} (ISO {fecha_referencia.isoformat()}).\n"
        f"Ayer fue {ayer.isoformat()}.\n\nMensaje:\n{texto}"
    )
    response = client.chat.completions.create(
        model=MODELO_TEXTO,
        messages=[
            {"role": "system", "content": PROMPT_VINOS},
            {"role": "user", "content": contexto},
        ],
        temperature=0,
        max_tokens=1024,
    )
    raw = response.choices[0].message.content.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    try:
        datos = json.loads(raw.strip())
    except json.JSONDecodeError:
        return []
    return datos if isinstance(datos, list) else []


PROMPT_GASOIL = """Extraes registros de gasoil de una finca en España. Devuelve ÚNICAMENTE un array JSON válido
(sin markdown ni explicaciones). Un objeto por cada registro. Hay tres tipos:

1) Repostaje de un vehículo (se ha llenado de gasoil/gasolina en gasolinera):
{"tipo":"repostaje","vehiculo":"nombre tal como lo dice el usuario","litros":número,
 "contador":kilómetros u horas que marca el vehículo en ese momento (número) o null,
 "lleno":true/false/null (false solo si dice que no se llenó del todo),"importe":euros o null,"fecha":"YYYY-MM-DD"}

2) Entrada de gasoil en un depósito de la finca (alguien lo ha llenado/rellenado):
{"tipo":"entrada","deposito":"nombre del sitio, ej. casa grande, casa labor, tractor, casa pequeña",
 "numero_deposito":1, 2, "todos" o null,"es_total":true/false,"litros":número,"fecha":"YYYY-MM-DD"}

3) Lectura de un depósito (lo que hay ahora según el medidor):
{"tipo":"lectura", ...mismos campos que la entrada...}

Reglas:
- "a cada depósito", "en cada uno", "en los dos" -> numero_deposito "todos" y litros = la cantidad de CADA depósito.
- Si menciona "depósito 1" o "el primero" -> numero_deposito 1 (2 para el segundo).
- Si da un solo número para un sitio y no dice cuál depósito -> numero_deposito null.
- Si dice "en total", "entre los dos" -> es_total true.
- "Hemos llenado X y ahora hay Y" -> dos objetos: una entrada con X y una lectura con Y.
- Los números españoles pueden llevar punto de millares (128.500 = 128500) y coma decimal (47,5).
- Si falta la cantidad de litros o no es un registro de gasoil, devuelve []. No inventes datos."""


def extraer_gasoil(texto: str, fecha_referencia: Optional[date] = None) -> list[dict]:
    """Extrae repostajes, entradas y lecturas de depósitos de un mensaje (lista cruda del LLM)."""
    from datetime import timedelta

    if fecha_referencia is None:
        fecha_referencia = hoy()
    ayer = fecha_referencia - timedelta(days=1)
    contexto = (
        f"Hoy es {fecha_referencia.strftime('%d/%m/%Y')} (ISO {fecha_referencia.isoformat()}).\n"
        f"Ayer fue {ayer.isoformat()}.\n\nMensaje:\n{texto}"
    )
    response = client.chat.completions.create(
        model=MODELO_TEXTO,
        messages=[
            {"role": "system", "content": PROMPT_GASOIL},
            {"role": "user", "content": contexto},
        ],
        temperature=0,
        max_tokens=1024,
    )
    raw = response.choices[0].message.content.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    try:
        datos = json.loads(raw.strip())
    except json.JSONDecodeError:
        return []
    return datos if isinstance(datos, list) else []


PROMPT_PIENSOS = """Extraes rellenos de comederos de pienso de una finca de caza en España. Devuelve ÚNICAMENTE un array JSON
válido (sin markdown ni explicaciones). Un objeto por cada comedero mencionado:
{"comedero":"nombre tal como lo dice el usuario","cantidad":número,"unidad":"kg"|"cestas"|"sacos"|null,
 "pienso":"jabalies"|"ciervos"|"ciervos trofeo"|null,"fecha":"YYYY-MM-DD"}

Reglas:
- La cantidad es lo que se ha repuesto/echado para volver a llenar ese comedero.
- "unidad": kg/kilos -> "kg"; cestas -> "cestas" (NO las conviertas); sacos -> "sacos". Si no dice la unidad -> null.
- "pienso" solo si el mensaje lo dice (jabalíes, ciervos, ciervos trofeo / berrea); si no, null.
- Los números españoles pueden llevar punto de millares (1.200 = 1200) y coma decimal (2,5).
- Si el mensaje lista varios comederos con su cantidad, un objeto por cada uno.
- Si no hay cantidad o no habla de rellenar comederos con pienso, devuelve []. No inventes datos.
- Si no hay fecha, usa la de hoy."""


def extraer_piensos(texto: str, fecha_referencia: Optional[date] = None) -> list[dict]:
    """Extrae rellenos de comederos de un mensaje (lista cruda del LLM)."""
    if fecha_referencia is None:
        fecha_referencia = hoy()
    ayer = fecha_referencia - timedelta(days=1)
    contexto = (
        f"Hoy es {fecha_referencia.strftime('%d/%m/%Y')} (ISO {fecha_referencia.isoformat()}).\n"
        f"Ayer fue {ayer.isoformat()}.\n\nMensaje:\n{texto}"
    )
    response = client.chat.completions.create(
        model=MODELO_TEXTO,
        messages=[{"role": "system", "content": PROMPT_PIENSOS}, {"role": "user", "content": contexto}],
        temperature=0,
        max_tokens=2048,
        **_extra_modelo(MODELO_TEXTO),
    )
    raw = (response.choices[0].message.content or "").strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    try:
        datos = json.loads(raw.strip())
    except json.JSONDecodeError:
        return []
    return datos if isinstance(datos, list) else []
