"""
Bot de Telegram para recibir los registros de la finca (partes, vinos, gasoil y piensos).
Funciona con polling: no necesita URL pública ni servidor web.

Flujo único: el usuario escribe -> el bot entiende -> enseña una FICHA con lo que va a apuntar
-> el usuario pulsa [Sí, apuntar] o [No] -> solo entonces se escribe en el Sheet y se avisa a la secretaria.
"""

import logging
import os
import re
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from dotenv import load_dotenv

from extractor import extraer_partes, extraer_movimientos_vino, extraer_gasoil, extraer_piensos
from sheets import guardar_partes, fechas_ya_registradas, borrar_partes_fecha, leer_partes_enviados
from workers import (TRABAJADORES, obtener_trabajador, obtener_admin_id, obtener_secretaria_id,
                     puede_gestionar_vinos, puede_ver_resumen)
import vinos as V
import gasoil as G
import piensos as P
import workers as _workers
from zona import ahora, hoy

load_dotenv(Path(__file__).parent / ".env")

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)   # sus avisos incluyen el token del bot en la URL
logger = logging.getLogger(__name__)

ES_VINO = re.compile(r"\b(vino|vinos|botella|botellas|cava|champ[aá][nñ]|magnum|caja|cajas)\b", re.I)
ES_GASOIL = re.compile(r"\b(gasoil|gas[oó]leo|di[eé]sel|litros?|dep[oó]sitos?|repost\w*|km|kil[oó]metros?|lector)\b", re.I)
ES_PIENSO = re.compile(r"\b(pienso|piensos|comederos?|cestas?|silos?|rellen\w+|kg|kilos?)\b", re.I)
SALUDO = re.compile(r"^\s*(hola+|buenas|buenos d[ií]as|buenas tardes|buenas noches|hey|ey|men[uú]|ayuda|inicio|opciones)\W*$", re.I)
ETIQUETA = {"parte": "Parte de trabajo", "vino": "Vinos", "repostaje": "Repostaje", "deposito": "Gasoil", "pienso": "Piensos"}


# ── Utilidades ────────────────────────────────────

def _resumen_partes(partes: list[dict]) -> str:
    por_dia: dict[str, list] = {}
    for p in partes:
        por_dia.setdefault(p.get("fecha", "?"), []).append(p)
    lineas = []
    for fecha, tareas in sorted(por_dia.items()):
        horas_total = sum(t.get("horas") or 0 for t in tareas)
        cats = ", ".join(t.get("categoria", "") for t in tareas)
        lineas.append(f"  {fecha}: {horas_total}h — {cats}")
    return "\n".join(lineas)


def _n(x) -> str:
    if x is None:
        return ""
    x = float(x)
    if x.is_integer():
        return f"{int(x):,}".replace(",", ".")
    return f"{x:.1f}".replace(".", ",")


def _f_num(x):
    try:
        return float(str(x).replace(",", ".").strip())
    except ValueError:
        return None


def _trabajador_o_aviso(update: Update) -> str | None:
    return obtener_trabajador(update.effective_user.id)


async def _no_registrado(update: Update) -> None:
    await update.message.reply_text(
        f"No estás registrado en el sistema.\n"
        f"Tu ID de Telegram es: {update.effective_user.id}\n"
        "Comunícaselo al administrador."
    )


def _es_secretaria(user_id: int) -> bool:
    sec = obtener_secretaria_id()
    return bool(sec) and user_id == sec


def _puede_vinos(user_id: int) -> bool:
    return puede_gestionar_vinos(user_id)


def _puede_gasoil(user_id: int) -> bool:
    return obtener_trabajador(user_id) is not None


def _puede_piensos(user_id: int) -> bool:
    return obtener_trabajador(user_id) is not None


async def _enviar(context: ContextTypes.DEFAULT_TYPE, update: Update, texto: str, markup=None) -> None:
    await context.bot.send_message(chat_id=update.effective_chat.id, text=texto, reply_markup=markup)


def _boton(texto: str, data: str) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(texto, callback_data=data)]


# ── Avisos a la secretaria y al administrador ───────────────

_ultimo_fallo_aviso = 0.0


async def _notificar_admin(context: ContextTypes.DEFAULT_TYPE, mensaje: str) -> None:
    admin_id = obtener_admin_id()
    if admin_id:
        try:
            await context.bot.send_message(chat_id=admin_id, text=f"⚠️ {mensaje}")
        except Exception as e:
            logger.warning(f"No se pudo avisar al administrador: {e}")


async def _notificar_secretaria(context: ContextTypes.DEFAULT_TYPE, mensaje: str, excluir_id: int | None = None) -> None:
    """Envía un aviso al chat de la secretaria. Si falla, avisa al admin (máx. una vez cada 30 min)."""
    global _ultimo_fallo_aviso
    sec_id = obtener_secretaria_id()
    if not sec_id or (sec_id == excluir_id and not _workers.SECRETARIA_EN_PRUEBA):
        return
    try:
        await context.bot.send_message(chat_id=sec_id, text=mensaje[:4000])
    except Exception as e:
        logger.warning(f"No se pudo avisar a la secretaria: {e}")
        if time.time() - _ultimo_fallo_aviso > 1800:
            _ultimo_fallo_aviso = time.time()
            await _notificar_admin(context, "No he podido enviar un aviso al chat de la secretaria "
                                            f"({e}). ¿Ha pulsado /start en el bot?")


async def _no_registrado_aviso(context: ContextTypes.DEFAULT_TYPE, user_id: int, trabajador: str,
                               motivo: str, original: str = "") -> None:
    """Cuando algo NO se puede registrar, la secretaria también lo sabe (todo pasa por su chat)."""
    texto = f"NO se ha registrado - mensaje de {trabajador}: {motivo}"
    if original:
        texto += f"\n\nMensaje original: {original[:400]}"
    await _notificar_secretaria(context, texto, excluir_id=user_id)


async def _reenviar_foto(context: ContextTypes.DEFAULT_TYPE, user_id: int, trabajador: str, file_id: str | None) -> None:
    sec = obtener_secretaria_id()
    if file_id and sec and (sec != user_id or _workers.SECRETARIA_EN_PRUEBA):
        try:
            await context.bot.send_photo(chat_id=sec, photo=file_id, caption=f"Foto enviada por {trabajador}")
        except Exception as e:
            logger.warning(f"No se pudo reenviar la foto a la secretaria: {e}")


# ── Ficha de confirmación (común a todo) ──────────
#
# Cada módulo prepara "operaciones" (op): {"area", "linea", "avisos", "commit", "commit_rep"?}.
# Nada se escribe hasta que el usuario pulsa [Sí, apuntar].

def _nueva_ficha(context: ContextTypes.DEFAULT_TYPE) -> dict:
    ficha = {"ops": [], "rechazos": [], "cancelados": 0, "dup": None}
    context.user_data["ficha"] = ficha
    return ficha


