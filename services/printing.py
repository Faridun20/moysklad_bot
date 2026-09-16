"""
Печать PDF на офисный принтер через CUPS (`lp` / `lpstat`).

Принтер стоит в офисе, CUPS — на том же сервере, что и бот; в контейнере
живёт только КЛИЕНТ CUPS (пакет `cups-client`), а сервер указывается
переменной `CUPS_SERVER`. Поэтому здесь нет ни IPP-клиента, ни разговора с
принтером напрямую: `lp` умеет и очередь, и ретраи, и статусы, а нам остаётся
позвать его и внятно пересказать результат.

Три решения, определяющие модуль:

* **Печать НИКОГДА не роняет основной поток.** Накладная проведена, документ
  уже ушёл в Telegram — отказ принтера обязан деградировать в сообщение
  «не напечаталось, вот почему», а не в исключение посреди одобрения заявки.
  Поэтому ни одна функция здесь не бросает: всё возвращается `PrintResult`.
* **Причина отказа — текстом для человека.** `bool` не даёт сказать, что
  именно случилось, а «Ошибка» отправляет менеджера искать админа. Разбираем
  типовые ответы CUPS в понятные фразы и оставляем сырой хвост в `detail`
  для диагностики.
* **Асинхронно.** `subprocess.run` заблокировал бы event loop aiogram и
  FastAPI на всё время разговора с CUPS — а он идёт по сети (Tailscale), и
  «обычно быстро» тут не гарантия. Запускаем через
  `asyncio.create_subprocess_exec`, как `legal_docs.render_pdf` запускает
  LibreOffice.

Аргументы собираются списком, без shell — имя принтера и путь в командную
строку подставляются как отдельные argv, интерпретировать их некому.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Имя очереди CUPS. Дефолт — принтер, который стоит в офисе сейчас; на другом
# сервере переопределяется переменной окружения.
DEFAULT_PRINTER = os.getenv("PRINTER_NAME", "Canon_MF272dw").strip() or "Canon_MF272dw"

# Сколько ждём CUPS. `lp` кладёт задание в очередь и выходит — он НЕ ждёт, пока
# принтер напечатает, поэтому 30 секунд с запасом. Если не уложились, значит
# недоступен сам сервер CUPS.
LP_TIMEOUT = 30
LPSTAT_TIMEOUT = 15

# Имя очереди CUPS: буквы, цифры, `_`, `-`, `.`. Пробелы и `/` CUPS не
# принимает сам, но с проверкой отказ читается как «поправьте PRINTER_NAME»,
# а не как невнятная ошибка от lp.
_PRINTER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,127}$")

# Типовые ответы CUPS → фраза для менеджера. Ключи ищем в stderr в нижнем
# регистре: формулировки CUPS от версии к версии плавают, а эти корни живут.
_KNOWN_ERRORS = (
    ("does not exist", "Очередь печати не найдена — проверьте имя принтера в CUPS"),
    ("unknown printer", "Очередь печати не найдена — проверьте имя принтера в CUPS"),
    ("bad printer", "Очередь печати не найдена — проверьте имя принтера в CUPS"),
    ("no default destination", "В CUPS не задан принтер по умолчанию"),
    ("connection refused", "CUPS не отвечает — сервер печати недоступен"),
    ("unable to connect", "CUPS не отвечает — сервер печати недоступен"),
    ("no such host", "CUPS не отвечает — не разрешается имя сервера печати"),
    ("forbidden", "CUPS отклонил задание: нет прав на эту очередь"),
    ("not accepting jobs", "Очередь остановлена — принтер не принимает задания"),
    ("timed out", "Принтер не ответил вовремя"),
)


@dataclass(frozen=True)
class PrintResult:
    """Исход печати. `ok` — задание принято ОЧЕРЕДЬЮ, не «бумага вышла».

    CUPS подтверждает постановку в очередь; дальше принтер может встать на
    замятии, и узнать об этом можно только `printer_status`. Поэтому формулируем
    в UI «отправлено на печать», а не «напечатано» — обещать второе нельзя.
    """

    ok: bool
    job: str = ""
    error: str = ""
    detail: str = ""

    @property
    def message(self) -> str:
        """Готовая строка для чата."""
        if self.ok:
            return f"Отправлено на печать{f' (задание {self.job})' if self.job else ''}"
        return self.error or "Не удалось отправить на печать"


# ─── Callback-данные кнопки «Распечатать» ────────────────────────────────────
#
# Формат живёт ЗДЕСЬ, а не в хендлере: его пишет `order_workflow`, когда
# отправляет печатную форму, а читает `handlers/printing.py`. Разъехаться эти
# две строки не должны, а тащить ради них aiogram в модуль печати незачем —
# это просто текст.
#
# Telegram отводит под callback_data 64 байта, поэтому в кнопку кладём ТИП и
# ID, а не путь к файлу: путь и не влез бы, и позволил бы напечатать любой
# файл контейнера, подставив чужую строку в запрос.

CALLBACK_PREFIX = "prn:"


# Языки печатной формы накладной (`invoice_pdf.DOC_LANGS`). Дублируются
# строкой, а не импортом: модуль печати не тянет за собой рендер.
_INVOICE_LANGS = ("ru_uz", "ru", "uz")


def invoice_callback(invoice_id: int, lang: str | None = None) -> str:
    """`prn:inv:42` или `prn:inv:42:uz` — с языком товарной накладной.
    Без языка (старые кнопки в чатах) печатается язык, выбранный человеком
    последним (`user_prefs.doc_lang`)."""
    tail = f":{lang}" if lang in _INVOICE_LANGS else ""
    return f"{CALLBACK_PREFIX}inv:{invoice_id}{tail}"


def document_callback(doc_id: int) -> str:
    """Юридический документ (generated_documents) — печатается из файла."""
    return f"{CALLBACK_PREFIX}doc:{doc_id}"


def parse_callback(data: str) -> tuple[str, int] | None:
    """`prn:inv:42` → `("inv", 42)`. `None` — чужой или битый callback."""
    if not data.startswith(CALLBACK_PREFIX):
        return None
    parts = data[len(CALLBACK_PREFIX):].split(":", 1)
    if len(parts) != 2:
        return None
    kind, raw = parts
    raw, _, lang = raw.partition(":")
    if lang and lang not in _INVOICE_LANGS:
        return None
    try:
        ref = int(raw)
    except ValueError:
        return None
    if ref <= 0:
        return None
    return kind, ref


def callback_lang(data: str) -> str | None:
    """Язык из `prn:inv:42:uz` → `uz`; нет или чужой — None."""
    if parse_callback(data) is None:
        return None
    lang = data[len(CALLBACK_PREFIX):].split(":")[2:3]
    return lang[0] if lang and lang[0] in _INVOICE_LANGS else None


def is_available() -> bool:
    """Есть ли в этом контейнере клиент CUPS.

    Печать — необязательная часть: образ без `cups-client` должен работать,
    просто без кнопки. Проверяем наличие бинаря, а не «пингуем» CUPS: сетевой
    запрос на каждую отрисовку клавиатуры того не стоит.
    """
    return shutil.which("lp") is not None


def _explain(stderr: str, stdout: str, returncode: int) -> str:
    haystack = f"{stderr}\n{stdout}".lower()
    for needle, text in _KNOWN_ERRORS:
        if needle in haystack:
            return text
    tail = (stderr or stdout or "").strip().splitlines()
    if tail:
        return f"CUPS отказал: {tail[-1][:200]}"
    return f"CUPS отказал (код {returncode})"


async def _run(argv: list[str], timeout_sec: int) -> tuple[int, str, str] | None:
    """Запустить команду. `None` — не запустилась или не уложилась в срок."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        logger.error("Печать: команда %s не найдена в контейнере", argv[0])
        return None
    except OSError:
        logger.exception("Печать: не удалось запустить %s", argv[0])
        return None

    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout_sec)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        logger.error("Печать: %s не ответил за %s с", argv[0], timeout_sec)
        return None

    return (
        proc.returncode or 0,
        out.decode(errors="replace").strip(),
        err.decode(errors="replace").strip(),
    )


