"""Процесс-локальный кэш каталога датакронов (сезоны/способности/фокус-персонажи) для
веб-дашборда — тот же смысл, что bot.datacron_cache в боте, но веб-процесс не имеет
доступа к объекту bot, поэтому кэш живёт здесь: TTL 12ч, лениво строится при первом
обращении (без фонового tasks.loop, в отличие от бота — веб-процесс не держит долгоживущих
циклов). Реально всю работу делает cogs.datacron_requirements._fetch_datacron_cache —
не дублируем её, только оборачиваем в TTL-кэш."""

import time

from cogs.datacron_requirements import _fetch_datacron_cache

_TTL_SECONDS = 12 * 60 * 60

_cache = None
_cached_at = 0.0


async def get_catalog(comlink) -> dict:
    """Может бросить исключение, если кэш ещё ни разу не строился успешно и Comlink
    недоступен — вызывающий код должен обработать (страница показывает "ещё загружается",
    не падает с 500). Если кэш уже был построен хотя бы раз, сетевой сбой при плановом
    обновлении молча игнорируется и возвращается прошлый (устаревший) кэш."""
    global _cache, _cached_at
    now = time.time()
    if _cache is not None and (now - _cached_at) <= _TTL_SECONDS:
        return _cache
    try:
        fresh = await _fetch_datacron_cache(comlink)
    except Exception as e:
        if _cache is not None:
            print(f"⚠️ [web] Не удалось обновить каталог датакронов, использую прошлый: {e}")
            return _cache
        raise
    _cache = fresh
    _cached_at = now
    return _cache