def _ficha(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.user_data.get("ficha") or _nueva_ficha(context)


async def _mostrar_ficha(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ficha = context.user_data.pop("ficha", None)
    if not ficha:
        return
    user_id = update.effective_user.id
    usuario = obtener_trabajador(user_id) or "?"
    origen = context.user_data.get("origen", "")
    ops, rechazos = ficha["ops"], ficha["rechazos"]

    if not ops:
        if rechazos:
            await _enviar(context, update, "No he apuntado nada:\n- " + "\n- ".join(rechazos))
            await _no_registrado_aviso(context, user_id, usuario, "; ".join(rechazos), origen)
        elif ficha["cancelados"]:
            await _enviar(context, update, "Vale, no se ha apuntado nada.")
        return

    avisos = list(dict.fromkeys(a for op in ops for a in op["avisos"]))
    texto = "Esto es lo que he entendido:\n" + "\n".join(op["linea"] for op in ops)
    if avisos:
        texto += "\n\nOjo: " + "; ".join(avisos) + "."
    if rechazos:
        texto += "\n\nNo he podido apuntar:\n- " + "\n- ".join(rechazos)
    pid = uuid.uuid4().hex[:6]
    if ficha["dup"]:
        texto += (f"\n\nYa hay datos tuyos de: {', '.join(ficha['dup'])}. ¿Qué hago?\n"
                  "• Reemplazar: borra lo anterior de esa fecha y apunta esto\n"
                  "• Añadir: lo suma a lo que ya había")
        botones = [_boton("Reemplazar lo anterior", f"ficha:rep:{pid}"), _boton("Añadir", f"ficha:ok:{pid}"),
                   _boton("No", f"ficha:no:{pid}")]
        teclado = InlineKeyboardMarkup([b[0:1] for b in botones])
    else:
        texto += "\n\n¿Es correcto?"
        teclado = InlineKeyboardMarkup([[InlineKeyboardButton("Sí, apuntar", callback_data=f"ficha:ok:{pid}"),
                                         InlineKeyboardButton("No", callback_data=f"ficha:no:{pid}")]])
    context.user_data["ficha_pend"] = {"pid": pid, "ops": ops, "origen": origen, "avisos": avisos,
                                       "foto": context.user_data.get("parte_foto"), "rechazos": rechazos}
    await _enviar(context, update, texto, teclado)


async def callback_ficha(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    trabajador = obtener_trabajador(uid)
    if trabajador is None:
        return
    _, accion, pid = query.data.split(":")
    pend = context.user_data.get("ficha_pend")
    if not pend or pend["pid"] != pid:
        await query.edit_message_text("Esta pregunta ya no está activa. Vuelve a enviar el mensaje.")
        return
    context.user_data.pop("ficha_pend")
    if accion == "no":
        await query.edit_message_text("Vale, no se ha apuntado nada. Escríbelo otra vez como quieras.")
        return

    resultados, errores = [], []
    for op in pend["ops"]:
        fn = op["commit_rep"] if accion == "rep" and op.get("commit_rep") else op["commit"]
        try:
            resultados.append(fn() or op["linea"])
        except Exception as e:
            logger.error(f"Error al apuntar: {e}")
            errores.append(f"{op['linea']} ({e})")

    texto = ("Apuntado:\n" + "\n".join(resultados)) if resultados else "No se ha podido apuntar."
    if errores:
        texto += "\n\nHa fallado, no se ha apuntado:\n- " + "\n- ".join(errores)
    await query.edit_message_text(texto)

    areas = ", ".join(dict.fromkeys(ETIQUETA[op["area"]] for op in pend["ops"]))
    if resultados:
        if any(op["area"] == "parte" for op in pend["ops"]):
            await _reenviar_foto(context, uid, trabajador, pend["foto"])
        aviso = f"{areas} - {trabajador}:\n" + "\n".join(resultados)
        if pend["avisos"]:
            aviso += "\nOJO: " + "; ".join(pend["avisos"])
        if pend["origen"]:
            aviso += f"\n\nMensaje original: {pend['origen'][:400]}"
        await _notificar_secretaria(context, aviso, excluir_id=uid)
        if pend["avisos"]:
            await _notificar_admin(context, f"Apuntado con avisos ({areas}, {trabajador}): " + "; ".join(pend["avisos"]))
    if errores:
        await _notificar_admin(context, f"Error al apuntar ({areas}, {trabajador}): " + "; ".join(errores))
        await _no_registrado_aviso(context, uid, trabajador, "error técnico al apuntar: " + "; ".join(errores), pend["origen"])


# ── Comandos básicos y menú ───────────────────────

MODOS = {
    "parte": ("Escribe tu parte de trabajo o mándalo en foto. Ejemplo:\n"
              "Hoy 9-18h\n- Riego almendros con cuba\n- Revisión malla zona norte"),
    "repostaje": ("Escribe el repostaje: vehículo, litros y lo que marca el contador. Ejemplo:\n"
                  "Ranger, 47 litros, 128500 km"),
    "deposito": ("Escribe lo que ha entrado o lo que marca el lector de los depósitos. Ejemplos:\n"
                 "Hemos llenado 1000 litros en cada depósito de la casa grande\n"
                 "Hoy el lector marca 1500 en cada depósito de la casa principal"),
    "pienso": ("Escribe lo que has rellenado en los comederos (indica siempre kilos o cestas). Ejemplo:\n"
               "Rellenado: Cuernico 10 cestas, Laguna 240 kg"),
    "vino": ("Escribe el movimiento de vino. Ejemplos:\n"
             "Se han bebido 2 botellas de Contino 2020\nHan llegado 3 cajas de Marqués de Murrieta 2019"),
    "stock": "Escribe el nombre del vino que quieres consultar. Ejemplo:\nContino 2020",
}
_MODO_SEGUNDOS = 600


def _boton_menu(texto: str, accion: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(texto, callback_data=f"menu:{accion}")


def _teclado_menu(user_id: int) -> InlineKeyboardMarkup:
    filas = []
    if obtener_trabajador(user_id) is not None:
        filas += [[_boton_menu("Parte de trabajo", "parte")],
                  [_boton_menu("Repostaje de vehículo", "repostaje"), _boton_menu("Depósitos de gasoil", "deposito")],
                  [_boton_menu("Piensos (comederos)", "pienso")]]
        if _puede_vinos(user_id):
            filas.append([_boton_menu("Vinos: apuntar", "vino"), _boton_menu("Vinos: consultar", "stock")])
    filas.append([_boton_menu("Ver depósitos", "ver_dep"), _boton_menu("Ver piensos", "ver_pie")])
    if puede_ver_resumen(user_id):
        filas.append([_boton_menu("Resumen de hoy", "hoy")])
    return InlineKeyboardMarkup(filas)


def _modo_activo(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    m = context.user_data.get("modo")
    if not m:
        return None
    nombre, t = m
    if time.time() - t > _MODO_SEGUNDOS:
        context.user_data.pop("modo", None)
        return None
    return nombre


async def _responder_sin_registro(update: Update, con_teclado: bool = False) -> None:
    """Quien escribe sin ser trabajador: la secretaria recibe una explicación y su menú, el resto su ID."""
    uid = update.effective_user.id
    if _es_secretaria(uid):
        await update.message.reply_text(
            "Hola. Este chat es para recibir los avisos de la finca (partes, vinos, gasoil y piensos). "
            "Desde aquí también puedes consultar lo recibido.",
            reply_markup=(ReplyKeyboardMarkup([["Menú"]], resize_keyboard=True, is_persistent=True) if con_teclado else None))
        await update.message.reply_text("¿Qué quieres ver?", reply_markup=_teclado_menu(uid))
    else:
        await _no_registrado(update)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    trabajador = _trabajador_o_aviso(update)
    if trabajador:
        await update.message.reply_text(
            f"Hola {trabajador}. Pulsa «Menú» (abajo) para elegir qué quieres hacer, "
            "o escríbeme directamente tu parte, repostaje, vino o pienso y lo entiendo. "
            "Antes de apuntar nada te enseñaré lo que he entendido para que me digas si es correcto.",
            reply_markup=ReplyKeyboardMarkup([["Menú"]], resize_keyboard=True, is_persistent=True))
        await update.message.reply_text("¿Qué quieres hacer?", reply_markup=_teclado_menu(update.effective_user.id))
    else:
        await _responder_sin_registro(update, con_teclado=True)


async def cmd_miid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"{update.effective_user.first_name}, tu ID es: {update.effective_user.id}")


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if obtener_trabajador(uid) is None and not puede_ver_resumen(uid):
        await _no_registrado(update)
        return
    context.user_data.pop("modo", None)
    await update.message.reply_text("¿Qué quieres hacer?", reply_markup=_teclado_menu(uid))


async def callback_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    es_trabajador = obtener_trabajador(uid) is not None
    if not (es_trabajador or puede_ver_resumen(uid)):
        return
    accion = query.data.split(":")[1]
    if accion == "cancel":
        context.user_data.pop("modo", None)
        await query.edit_message_text("Vale, cancelado. Escribe «menú» cuando quieras elegir otra cosa.")
        return
    if accion in MODOS:
        if not es_trabajador or (accion in ("vino", "stock") and not _puede_vinos(uid)):
            await query.edit_message_text("No tienes permiso para esta opción.")
            return
        context.user_data["modo"] = (accion, time.time())
        await query.edit_message_text(
            MODOS[accion], reply_markup=InlineKeyboardMarkup([[_boton_menu("Cancelar", "cancel")]]))
        return
    if accion == "ver_dep":
        await _mostrar_depositos(update, context)
    elif accion == "ver_pie":
        await _mostrar_piensos(update, context)
    elif accion == "hoy" and puede_ver_resumen(uid):
        await _mostrar_hoy(update, context, "")


# ── Partes de trabajo ─────────────────────────────

async def _procesar_partes(update: Update, context: ContextTypes.DEFAULT_TYPE, partes: list[dict], trabajador: str) -> None:
    user_id = update.effective_user.id
    if not partes:
        await update.message.reply_text(
            "No he podido extraer datos. ¿Puedes revisar el mensaje? "
            "Si no estás seguro de cómo escribirlo, escribe «menú» y elige qué quieres hacer.")
        await _no_registrado_aviso(context, user_id, trabajador, "no he podido sacar ninguna tarea del mensaje",
                                   context.user_data.get("origen", ""))
        return

    for p in partes:   # YYYY-MM-DD -> DD/MM/YYYY para comparar con el Sheet
        f = p.get("fecha", "")
        try:
            if f and "-" in f:
                p["fecha"] = datetime.strptime(f, "%Y-%m-%d").strftime("%d/%m/%Y")
        except ValueError:
            pass

    previas = fechas_ya_registradas(trabajador)
    duplicados = sorted({p["fecha"] for p in partes if p["fecha"] in previas})
    resumen = _resumen_partes(partes)
    avisos = list(dict.fromkeys(a for p in partes for a in p.get("_avisos", [])))

    def commit() -> str:
        guardar_partes(partes)
        return "Parte de trabajo:\n" + resumen

    def commit_rep() -> str:
        borrar_partes_fecha(trabajador, duplicados)
        guardar_partes(partes)
        return "Parte de trabajo (sustituye lo anterior de esa fecha):\n" + resumen

    ficha = _nueva_ficha(context)
    ficha["ops"].append({"area": "parte", "linea": "Parte de trabajo:\n" + resumen, "avisos": avisos,
                         "commit": commit, "commit_rep": commit_rep})
    ficha["dup"] = duplicados or None
    await _mostrar_ficha(update, context)


async def recibir_texto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    trabajador = _trabajador_o_aviso(update)
    if not trabajador:
        await _responder_sin_registro(update)
        return

    user_id = update.effective_user.id
    texto = update.message.text.strip()
    context.user_data["origen"] = texto
    context.user_data.pop("parte_foto", None)

    if SALUDO.match(texto):
        context.user_data.pop("modo", None)
        await update.message.reply_text("¿Qué quieres hacer?", reply_markup=_teclado_menu(user_id))
        return
    modo = _modo_activo(context)
    context.user_data.pop("modo", None)
    if modo in ("vino", "stock", "repostaje", "deposito", "pienso"):
        try:
            if modo == "vino" and _puede_vinos(user_id):
                await _procesar_vino(update, context, texto, explicito=True)
            elif modo == "stock" and _puede_vinos(user_id):
                await _consultar_stock(update, context, texto)
            elif modo in ("repostaje", "deposito"):
                await _procesar_gasoil(update, context, texto, explicito=True)
            elif modo == "pienso":
                await _procesar_piensos(update, context, texto, explicito=True)
        except Exception as e:
            logger.error(f"Error en modo {modo}: {e}")
            await update.message.reply_text("Ha habido un error y no se ha podido preparar. El administrador ha sido avisado.")
            await _notificar_admin(context, f"Error en modo {modo}: {e}")
            await _no_registrado_aviso(context, user_id, trabajador, f"error técnico ({modo}), NO se ha registrado", texto)
        return
    auto = modo != "parte"

    for activo, regex, nombre, fn in (
            (_puede_vinos(user_id), ES_VINO, "vinos", _procesar_vino),
            (_puede_gasoil(user_id), ES_GASOIL, "gasoil", _procesar_gasoil),
            (_puede_piensos(user_id), ES_PIENSO, "piensos", _procesar_piensos)):
        if auto and activo and regex.search(texto):
            try:
                if await fn(update, context, texto, explicito=False):
                    return
            except Exception as e:
                logger.error(f"Error en {nombre}: {e}")
                await update.message.reply_text("Ha habido un error y no se ha podido preparar. El administrador ha sido avisado.")
                await _notificar_admin(context, f"Error en {nombre}: {e}")
                await _no_registrado_aviso(context, user_id, trabajador, f"error técnico ({nombre}), NO se ha registrado", texto)
                return

    await update.message.reply_text("Procesando tu parte...")
    try:
        partes = extraer_partes(trabajador=trabajador, texto=texto)
        await _procesar_partes(update, context, partes, trabajador)
    except Exception as e:
        logger.error(f"Error con {trabajador}: {e}")
        await update.message.reply_text("Error al procesar el parte. El administrador ha sido notificado.")
        await _notificar_admin(context, f"Error en parte de {trabajador}: {e}")
        await _no_registrado_aviso(context, user_id, trabajador, "error técnico, el parte NO se ha guardado", texto)


async def recibir_foto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    trabajador = _trabajador_o_aviso(update)
    if not trabajador:
        await _responder_sin_registro(update)
        return

    user_id = update.effective_user.id
    context.user_data.pop("modo", None)
    await update.message.reply_text("Analizando imagen...")
    foto = update.message.photo[-1]
    context.user_data["origen"] = (update.message.caption or "").strip() or "(foto)"
    context.user_data["parte_foto"] = foto.file_id
    try:
        archivo = await context.bot.get_file(foto.file_id)
        imagen_bytes = bytes(await archivo.download_as_bytearray())
        partes = extraer_partes(trabajador=trabajador, texto=update.message.caption or "", imagen_bytes=imagen_bytes)
        await _procesar_partes(update, context, partes, trabajador)
    except Exception as e:
        logger.error(f"Error foto de {trabajador}: {e}")
        await update.message.reply_text("Error al procesar la imagen.")
        await _notificar_admin(context, f"Error en imagen de {trabajador}: {e}")
        await _no_registrado_aviso(context, user_id, trabajador, "error técnico con una foto, NO se ha guardado", "(foto)")
        await _reenviar_foto(context, user_id, trabajador, foto.file_id)


async def recibir_otro(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Audios, vídeos, archivos y stickers."""
    trabajador = _trabajador_o_aviso(update)
    if not trabajador:
        if not _es_secretaria(update.effective_user.id):
            await _no_registrado(update)
        return
    await update.message.reply_text(
        "De momento solo entiendo mensajes de texto y fotos. Escríbelo, por favor: "
        "los audios y los archivos no los puedo leer.")
    await _no_registrado_aviso(context, update.effective_user.id, trabajador,
                               "ha enviado un audio, vídeo o archivo que el bot no puede leer", "")


# ── Vinos ─────────────────────────────────────────

def _linea_vino(v: dict) -> str:
    marca = " [datos por revisar]" if v.get("dudoso") else ""
    return f"{V.descripcion(v)} - {v['ubicacion'] or 'sin ubicación'} - {v['stock']} ud{marca}"


def _texto_vino(m: dict, desc: str, lugar: str, antes: int, despues: int, nuevo: bool = False,
                hecho: bool = False) -> str:
    cant = m["cantidad"]
    n = abs(cant)
    botellas = "botella" if n == 1 else "botellas"
    frase = f"han salido {n} {botellas}" if cant < 0 else f"han entrado {n} {botellas}"
    t = ("Vino NUEVO (no estaba en el inventario): " if nuevo else "Vino: ") + f"{frase} de {desc} ({lugar}). " + (f"Ahora hay {despues}." if hecho else f"Ahora hay {antes} y quedarán {despues}.")
    extras = []
    if m["motivo"]:
        extras.append(f"Motivo: {m['motivo']}")
    if m.get("quien"):
        extras.append(f"Quién: {m['quien']}")
    if extras:
        t += " " + ". ".join(extras) + "."
    if m["era_caja"]:
        t += f" (cada caja = {V.BOTELLAS_POR_CAJA} botellas)"
    return t


async def _procesar_vino(update: Update, context: ContextTypes.DEFAULT_TYPE, texto: str, explicito: bool) -> bool:
    """Devuelve True si el mensaje se ha tratado como movimiento de vino."""
    crudos = extraer_movimientos_vino(texto)
    movs = [m for m in (V.validar_movimiento(x) for x in crudos) if m]
    if not movs:
        if explicito:
            await update.message.reply_text(
                "No he entendido ningún movimiento. Ejemplo: /vino se han bebido 3 botellas de Contino 2020")
        return explicito
    if len(movs) < len(crudos):
        await update.message.reply_text(
            f"No he podido interpretar {len(crudos) - len(movs)} movimiento(s) del mensaje; revísalos.")
    if context.user_data.get("vino_pend") or context.user_data.get("vino_cola"):
        await update.message.reply_text("Tenía una pregunta anterior sin contestar; la he descartado.")
    _nueva_ficha(context)
    context.user_data["vino_cola"] = movs
    context.user_data["vino_msg"] = texto
    context.user_data["vino_delta"] = {}
    context.user_data.pop("vino_pend", None)
    await _seguir_cola(update, context)
    return True


async def _seguir_cola(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cola = context.user_data.get("vino_cola", [])
    ficha = _ficha(context)
    while cola:
        m = cola.pop(0)
        vinos = V.leer_vinos()
        estado, cands = V.resolver(m["vino"], m["anyo"], m["formato"], vinos, m["ubicacion"])
        nombre = f"{m['vino']} {m['anyo'] or ''}".strip()

        if estado == "no_encontrado":
            if m["cantidad"] > 0:
                _preparar_vino_nuevo(update, context, m)
            else:
                ficha["rechazos"].append(f'No encuentro "{nombre}" en el inventario (no se puede sacar algo que no existe)')
            continue
        if estado == "ambiguo":
            pid = uuid.uuid4().hex[:6]
            context.user_data["vino_pend"] = {"pid": pid, "mov": m}
            filas = [_boton(_linea_vino(v), f"vino:sel:{pid}:{v['id']}") for v in cands]
            filas.append(_boton("Ninguno de estos", f"vino:no:{pid}"))
            accion = "sacar" if m["cantidad"] < 0 else "añadir"
            await _enviar(context, update,
                f'¿Cuál de estos es? Quiero {accion} {abs(m["cantidad"])} ud de "{nombre}":',
                InlineKeyboardMarkup(filas))
            return
        _preparar_vino(update, context, m, cands[0])
    await _mostrar_ficha(update, context)


def _preparar_vino(update: Update, context: ContextTypes.DEFAULT_TYPE, m: dict, vino: dict) -> None:
    usuario = obtener_trabajador(update.effective_user.id) or "?"
    msg = context.user_data.get("vino_msg", "")
    delta = context.user_data.setdefault("vino_delta", {})
    antes = vino["stock"] + delta.get(vino["id"], 0)
    despues = antes + m["cantidad"]
    delta[vino["id"]] = delta.get(vino["id"], 0) + m["cantidad"]
    lugar = vino["ubicacion"] or "sin ubicación"
    desc = V.descripcion(vino)
    avisos = []
    if despues < 0:
        avisos.append(f"de {desc} ({lugar}) solo hay {antes} y el stock quedaría en {despues}")
    if abs(m["cantidad"]) > V.UMBRAL_GRANDE:
        avisos.append(f"cantidad grande ({abs(m['cantidad'])} botellas)")

    def commit() -> str:
        r = V.registrar_movimiento(vino["id"], m["cantidad"], m["fecha"], m["motivo"], usuario, msg,
                                   permitir_negativo=True, quien=m.get("quien"), revisar="; ".join(avisos))
        return _texto_vino(m, desc, lugar, r["antes"], r["despues"], hecho=True)

    _ficha(context)["ops"].append({"area": "vino", "linea": _texto_vino(m, desc, lugar, antes, despues),
                                   "avisos": avisos, "commit": commit})


def _preparar_vino_nuevo(update: Update, context: ContextTypes.DEFAULT_TYPE, m: dict) -> None:
    usuario = obtener_trabajador(update.effective_user.id) or "?"
    msg = context.user_data.get("vino_msg", "")
    desc = f"{m['vino']} {m['anyo'] or ''}".strip()
    avisos = [f'"{desc}" no está en el inventario: se creará como vino nuevo']

    def commit() -> str:
        v = V.crear_vino(m["vino"], m["anyo"], m["formato"], usuario)
        r = V.registrar_movimiento(v["id"], m["cantidad"], m["fecha"], m["motivo"], usuario, msg,
                                   permitir_negativo=True, quien=m.get("quien"), revisar="; ".join(avisos))
        return _texto_vino(m, desc, "sin ubicación", r["antes"], r["despues"], nuevo=True, hecho=True) + \
            " Completa sus datos en la hoja Vinos."

    _ficha(context)["ops"].append({"area": "vino", "linea": _texto_vino(m, desc, "sin ubicación", 0, m["cantidad"], nuevo=True),
                                   "avisos": avisos, "commit": commit})


async def callback_vino(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not _puede_vinos(query.from_user.id):
        return
    partes = query.data.split(":")
    accion = partes[1]
    pend = context.user_data.get("vino_pend")
    if not pend or pend["pid"] != partes[2]:
        await query.edit_message_text("Esta pregunta ya no está activa. Vuelve a enviar el movimiento.")
        return
    context.user_data.pop("vino_pend")
    try:
        if accion == "no":
            await query.edit_message_text("Vale, ese vino no se tendrá en cuenta.")
            _ficha(context)["cancelados"] += 1
        elif accion == "sel":
            vino = next((v for v in V.leer_vinos() if v["id"] == partes[3]), None)
            if vino is None:
                await query.edit_message_text("Ese vino ya no existe en el inventario.")
            else:
                await query.edit_message_text(f"Elegido: {_linea_vino(vino)}")
                _preparar_vino(update, context, pend["mov"], vino)
    except Exception as e:
        logger.error(f"Error en callback de vino: {e}")
        await _enviar(context, update, "Ha habido un error. Vuelve a enviar el mensaje.")
        await _notificar_admin(context, f"Error en callback de vino: {e}")
        return
    await _seguir_cola(update, context)


async def cmd_vino(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _puede_vinos(update.effective_user.id):
        await update.message.reply_text("No tienes permiso para gestionar vinos.")
        return
    texto = " ".join(context.args).strip()
    if not texto:
        await update.message.reply_text(
            "Escribe el movimiento tras el comando. Ejemplos:\n"
            "/vino se han bebido 3 botellas de Contino 2020\n"
            "/vino llegan 2 cajas de Marqués de Murrieta 2019\n\n"
            "También funciona sin comando si el mensaje habla de vino o botellas, o desde el menú.\n"
            "/stock <nombre> consulta existencias.")
        return
    context.user_data["origen"] = texto
    try:
        await _procesar_vino(update, context, texto, explicito=True)
    except Exception as e:
        logger.error(f"Error en /vino: {e}")
        await update.message.reply_text("Error al preparar el movimiento.")
        await _notificar_admin(context, f"Error en /vino: {e}")


async def _consultar_stock(update: Update, context: ContextTypes.DEFAULT_TYPE, texto: str) -> None:
    try:
        vinos = V.leer_vinos()
        if not texto:
            await _enviar(context, update,
                f"Inventario: {len(vinos)} referencias, {sum(v['stock'] for v in vinos)} botellas en total.\n"
                "Usa /stock <nombre> para consultar un vino.")
            return
        anyo = re.search(r"\b(19|20)\d{2}\b", texto)
        nombre = re.sub(r"\b(19|20)\d{2}\b", "", texto)
        res = V.buscar_candidatos(nombre, int(anyo.group()) if anyo else None, None, vinos)
        if not res:
            await _enviar(context, update, f'No encuentro "{texto}" en el inventario.')
            return
        await _enviar(context, update, "\n".join(_linea_vino(v) for _, v in res[:10]))
    except Exception as e:
        logger.error(f"Error en /stock: {e}")
        await _enviar(context, update, "Error al consultar el inventario.")


async def cmd_stock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _puede_vinos(update.effective_user.id):
        await update.message.reply_text("No tienes permiso para gestionar vinos.")
        return
    await _consultar_stock(update, context, " ".join(context.args).strip())


# ── Gasoil: repostajes y depósitos ────────────────

def _fecha_txt(fecha: date) -> str:
    return "" if fecha == hoy() else f" [fecha: {fecha.strftime('%d/%m/%Y')}]"


async def _procesar_gasoil(update: Update, context: ContextTypes.DEFAULT_TYPE, texto: str, explicito: bool) -> bool:
    """Devuelve True si el mensaje se ha tratado como registro de gasoil."""
    crudos = extraer_gasoil(texto)
    items = [i for i in (G.validar_item(x) for x in crudos) if i]
    if not items:
        if explicito:
            await update.message.reply_text(
                "No he entendido ningún registro de gasoil. Ejemplos:\n"
                "Ranger, 47 litros, 128500 km\n"
                "Hemos llenado 1000 litros en cada depósito de la casa grande\n"
                "Hoy quedan 1400 litros en el depósito del tractor")
        return explicito
    if not explicito:
        vehs, deps = G.leer_vehiculos(), G.leer_depositos()
        if not any(G.item_reconocible(i, vehs, deps) for i in items):
            return False   # no es de nuestros vehículos/depósitos: se trata como parte de trabajo
    orden = {"repostaje": 0, "entrada": 1, "lectura": 2}
    items.sort(key=lambda i: orden[i["tipo"]])
    if context.user_data.get("gas_pend") or context.user_data.get("gas_cola"):
        await update.message.reply_text("Tenía una pregunta anterior sin contestar; la he descartado.")
    _nueva_ficha(context)
    context.user_data["gas_cola"] = items
    context.user_data["gas_msg"] = texto
    context.user_data.pop("gas_pend", None)
    await _seguir_cola_gas(update, context)
    if not explicito and re.search(r"\b\d+(?:[.,]\d+)?\s*(?:h|horas?)\b", texto, re.I):
        await update.message.reply_text(
            "Si en el mismo mensaje había también tu parte de trabajo, envíalo aparte: "
            "aquí solo he preparado el gasoil.")
    return True


async def _seguir_cola_gas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cola = context.user_data.get("gas_cola", [])
    ficha = _ficha(context)
    while cola:
        it = cola.pop(0)
        if it["tipo"] != "repostaje":
            _preparar_deposito(update, context, it)
            continue
        if it["contador"] is None:
            ficha["rechazos"].append(f'repostaje de "{it["vehiculo"]}": falta lo que marca el contador (km u horas). '
                                     'Escríbelo todo en un mensaje, por ejemplo: "Ranger, 47 litros, 128500 km"')
            continue
        vehs = G.leer_vehiculos()
        estado, cands = G.resolver_vehiculo(it["vehiculo"], vehs)
        if estado == "no_encontrado":
            ficha["rechazos"].append(f'no reconozco el vehículo "{it["vehiculo"]}". Los que conozco son: '
                                     + ", ".join(v["nombre"] for v in vehs))
            continue
        if estado == "ambiguo":
            pid = uuid.uuid4().hex[:6]
            context.user_data["gas_pend"] = {"pid": pid, "item": it, "cands": [v["nombre"] for v in cands]}
            filas = [_boton(v["nombre"], f"gas:sel:{pid}:{i}") for i, v in enumerate(cands)]
            filas.append(_boton("Ninguno de estos", f"gas:no:{pid}"))
            await _enviar(context, update, f'¿Cuál de estos vehículos es "{it["vehiculo"]}"?', InlineKeyboardMarkup(filas))
            return
        _preparar_repostaje(update, context, it, cands[0])
    await _mostrar_ficha(update, context)


def _texto_repostaje(veh: dict, it: dict, a: dict) -> str:
    u = a["unidad"]
    t = f"Repostaje: {veh['nombre']} - {_n(it['litros'])} L - contador {_n(it['contador'])} {u}{_fecha_txt(it['fecha'])}."
    if a["tramo"]:
        t += f" Desde el anterior ({a['prev_fecha']}): {_n(a['tramo'])} {u}."
        if a["consumo"] is not None:
            t += f" Consumo aprox.: {_n(a['consumo'])} {'L/h' if a['horas'] else 'L/100 km'}."
        elif not it["lleno"]:
            t += " (no lleno: no cuenta para el consumo)"
    elif a["primero"]:
        t += " Primer repostaje que tengo de este vehículo."
    return t


def _preparar_repostaje(update: Update, context: ContextTypes.DEFAULT_TYPE, it: dict, veh: dict) -> None:
    usuario = obtener_trabajador(update.effective_user.id) or "?"
    msg = context.user_data.get("gas_msg", "")
    a = G.analizar_repostaje(veh, it["litros"], it["contador"], it["lleno"], it["fecha"])

    def commit() -> str:
        r = G.registrar_repostaje(veh, it["litros"], it["contador"], it["lleno"], it["fecha"], usuario, msg, it.get("importe"),
                                  revisar="; ".join(a["avisos"]))
        return _texto_repostaje(veh, it, r)

    _ficha(context)["ops"].append({"area": "repostaje", "linea": _texto_repostaje(veh, it, a),
                                   "avisos": a["avisos"], "commit": commit})


def _preparar_deposito(update: Update, context: ContextTypes.DEFAULT_TYPE, it: dict) -> None:
    ficha = _ficha(context)
    usuario = obtener_trabajador(update.effective_user.id) or "?"
    msg = context.user_data.get("gas_msg", "")
    estado, targets, err = G.resolver_depositos(it["deposito"], it.get("numero_deposito"), it.get("es_total"), G.leer_depositos())
    if estado != "ok":
        ficha["rechazos"].append(err)
        return
    if it["tipo"] == "lectura":
        sin = [d["nombre"] for d in targets if not d["mide"]]
        if sin:
            ficha["rechazos"].append(f"{', '.join(sin)} no tiene medidor: solo puedo apuntar lo que entra")
            return
    for d in targets:
        a = G.analizar_gasoil(d, it["tipo"], it["litros"], it["fecha"])
        que = f"entrada de {_n(it['litros'])} L" if it["tipo"] == "entrada" else f"lectura de {_n(it['litros'])} L"
        extra = " (sustituye la lectura que ya había ese día)" if a["sustituye"] else ""
        linea = f"Depósito {d['nombre']}: {que}{extra}{_fecha_txt(it['fecha'])}."

        def commit(d=d, linea=linea, rev="; ".join(a["avisos"])) -> str:
            G.registrar_gasoil(d, it["tipo"], it["litros"], it["fecha"], usuario, msg, revisar=rev)
            return linea

        ficha["ops"].append({"area": "deposito", "linea": linea,
                             "avisos": [f"{d['nombre']}: {x}" for x in a["avisos"]], "commit": commit})


async def callback_gas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not _puede_gasoil(query.from_user.id):
        return
    partes = query.data.split(":")
    accion = partes[1]
    pend = context.user_data.get("gas_pend")
    if not pend or pend["pid"] != partes[2]:
        await query.edit_message_text("Esta pregunta ya no está activa. Vuelve a enviar el mensaje.")
        return
    context.user_data.pop("gas_pend")
    try:
        if accion == "no":
            await query.edit_message_text("Vale, ese repostaje no se tendrá en cuenta.")
            _ficha(context)["cancelados"] += 1
        elif accion == "sel":
            nombre = pend["cands"][int(partes[3])]
            veh = next((v for v in G.leer_vehiculos() if v["nombre"] == nombre), None)
            if veh is None:
                await query.edit_message_text("Ese vehículo ya no está en la lista.")
            else:
                await query.edit_message_text(f"Elegido: {nombre}")
                _preparar_repostaje(update, context, pend["item"], veh)
    except Exception as e:
        logger.error(f"Error en callback de gasoil: {e}")
        await _enviar(context, update, "Ha habido un error. Vuelve a enviar el mensaje.")
        await _notificar_admin(context, f"Error en callback de gasoil: {e}")
        return
    await _seguir_cola_gas(update, context)


async def cmd_gasoil(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _puede_gasoil(update.effective_user.id):
        await _no_registrado(update)
        return
    texto = " ".join(context.args).strip()
    if not texto:
        await update.message.reply_text(
            "Escribe el registro tras el comando (o sin comando, si menciona litros o depósitos, o desde el menú). Ejemplos:\n"
            "Ranger, 47 litros, 128500 km\n"
            "Hemos llenado 1000 litros en cada depósito de la casa grande\n"
            "Hoy el lector marca 1500 en cada depósito de la casa principal\n"
            "Casa labor depósito 1: 600, depósito 2: 200\n\n"
            "Con /depositos ves la última lectura de cada depósito.")
        return
    context.user_data["origen"] = texto
    try:
        await _procesar_gasoil(update, context, texto, explicito=True)
    except Exception as e:
        logger.error(f"Error en /gasoil: {e}")
        await update.message.reply_text("Error al preparar el registro.")
        await _notificar_admin(context, f"Error en /gasoil: {e}")


async def _mostrar_depositos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        deps = G.estado_depositos()
    except Exception as e:
        logger.error(f"Error en /depositos: {e}")
        await _enviar(context, update, "No he podido leer el Sheet. Inténtalo en un momento.")
        return
    lineas = ["Depósitos de gasoil:", ""]
    for d in deps:
        entradas = f"Entradas este año: {d['entradas'] or 0} L."
        if not d["mide"]:
            lineas.append(f"- {d['nombre']}: {entradas}")
        elif not d["lectura"]:
            lineas.append(f"- {d['nombre']}: todavía no hay ninguna lectura. {entradas}")
        else:
            lineas.append(f"- {d['nombre']}: última lectura {d['lectura']} L ({d['fecha']}). {entradas}")
    await _enviar(context, update, "\n".join(lineas))


async def cmd_depositos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _puede_gasoil(update.effective_user.id):
        await _no_registrado(update)
        return
    await _mostrar_depositos(update, context)


# ── Piensos: relleno de comederos ─────────────────

async def _procesar_piensos(update: Update, context: ContextTypes.DEFAULT_TYPE, texto: str, explicito: bool) -> bool:
    """Devuelve True si el mensaje se ha tratado como relleno de comederos."""
    crudos = extraer_piensos(texto)
    items = [i for i in (P.validar_item(x) for x in crudos) if i]
    if not items:
        if explicito:
            await update.message.reply_text(
                "No he entendido ningún relleno. Escríbelo así, por ejemplo:\n"
                "Rellenado: Cuernico 10 cestas, Laguna 240 kg, Charca Roja 8 cestas\n"
                "(indica siempre si son kilos o cestas)")
        return explicito
    if not explicito:
        coms = P.leer_comederos()
        if not any(P.item_reconocible(i, coms) for i in items):
            return False   # no habla de nuestros comederos: se trata como parte de trabajo
    if context.user_data.get("pie_pend") or context.user_data.get("pie_cola"):
        await update.message.reply_text("Tenía una pregunta anterior sin contestar; la he descartado.")
    _nueva_ficha(context)
    context.user_data["pie_cola"] = items
    context.user_data["pie_msg"] = texto
    context.user_data.pop("pie_pend", None)
    await _seguir_cola_pie(update, context)
    return True


async def _seguir_cola_pie(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cola = context.user_data.get("pie_cola", [])
    ficha = _ficha(context)
    coms = P.leer_comederos()
    while cola:
        it = cola.pop(0)
        if it["sacos"]:
            ficha["rechazos"].append(f'{it["comedero"]}: no sé pasar sacos a kilos; dime los kg o las cestas')
            continue
        if it["sin_unidad"]:
            ficha["rechazos"].append(f'{it["comedero"]}: falta la unidad de "{it["dicha"]}" (¿kg o cestas?)')
            continue
        estado, cands = P.resolver_comedero(it["comedero"], coms)
        if estado == "no_encontrado":
            ficha["rechazos"].append(f'"{it["comedero"]}" no es un comedero que conozca')
            continue
        if estado == "ambiguo":
            pid = uuid.uuid4().hex[:6]
            context.user_data["pie_pend"] = {"pid": pid, "item": it, "cands": [c["nombre"] for c in cands]}
            filas = [_boton(c["nombre"], f"pie:sel:{pid}:{i}") for i, c in enumerate(cands)]
            filas.append(_boton("Ninguno de estos", f"pie:no:{pid}"))
            await _enviar(context, update, f'¿Cuál de estos comederos es "{it["comedero"]}"?', InlineKeyboardMarkup(filas))
            return
        _preparar_relleno(update, context, it, cands[0])
    await _mostrar_ficha(update, context)


def _texto_relleno(com: dict, it: dict, a: dict) -> str:
    t = f"Comedero {com['nombre']}: {_n(it['kg'])} kg"
    if not it["dicha"].endswith("kg"):
        t += f" ({it['dicha']})"
    t += _fecha_txt(it["fecha"])
    if a["kgdia"] is not None:
        t += f". {a['dias']} días desde el anterior ({a['prev']}): {_n(a['kgdia'])} kg/día, unos {_n(a['kgdia'] * 7)} kg a la semana"
    elif a["primero"]:
        t += ". Primer relleno que tengo de este comedero"
    return t + "."


def _preparar_relleno(update: Update, context: ContextTypes.DEFAULT_TYPE, it: dict, com: dict) -> None:
    usuario = obtener_trabajador(update.effective_user.id) or "?"
    msg = context.user_data.get("pie_msg", "")
    a = P.analizar_relleno(com, it)

    def commit() -> str:
        r = P.registrar_relleno(com, it, usuario, msg, revisar="; ".join(a["avisos"]))
        return _texto_relleno(com, it, r)

    _ficha(context)["ops"].append({"area": "pienso", "linea": _texto_relleno(com, it, a),
                                   "avisos": [f"{com['nombre']}: {x}" for x in a["avisos"]], "commit": commit})


async def callback_pie(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not _puede_piensos(query.from_user.id):
        return
    partes = query.data.split(":")
    accion = partes[1]
    pend = context.user_data.get("pie_pend")
    if not pend or pend["pid"] != partes[2]:
        await query.edit_message_text("Esta pregunta ya no está activa. Vuelve a enviar el mensaje.")
        return
    context.user_data.pop("pie_pend")
    try:
        if accion == "no":
            await query.edit_message_text("Vale, ese comedero no se tendrá en cuenta.")
            _ficha(context)["cancelados"] += 1
        elif accion == "sel":
            nombre = pend["cands"][int(partes[3])]
            com = next((c for c in P.leer_comederos() if c["nombre"] == nombre), None)
            if com is None:
                await query.edit_message_text("Ese comedero ya no está en la lista.")
            else:
                await query.edit_message_text(f"Elegido: {nombre}")
                _preparar_relleno(update, context, pend["item"], com)
    except Exception as e:
        logger.error(f"Error en callback de piensos: {e}")
        await _enviar(context, update, "Ha habido un error. Vuelve a enviar el mensaje.")
        await _notificar_admin(context, f"Error en callback de piensos: {e}")
        return
    await _seguir_cola_pie(update, context)


async def _mostrar_piensos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        coms = P.estado_comederos()
    except Exception as e:
        logger.error(f"Error en /piensos: {e}")
        await _enviar(context, update, "No he podido leer el Sheet. Inténtalo en un momento.")
        return
    con_datos = sorted((c for c in coms if _f_num(c["semana"]) is not None), key=lambda c: -_f_num(c["semana"]))
    lineas = ["Consumo de pienso por comedero (último ciclo, de más a menos):", ""]
    for i, c in enumerate(con_datos, start=1):
        media = f", media {_n(_f_num(c['media']))}" if _f_num(c["media"]) is not None else ""
        lineas.append(f"{i}. {c['nombre']}: {_n(_f_num(c['semana']))} kg/semana{media} (relleno del {c['fecha']})")
    sin = [c["nombre"] for c in coms if _f_num(c["semana"]) is None]
    if not con_datos:
        lineas.append("Todavía no hay ciclos completos: hacen falta dos rellenos de cada comedero.")
    if sin:
        lineas += ["", "Sin datos de consumo todavía: " + ", ".join(sin)]
    await _enviar(context, update, "\n".join(lineas))


async def cmd_piensos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _puede_piensos(update.effective_user.id):
        await _no_registrado(update)
        return
    texto = " ".join(context.args).strip()
    if not texto:
        await _mostrar_piensos(update, context)
        return
    context.user_data["origen"] = texto
    try:
        await _procesar_piensos(update, context, texto, explicito=True)
    except Exception as e:
        logger.error(f"Error en /piensos: {e}")
        await update.message.reply_text("Error al preparar el relleno.")
        await _notificar_admin(context, f"Error en /piensos: {e}")


# ── Resumen de lo recibido ────────────────────────

async def cmd_hoy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not puede_ver_resumen(update.effective_user.id):
        await update.message.reply_text("No tienes permiso para ver este resumen.")
        return
    await _mostrar_hoy(update, context, " ".join(context.args).strip().lower())


async def _mostrar_hoy(update: Update, context: ContextTypes.DEFAULT_TYPE, arg: str) -> None:
    dia = hoy()
    if arg == "ayer":
        dia -= timedelta(days=1)
    elif arg:
        try:
            dia = datetime.strptime(arg, "%d/%m/%Y").date()
        except ValueError:
            await _enviar(context, update, "Escribe /hoy, /hoy ayer o /hoy 05/10/2026")
            return
    try:
        filas = leer_partes_enviados(dia)
        movs = V.movimientos_enviados(dia)
        gas = G.movimientos_enviados(dia)
        pie = P.movimientos_enviados(dia)
    except Exception as e:
        logger.error(f"Error en /hoy: {e}")
        await _enviar(context, update, "No he podido leer los datos del Sheet. Inténtalo en un momento.")
        return

    por_parte: dict[tuple[str, str], list[str]] = {}
    for r in filas:
        clave = (r.get("Trabajador", ""), r.get("Fecha", ""))
        por_parte.setdefault(clave, []).append(f"{r.get('Categoría', '')} {r.get('Horas', '')}h")
    lineas = [f"Resumen de lo recibido el {dia.strftime('%d/%m/%Y')}", "", "PARTES DE TRABAJO"]
    lineas += ([f"- {t} (parte del {f}): " + ", ".join(ts) for (t, f), ts in por_parte.items()] or ["- Ninguno"])
    lineas += ["", "VINOS"]
    lineas += ([f"- {m['hora']} {m['usuario']}: {m['cantidad']:+d} {m['vino']}" + (f" - {m['quien']}" if m["quien"] else "")
                for m in movs] or ["- Ninguno"])
    lineas += ["", "REPOSTAJES DE VEHÍCULOS"]
    lineas += ([f"- {g['hora']} {g['usuario']}: {g['vehiculo']}, {g['litros']} L, contador {g['contador']}"
                for g in gas["repostajes"]] or ["- Ninguno"])
    lineas += ["", "DEPÓSITOS DE GASOIL"]
    lineas += ([f"- {g['hora']} {g['usuario']}: {g['tipo'].lower()} {g['litros']} L en {g['deposito']}"
                for g in gas["gasoil"]] or ["- Ninguno"])
    lineas += ["", "PIENSOS (RELLENO DE COMEDEROS)"]
    lineas += ([f"- {g['hora']} {g['usuario']}: {g['comedero']}, {g['kg']} kg" for g in pie] or ["- Ninguno"])
    texto = "\n".join(lineas)
    for i in range(0, len(texto), 4000):
        await _enviar(context, update, texto[i:i + 4000])


# ── Arranque ──────────────────────────────────────

async def _al_arrancar(app: Application) -> None:
    admin_id = obtener_admin_id()
    if not admin_id:
        return
    pendientes = []
    if not obtener_secretaria_id():
        pendientes.append("no hay ID de secretaria (SECRETARIA_ID en workers.py): nada llegará a su chat")
    provisionales = [n for i, n in TRABAJADORES.items() if 123456000 <= i < 123457000]
    if provisionales:
        pendientes.append("trabajadores con ID provisional: " + ", ".join(provisionales))
    if _workers.SECRETARIA_EN_PRUEBA:
        pendientes.append("MODO PRUEBA: los avisos de la secretaria te llegan a ti (SECRETARIA_ID es el tuyo)")
    texto = "Bot iniciado." + (("\nPendiente: " + "; ".join(pendientes) + ".") if pendientes else "")
    try:
        await app.bot.send_message(chat_id=admin_id, text=texto)
    except Exception as e:
        logger.warning(f"No se pudo enviar el aviso de arranque: {e}")


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Error no controlado", exc_info=context.error)
    await _notificar_admin(context, f"Error no controlado en el bot: {context.error}")


def main() -> None:
    app = Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"]).post_init(_al_arrancar).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("ayuda", cmd_menu))
    app.add_handler(CommandHandler("miid", cmd_miid))
    app.add_handler(CommandHandler("hoy", cmd_hoy))
    app.add_handler(CommandHandler("gasoil", cmd_gasoil))
    app.add_handler(CommandHandler("depositos", cmd_depositos))
    app.add_handler(CommandHandler("piensos", cmd_piensos))
    app.add_handler(CommandHandler("vino", cmd_vino))
    app.add_handler(CommandHandler("stock", cmd_stock))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_texto))
    app.add_handler(MessageHandler(filters.PHOTO, recibir_foto))
    app.add_handler(MessageHandler(
        filters.VOICE | filters.AUDIO | filters.VIDEO | filters.VIDEO_NOTE
        | filters.Document.ALL | filters.Sticker.ALL, recibir_otro))
    app.add_handler(CallbackQueryHandler(callback_ficha, pattern=r"^ficha:"))
    app.add_handler(CallbackQueryHandler(callback_vino, pattern=r"^vino:"))
    app.add_handler(CallbackQueryHandler(callback_gas, pattern=r"^gas:"))
    app.add_handler(CallbackQueryHandler(callback_pie, pattern=r"^pie:"))
    app.add_handler(CallbackQueryHandler(callback_menu, pattern=r"^menu:"))
    app.add_error_handler(_on_error)

    logger.info("Bot iniciado.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    import asyncio
    asyncio.set_event_loop(asyncio.new_event_loop())
    main()
