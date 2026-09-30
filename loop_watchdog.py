"""Сторож event loop'а: ловит, КАКОЙ код блокирует asyncio.

Корутина-пульс раз в BEAT_SEC обновляет отметку времени; отдельный поток-демон
следит за ней. Если пульса нет дольше STALL_SEC — loop кем-то заблокирован
(синхронный вызов в async-коде), и поток печатает текущий стек потока loop'а,
т.е. ровно ту строку, которая сейчас держит loop. После разблокировки печатается
итоговая длительность зависания.

Зачем: периодические «Unknown interaction» (см. память про event-loop blocking) —
по корреляции логов виновника не найти, нужен стек в момент зависания.
"""
import asyncio
import sys
import threading
import time
import traceback

BEAT_SEC = 0.25
STALL_SEC = 1.5

_last_beat = time.monotonic()
_loop_thread_id = None
_started = False


async def _heartbeat():
    global _last_beat
    while True:
        _last_beat = time.monotonic()
        await asyncio.sleep(BEAT_SEC)


def _other_threads(frames) -> str:
    """Хвосты стеков остальных занятых потоков. Если loop стоит в select/на ровном
    месте, его держит не свой код, а GIL, захваченный другим потоком (например,
    json.loads огромного ответа Comlink внутри to_thread — C-декодер GIL не отпускает)."""
    names = {t.ident: t.name for t in threading.enumerate()}
    out = []
    for tid, f in frames.items():
        if tid in (_loop_thread_id, threading.get_ident()):
            continue
        tail = traceback.format_stack(f)[-4:]
        if "threading.py" in tail[-1] and "wait" in tail[-1]:
            continue  # простаивающий поток пула
        out.append(f"  --- поток {names.get(tid, tid)}:\n" + "".join(tail))
    return "🐢 [Watchdog] Занятые потоки:\n" + "".join(out) if out else ""


def _watch():
    stalled_since = None
    while True:
        time.sleep(BEAT_SEC)
        lag = time.monotonic() - _last_beat
        if lag > STALL_SEC:
            if stalled_since is None:
                stalled_since = _last_beat
                frames = sys._current_frames()
                frame = frames.get(_loop_thread_id)
                stack = "".join(traceback.format_stack(frame)) if frame else "(стек недоступен)\n"
                ts = time.strftime("%H:%M:%S", time.gmtime())
                print(f"🐢 [Watchdog] {ts} UTC: event loop заблокирован уже {lag:.1f}с. Стек потока loop'а:\n{stack}"
                      + _other_threads(frames), end="")
        elif stalled_since is not None:
            print(f"🐢 [Watchdog] Event loop разблокирован, зависание длилось ~{_last_beat - stalled_since:.1f}с")
            stalled_since = None


def start():
    """Идемпотентно — on_ready может срабатывать несколько раз (реконнекты)."""
    global _started, _loop_thread_id, _last_beat
    if _started:
        return
    _started = True
    _loop_thread_id = threading.get_ident()
    _last_beat = time.monotonic()
    asyncio.get_running_loop().create_task(_heartbeat())
    threading.Thread(target=_watch, name="loop-watchdog", daemon=True).start()
    print(f"🐢 [Watchdog] Сторож event loop'а запущен (порог {STALL_SEC}с)")
