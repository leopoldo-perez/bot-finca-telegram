"""
Generador de reportes automáticos.

Lee los registros de Google Sheets, pide a Claude un resumen analítico
y lo guarda en la hoja "Reportes". Opcionalmente lo envía por Telegram al admin.

Uso desde terminal:
  python reporter.py --tipo mensual --mes 4 --anyo 2026
  python reporter.py --tipo semanal
  python reporter.py --tipo mensual --telegram    # envía al admin por Telegram
"""

import argparse
import asyncio
import os
from datetime import datetime, timedelta

from groq import Groq
from dotenv import load_dotenv
from pathlib import Path

from extractor import MODELO_TEXTO
from sheets import leer_registros, guardar_reporte
from workers import obtener_admin_id
from zona import ahora, hoy

load_dotenv(Path(__file__).parent / ".env")

groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])

PROMPT_REPORTE = """Eres el gestor de una finca de caza en España. Analiza los siguientes registros de partes de trabajo
y genera un informe ejecutivo claro y estructurado en español.

El informe debe incluir:
1. RESUMEN GENERAL: días trabajados por cada trabajador, horas totales, días libres
2. TAREAS PRINCIPALES: las actividades más frecuentes o relevantes del período
3. ZONAS TRABAJADAS: qué áreas de la finca han recibido atención
4. MAQUINARIA E INSUMOS: equipos utilizados con frecuencia
5. INCIDENCIAS: cualquier situación fuera de lo ordinario
6. OBSERVACIONES: algo destacable o que requiera atención del gestor

Sé conciso pero completo. Usa listas con guiones. No uses markdown con asteriscos.
"""


def _registros_a_texto(registros: list[dict]) -> str:
    if not registros:
        return "Sin registros para este período."

    lineas = []
    for r in registros:
        lineas.append(
            f"{r.get('Trabajador','')} | {r.get('Fecha','')} ({r.get('Día semana','')}) | "
            f"{r.get('Horas',0)}h | {r.get('Categoría','')}"
            + (f" | {r['Notas']}" if r.get('Notas') else "")
        )
    return "\n".join(lineas)


def _fecha_en_rango(fecha_str: str, inicio: datetime, fin: datetime) -> bool:
    try:
        f = datetime.strptime(fecha_str, "%d/%m/%Y")
        return inicio <= f <= fin
    except ValueError:
        return False


def generar_reporte(tipo: str, mes: int = None, anyo: int = None) -> tuple[str, str]:
    """
    Genera el reporte con Claude.
    Devuelve (titulo, texto_reporte).
    """
    hoy = ahora()
    mes = mes or hoy.month
    anyo = anyo or hoy.year

    if tipo == "mensual":
        registros = leer_registros(mes=mes, anyo=anyo)
        titulo = f"Reporte mensual {mes:02d}/{anyo}"

    elif tipo == "semanal":
        lunes = hoy - timedelta(days=hoy.weekday() + 7)
        domingo = lunes + timedelta(days=6)
        todos = leer_registros(mes=lunes.month, anyo=lunes.year)
        registros = [r for r in todos if _fecha_en_rango(r.get("Fecha", ""), lunes, domingo)]
        titulo = f"Reporte semanal {lunes.strftime('%d/%m')} - {domingo.strftime('%d/%m/%Y')}"

    else:
        raise ValueError(f"Tipo desconocido: {tipo}")

    datos = _registros_a_texto(registros)
    contexto = f"Período: {titulo}\nTotal registros: {len(registros)}\n\n{datos}"

    response = groq_client.chat.completions.create(
        model=MODELO_TEXTO,
        messages=[
            {"role": "system", "content": PROMPT_REPORTE},
            {"role": "user", "content": contexto}
        ],
        temperature=0.3,
        max_tokens=1500,
    )
    reporte = response.choices[0].message.content.strip()

    guardar_reporte(titulo=titulo, contenido=reporte)

    print(f"\n{'='*60}\n{titulo}\n{'='*60}")
    print(reporte)
    print('='*60)

    return titulo, reporte


async def _enviar_telegram(titulo: str, reporte: str) -> None:
    """Envía el reporte al administrador por Telegram."""
    from telegram import Bot

    admin_id = obtener_admin_id()
    if not admin_id:
        print("ADMIN_ID no configurado en workers.py. Reporte no enviado por Telegram.")
        return

    bot = Bot(token=os.environ["TELEGRAM_BOT_TOKEN"])
    mensaje = f"*{titulo}*\n\n{reporte}"

    # Telegram tiene límite de 4096 chars por mensaje
    for i in range(0, len(mensaje), 4000):
        await bot.send_message(chat_id=admin_id, text=mensaje[i:i+4000])

    print(f"Reporte enviado al administrador por Telegram.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Genera reportes de partes de trabajo")
    parser.add_argument("--tipo", choices=["mensual", "semanal"], default="mensual")
    parser.add_argument("--mes", type=int, help="Mes (1-12)")
    parser.add_argument("--anyo", type=int, help="Año (ej: 2026)")
    parser.add_argument("--telegram", action="store_true", help="Enviar al admin por Telegram")
    args = parser.parse_args()

    titulo, reporte = generar_reporte(tipo=args.tipo, mes=args.mes, anyo=args.anyo)

    if args.telegram:
        asyncio.run(_enviar_telegram(titulo, reporte))