async def print_document(
    pdf_path: str | Path, printer_name: str = "", *, label: str = ""
) -> PrintResult:
    """Отправить готовый PDF в очередь CUPS.

    `label` — что и по какому поводу печатаем («накладная OUT-2026-0001,
    заказ #42»). Уходит в лог: строка «печать не прошла» без указания
    документа не даёт понять, что переделывать.
    """
    printer = (printer_name or DEFAULT_PRINTER).strip()
    if not _PRINTER_RE.match(printer):
        logger.error("Печать: недопустимое имя очереди %r", printer)
        return PrintResult(False, error="Некорректное имя принтера в настройках")

    path = Path(pdf_path)
    # Обращения к диску — через поток: проверка существования файла на сетевом
    # или занятом томе блокирует event loop ровно так же, как и чтение.
    if not await asyncio.to_thread(path.is_file):
        logger.error("Печать: файл не найден: %s", path)
        return PrintResult(False, error="Файл документа не найден")

    if not is_available():
        return PrintResult(
            False,
            error="Печать недоступна: в контейнере нет клиента CUPS",
            detail="нет команды lp (пакет cups-client)",
        )

    # -t: имя задания в очереди CUPS. Без него в `lpstat` видно «document.pdf»
    # у всех подряд, и оператор у принтера не понимает, чья это бумага.
    argv = ["lp", "-d", printer]
    if label:
        argv += ["-t", label[:120]]
    argv.append(str(path))

    res = await _run(argv, LP_TIMEOUT)
    if res is None:
        logger.error("Печать не прошла (%s): CUPS не ответил", label or path.name)
        return PrintResult(
            False, error="Сервер печати не ответил — проверьте, что CUPS доступен"
        )

    code, stdout, stderr = res
    if code != 0:
        reason = _explain(stderr, stdout, code)
        logger.error("Печать не прошла (%s): %s | %s", label or path.name, reason, stderr[:300])
        return PrintResult(False, error=reason, detail=stderr[:300])

    # «request id is Canon_MF272dw-42 (1 file(s))» — забираем идентификатор.
    job = ""
    m = re.search(r"request id is (\S+)", stdout)
    if m:
        job = m.group(1)
    logger.info("Печать принята (%s): принтер=%s задание=%s", label or path.name, printer, job)
    return PrintResult(True, job=job)


