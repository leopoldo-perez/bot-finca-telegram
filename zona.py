"""Fecha y hora siempre en horario de España, aunque el servidor (p. ej. en la nube) use UTC."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("Europe/Madrid")


def ahora() -> datetime:
    return datetime.now(_TZ).replace(tzinfo=None)


def hoy() -> date:
    return ahora().date()
