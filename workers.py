"""
Mapeo de IDs de Telegram a nombres de trabajadores.

Para obtener el ID de Telegram de cada trabajador:
1. Que el trabajador instale Telegram y escriba /start al bot
2. El bot responderá con su ID numérico
3. Añade ese número aquí con su nombre

El ID de Telegram es un número entero, por ejemplo: 123456789
"""

# Clave: ID numérico de Telegram (int)
# Valor: nombre del trabajador (str); es el que se escribe en la columna Trabajador del Sheet.
# Aquí van IDs de ejemplo. Los reales van en workers_local.py (no se sube a git; ver más abajo).
TRABAJADORES: dict[int, str] = {
    111111111: "Administrador",     # Admin (propietario del proyecto)
    123456790: "Isaac",             # Guarda de la finca (responsable del campo)
    123456791: "Andrés",            # Mozo de Isaac (campo)
    123456792: "José Luis",         # Mozo de Isaac (campo)
    123456793: "Roberto",           # Pareja que trabaja en la casa
    123456794: "Carmen",            # Pareja que trabaja en la casa
}

# ID de Telegram del administrador (recibe avisos de error y reportes)
ADMIN_ID: int = 111111111


# ID de Telegram de la secretaria (recibe avisos de cada parte/movimiento y puede usar /hoy).
# Se obtiene con /miid desde su Telegram. Si vale 0 no se envía nada.
# PRUEBA: ahora es el del admin, para ver cómo le llegan los avisos a Eva.
# Al poner el ID real de Eva: SECRETARIA_ID = <su id> y SECRETARIA_EN_PRUEBA = False.
SECRETARIA_ID: int = ADMIN_ID
SECRETARIA_EN_PRUEBA: bool = True   # True = también recibe aviso de los mensajes que envía ella misma


def obtener_secretaria_id() -> int | None:
    return SECRETARIA_ID if SECRETARIA_ID != 0 else None


def puede_ver_resumen(telegram_id: int) -> bool:
    return telegram_id in (ADMIN_ID, SECRETARIA_ID)


# Quién puede registrar y consultar vinos (/vino, /stock, /deshacer). Se define por NOMBRE (el de
# TRABAJADORES), así no hay que tocar nada cuando se sustituyan los IDs provisionales por los reales.
VINOS_AUTORIZADOS: set[str] = {"Administrador", "Roberto", "Carmen"}


def puede_gestionar_vinos(telegram_id: int) -> bool:
    return TRABAJADORES.get(telegram_id) in VINOS_AUTORIZADOS


def obtener_trabajador(telegram_id: int) -> str | None:
    """Devuelve el nombre del trabajador por su ID de Telegram, o None si no está registrado."""
    return TRABAJADORES.get(telegram_id)


def obtener_admin_id() -> int | None:
    """Devuelve el ID del administrador."""
    return ADMIN_ID if ADMIN_ID != 0 else None


def listar_trabajadores() -> list[str]:
    """Devuelve la lista de nombres de todos los trabajadores registrados."""
    return list(TRABAJADORES.values())


# ── Datos reales ──────────────────────────────────────────────
# Si existe workers_local.py (ignorado por git), sus valores sustituyen a los de ejemplo de arriba.
try:
    from workers_local import (ADMIN_ID, SECRETARIA_EN_PRUEBA, SECRETARIA_ID,  # noqa: F401,F811
                               TRABAJADORES, VINOS_AUTORIZADOS)
except ImportError:
    pass