async def print_pdf_bytes(
    pdf_bytes: bytes, *, filename: str = "document.pdf", printer_name: str = "", label: str = ""
) -> PrintResult:
    """То же, но для PDF, который собран в память и на диск не ложился.

    Такова и накладная (`invoice_pdf.render_invoice_pdf` отдаёт байты), и всё,
    что печатается по кнопке: документ пересобирается в момент нажатия, и
    хранить его файлом между показом и печатью незачем — а на эфемерной
    файловой системе ещё и ненадёжно.
    """
    if not pdf_bytes:
        return PrintResult(False, error="Пустой документ — печатать нечего")
    # Каталог временный и удаляется сразу: `lp` КОПИРУЕТ файл в спул CUPS
    # (по умолчанию, без `-o job-hold`), поэтому к моменту выхода из блока
    # задание уже не зависит от нашего файла.
    with tempfile.TemporaryDirectory(prefix="print-") as tmp:
        path = Path(tmp) / (Path(filename).name or "document.pdf")
        try:
            await asyncio.to_thread(path.write_bytes, pdf_bytes)
        except OSError:
            logger.exception("Печать: не удалось сохранить временный файл")
            return PrintResult(False, error="Не удалось подготовить файл к печати")
        return await print_document(path, printer_name, label=label)


@dataclass(frozen=True)
class PrinterStatus:
    """Состояние очереди. `state` — idle | busy | error | unknown."""

    ok: bool
    state: str = "unknown"
    text: str = ""

    @property
    def label(self) -> str:
        return {
            "idle": "🟢 Готов",
            "busy": "🖨 Печатает",
            "error": "🔴 Проблема",
        }.get(self.state, "⚪️ Состояние неизвестно")


async def printer_status(printer_name: str = "") -> PrinterStatus:
    """Состояние очереди через `lpstat -p`.

    Нужна ровно для случая «бот сказал „отправлено“, а бумага не вышла»:
    задание принято очередью, но принтер стоит — и увидеть это можно только
    здесь.
    """
    printer = (printer_name or DEFAULT_PRINTER).strip()
    if not _PRINTER_RE.match(printer):
        return PrinterStatus(False, text="Некорректное имя принтера в настройках")
    if shutil.which("lpstat") is None:
        return PrinterStatus(False, text="В контейнере нет клиента CUPS (пакет cups-client)")

    res = await _run(["lpstat", "-p", printer], LPSTAT_TIMEOUT)
    if res is None:
        return PrinterStatus(False, text="Сервер печати не ответил")

    code, stdout, stderr = res
    if code != 0:
        return PrinterStatus(False, text=_explain(stderr, stdout, code))

    # «printer Canon_MF272dw is idle.  enabled since …» / «… now printing …» /
    # «… disabled since … reason».
    low = stdout.lower()
    if "is idle" in low:
        state = "idle"
    elif "now printing" in low or "is printing" in low or "is busy" in low:
        state = "busy"
    elif "disabled" in low or "stopped" in low:
        state = "error"
    else:
        state = "unknown"
    return PrinterStatus(True, state=state, text=stdout[:500])
