"""
Печать документов через CUPS (services/printing + handlers/printing).

Мокаем ГРАНИЦУ с внешним миром — запуск процесса `lp`/`lpstat`
(`asyncio.create_subprocess_exec`), а не свои функции: так реально
исполняется сборка argv, разбор вывода и перевод ошибок CUPS в текст для
менеджера. Именно там жили бы настоящие баги.
"""

import asyncio

import pytest

import services.printing as printing
import services.roles as roles


def _run(coro):
    return asyncio.run(coro)


class _FakeProc:
    """Процесс, который отдаёт заранее заданный вывод."""

    def __init__(self, returncode=0, stdout=b"", stderr=b"", hang=False):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._hang = hang
        self.killed = False

    async def communicate(self):
        if self._hang:
            await asyncio.sleep(3600)
        return self._stdout, self._stderr

    def kill(self):
        self.killed = True

    async def wait(self):
        return self.returncode


def _spawn(monkeypatch, proc=None, *, raises=None):
    """Подменить запуск процесса. Возвращает список полученных argv."""
    calls: list[list[str]] = []

    async def _exec(*argv, **kwargs):
        calls.append(list(argv))
        if raises is not None:
            raise raises
        return proc

    monkeypatch.setattr(printing.asyncio, "create_subprocess_exec", _exec)
    # `lp`/`lpstat` в тест-контейнере нет — иначе печать отключилась бы до
    # запуска процесса, и мок не проверял бы ничего.
    monkeypatch.setattr(printing.shutil, "which", lambda name: f"/usr/bin/{name}")
    return calls


@pytest.fixture
def pdf(tmp_path):
    path = tmp_path / "invoice.pdf"
    path.write_bytes(b"%PDF-1.4 fake")
    return path


# ─── print_document: успех ────────────────────────────────────────────────────


def test_sends_file_to_the_named_queue(monkeypatch, pdf):
    calls = _spawn(
        monkeypatch,
        _FakeProc(0, b"request id is Canon_MF272dw-42 (1 file(s))\n"),
    )
    res = _run(printing.print_document(pdf, "Canon_MF272dw", label="Накладная OUT-1"))

    assert res.ok is True
    assert res.job == "Canon_MF272dw-42"
    assert "Отправлено на печать" in res.message
    argv = calls[0]
    assert argv[0] == "lp"
    assert argv[1:3] == ["-d", "Canon_MF272dw"]
    # Имя задания — чтобы у принтера было видно, чья это бумага.
    assert "-t" in argv and "Накладная OUT-1" in argv
    assert argv[-1] == str(pdf)


def test_queue_name_falls_back_to_the_configured_one(monkeypatch, pdf):
    monkeypatch.setattr(printing, "DEFAULT_PRINTER", "Office_HP")
    calls = _spawn(monkeypatch, _FakeProc(0, b"request id is Office_HP-1 (1 file(s))"))
    assert _run(printing.print_document(pdf)).ok is True
    assert calls[0][1:3] == ["-d", "Office_HP"]


def test_job_id_absent_is_not_a_failure(monkeypatch, pdf):
    """CUPS иногда молчит про request id. Задание принято — это не отказ."""
    _spawn(monkeypatch, _FakeProc(0, b""))
    res = _run(printing.print_document(pdf))
    assert res.ok is True and res.job == ""
    assert res.message == "Отправлено на печать"


# ─── print_document: отказы говорят, ЧТО случилось ───────────────────────────


@pytest.mark.parametrize(
    "stderr,expected",
    [
        (b"lp: The printer or class does not exist.", "Очередь печати не найдена"),
        (b"lp: Unable to connect to server: Connection refused", "CUPS не отвечает"),
        (b"lp: Error - printer is not accepting jobs.", "Очередь остановлена"),
        (b"lp: Forbidden", "нет прав"),
    ],
)
def test_known_cups_errors_become_human_text(monkeypatch, pdf, stderr, expected):
    _spawn(monkeypatch, _FakeProc(1, b"", stderr))
    res = _run(printing.print_document(pdf))
    assert res.ok is False
    assert expected in res.error
    # Сырой хвост остаётся для диагностики, но в текст менеджеру не лезет.
    assert res.detail


def test_unknown_error_shows_the_last_line_not_just_error(monkeypatch, pdf):
    _spawn(monkeypatch, _FakeProc(1, b"", b"lp: something entirely new\n"))
    res = _run(printing.print_document(pdf))
    assert res.ok is False
    assert "something entirely new" in res.error
    assert res.error != "Error"


def test_timeout_kills_the_process_and_reports_it(monkeypatch, pdf):
    proc = _FakeProc(hang=True)
    _spawn(monkeypatch, proc)
    monkeypatch.setattr(printing, "LP_TIMEOUT", 0)

    res = _run(printing.print_document(pdf))
    assert res.ok is False
    assert "не ответил" in res.error
    # Зависший lp обязан быть убит: иначе процессы копятся до OOM.
    assert proc.killed is True


def test_missing_cups_client_is_stated_plainly(monkeypatch, pdf):
    _spawn(monkeypatch, _FakeProc(0))
    monkeypatch.setattr(printing.shutil, "which", lambda _name: None)
    res = _run(printing.print_document(pdf))
    assert res.ok is False
    assert "нет клиента CUPS" in res.error


def test_missing_binary_at_spawn_is_not_a_crash(monkeypatch, pdf):
    """`lp` есть в PATH, но исчез между проверкой и запуском (пересборка образа)."""
    _spawn(monkeypatch, raises=FileNotFoundError("lp"))
    res = _run(printing.print_document(pdf))
    assert res.ok is False and "не ответил" in res.error


def test_missing_file_is_refused_before_spawning(monkeypatch, tmp_path):
    calls = _spawn(monkeypatch, _FakeProc(0))
    res = _run(printing.print_document(tmp_path / "нет-такого.pdf"))
    assert res.ok is False
    assert "не найден" in res.error
    assert calls == [], "процесс не должен запускаться без файла"


@pytest.mark.parametrize("bad", ["", "имя с пробелом", "queue;rm -rf /", "../../etc/passwd"])
def test_bad_queue_name_is_refused_before_spawning(monkeypatch, pdf, bad):
    """Имя очереди идёт отдельным argv (shell нет), но мусорное значение даёт
    невнятный отказ CUPS вместо понятного «поправьте PRINTER_NAME»."""
    calls = _spawn(monkeypatch, _FakeProc(0))
    monkeypatch.setattr(printing, "DEFAULT_PRINTER", bad or "x x")
    res = _run(printing.print_document(pdf, bad))
    assert res.ok is False
    assert "имя принтера" in res.error.lower()
    assert calls == []


# ─── print_pdf_bytes ─────────────────────────────────────────────────────────


def test_bytes_are_written_to_a_temp_file_and_printed(monkeypatch):
    seen: dict = {}

    calls = _spawn(monkeypatch, _FakeProc(0, b"request id is q-7 (1 file(s))"))

    real_exec = printing.asyncio.create_subprocess_exec

    def _peek(path: str) -> None:
        # Файл обязан существовать В МОМЕНТ запуска lp: временный каталог
        # удаляется на выходе из функции, и порядок здесь — суть.
        from pathlib import Path

        seen["exists"] = Path(path).is_file()
        seen["content"] = Path(path).read_bytes() if seen["exists"] else b""

    async def _exec(*argv, **kwargs):
        await asyncio.to_thread(_peek, argv[-1])
        return await real_exec(*argv, **kwargs)

    monkeypatch.setattr(printing.asyncio, "create_subprocess_exec", _exec)

    res = _run(printing.print_pdf_bytes(b"%PDF-here", filename="OUT-1.pdf", label="Накладная"))
    assert res.ok is True and res.job == "q-7"
    assert seen["exists"] is True
    assert seen["content"] == b"%PDF-here"
    assert calls[0][-1].endswith("OUT-1.pdf")


def test_empty_bytes_are_refused(monkeypatch):
    calls = _spawn(monkeypatch, _FakeProc(0))
    res = _run(printing.print_pdf_bytes(b""))
    assert res.ok is False and "Пустой документ" in res.error
    assert calls == []


def test_filename_from_the_caller_cannot_escape_the_temp_dir(monkeypatch):
    """Имя приходит из `invoice_filename`, но путь в нём не должен уводить
    запись за пределы временного каталога."""
    calls = _spawn(monkeypatch, _FakeProc(0, b"request id is q-1 (1 file(s))"))
    res = _run(printing.print_pdf_bytes(b"%PDF", filename="../../../etc/passwd"))
    assert res.ok is True
    assert calls[0][-1].endswith("/passwd")
    assert calls[0][-1] != "/etc/passwd"


# ─── Статус очереди ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "stdout,state",
    [
        (b"printer Canon_MF272dw is idle.  enabled since Fri 12 Sep", "idle"),
        (b"printer Canon_MF272dw now printing Canon_MF272dw-42.", "busy"),
        (b"printer Canon_MF272dw disabled since Fri 12 Sep -\n\tPaused", "error"),
        (b"printer Canon_MF272dw is doing something else", "unknown"),
    ],
)
def test_status_is_parsed_from_lpstat(monkeypatch, stdout, state):
    calls = _spawn(monkeypatch, _FakeProc(0, stdout))
    st = _run(printing.printer_status("Canon_MF272dw"))
    assert st.ok is True and st.state == state
    assert st.label  # подпись есть для любого состояния
    assert calls[0] == ["lpstat", "-p", "Canon_MF272dw"]


def test_status_failure_explains_itself(monkeypatch):
    _spawn(monkeypatch, _FakeProc(1, b"", b"lpstat: Unable to connect to server"))
    st = _run(printing.printer_status())
    assert st.ok is False and "CUPS не отвечает" in st.text


def test_status_without_cups_client(monkeypatch):
    _spawn(monkeypatch, _FakeProc(0))
    monkeypatch.setattr(printing.shutil, "which", lambda _n: None)
    st = _run(printing.printer_status())
    assert st.ok is False and "cups-client" in st.text


# ─── Формат callback_data ────────────────────────────────────────────────────


def test_callback_roundtrip():
    assert printing.parse_callback(printing.invoice_callback(42)) == ("inv", 42)


@pytest.mark.parametrize(
    "data", ["", "prn:", "prn:inv:", "prn:inv:abc", "prn:inv:0", "prn:inv:-3", "other:inv:1"]
)
def test_broken_callback_is_rejected(data):
    assert printing.parse_callback(data) is None


def test_callback_fits_telegram_limit():
    """64 байта — жёсткий предел Telegram на callback_data."""
    assert len(printing.invoice_callback(10**9).encode()) <= 64


# ─── Кнопка «Распечатать» ────────────────────────────────────────────────────


class _FakeUser:
    def __init__(self, uid, full_name="User"):
        self.id = uid
        self.full_name = full_name


class _FakeMessage:
    def __init__(self):
        self.replies: list[str] = []

    async def reply(self, text, **kwargs):
        self.replies.append(text)

    async def answer(self, text, **kwargs):
        self.replies.append(text)


class _FakeCall:
    def __init__(self, data, uid, full_name="User"):
        self.data = data
        self.from_user = _FakeUser(uid, full_name)
        self.message = _FakeMessage()
        self.alerts: list[tuple[str, dict]] = []

    async def answer(self, text="", **kwargs):
        self.alerts.append((text, kwargs))


def _staff(db):
    roles.invalidate_all_roles()
    db.set_role(1, "mgr", "Manager", "manager")
    db.set_role(9, "keeper", "Keeper", "warehouse_keeper")


def _outgoing_invoice(db, name="Кабель PV 0.6", qty=3, price_cents=10000):
    """Провести расходную накладную и вернуть её id."""
    from services import container_receipt, warehouse

    pid = _run(container_receipt.create_product(name))["product_id"]
    wid = _run(warehouse.default_warehouse_id())
    _run(warehouse.create_invoice(
        invoice_type="incoming", warehouse_id=wid,
        items=[{"product_id": pid, "quantity": qty, "price_cents": None}],
    ))
    res = _run(warehouse.create_invoice(
        invoice_type="outgoing", warehouse_id=wid,
        items=[{"product_id": pid, "quantity": qty, "price_cents": price_cents}],
    ))
    assert res["ok"], res
    return res["invoice_id"]


def test_button_prints_the_invoice_it_names(isolated_db, monkeypatch):
    """Сквозной путь: id из кнопки → накладная из БД → PDF → очередь CUPS."""
    from handlers import printing as h

    db = isolated_db
    _staff(db)
    invoice_id = _outgoing_invoice(db)

    sent: dict = {}

    async def _fake_print(pdf_bytes, *, filename="", printer_name="", label=""):
        sent["bytes"] = pdf_bytes
        sent["filename"] = filename
        sent["label"] = label
        return printing.PrintResult(True, job="q-9")

    monkeypatch.setattr(h.printing, "print_pdf_bytes", _fake_print)

    call = _FakeCall(printing.invoice_callback(invoice_id), 1, "Manager")
    _run(h.cb_print(call))

    # PDF собран заново из накладной, а не взят с диска.
    assert sent["bytes"].startswith(b"%PDF")
    assert sent["filename"].endswith(".pdf")
    # В очереди видно, ЧТО и КТО печатает.
    assert "Накладная" in sent["label"] and "Manager" in sent["label"]
    assert any("Отправлено на печать" in r for r in call.message.replies)


def test_print_failure_shows_the_reason_not_just_error(isolated_db, monkeypatch):
    from handlers import printing as h

    db = isolated_db
    _staff(db)
    invoice_id = _outgoing_invoice(db)

    async def _fail(*a, **k):
        return printing.PrintResult(False, error="Очередь остановлена — принтер не принимает задания")

    monkeypatch.setattr(h.printing, "print_pdf_bytes", _fail)

    call = _FakeCall(printing.invoice_callback(invoice_id), 1)
    _run(h.cb_print(call))

    text = " ".join(call.message.replies)
    assert "Очередь остановлена" in text
    assert "Ошибка печати" in text


def test_button_is_refused_for_roles_without_stock_access(isolated_db, monkeypatch):
    from handlers import printing as h

    db = isolated_db
    _staff(db)
    printed: list = []

    async def _never(*a, **k):
        printed.append(1)
        return printing.PrintResult(True)

    monkeypatch.setattr(h.printing, "print_pdf_bytes", _never)

    call = _FakeCall(printing.invoice_callback(1), 9)  # кладовщик
    _run(h.cb_print(call))

    assert printed == [], "печать не должна запускаться без права"
    assert call.alerts and "Нет доступа" in call.alerts[0][0]


def test_unknown_invoice_says_so(isolated_db, monkeypatch):
    from handlers import printing as h

    db = isolated_db
    _staff(db)
    printed: list = []
    monkeypatch.setattr(
        h.printing, "print_pdf_bytes",
        lambda *a, **k: printed.append(1),
    )

    call = _FakeCall(printing.invoice_callback(999999), 1)
    _run(h.cb_print(call))

    assert printed == []
    assert any("не найдена" in r for r in call.message.replies)


def test_broken_callback_does_not_reach_the_printer(isolated_db, monkeypatch):
    from handlers import printing as h

    db = isolated_db
    _staff(db)
    printed: list = []
    monkeypatch.setattr(
        h.printing, "print_pdf_bytes", lambda *a, **k: printed.append(1)
    )

    for data in ("prn:", "prn:inv:abc", "prn:doc:5"):
        call = _FakeCall(data, 1)
        _run(h.cb_print(call))
        assert printed == [], data
        assert call.alerts


def test_button_absent_when_cups_client_is_missing(monkeypatch):
    """Кнопка, которая гарантированно ответит отказом, хуже отсутствующей."""
    from services import order_workflow

    monkeypatch.setattr(printing, "is_available", lambda: False)
    assert order_workflow._print_keyboard(5) is None

    monkeypatch.setattr(printing, "is_available", lambda: True)
    markup = order_workflow._print_keyboard(5)
    assert markup is not None
    buttons = [b for row in markup.inline_keyboard for b in row]
    assert buttons[0].callback_data == printing.invoice_callback(5)
    assert "Распечатать" in buttons[0].text


def test_no_keyboard_without_an_invoice(monkeypatch):
    from services import order_workflow

    monkeypatch.setattr(printing, "is_available", lambda: True)
    assert order_workflow._print_keyboard(None) is None


# ─── /printer ────────────────────────────────────────────────────────────────


class _FakeStatusMessage:
    def __init__(self, uid):
        self.from_user = _FakeUser(uid)
        self.answers: list[str] = []

    async def answer(self, text, **kwargs):
        self.answers.append(text)


def test_printer_command_reports_state(isolated_db, monkeypatch):
    from handlers import printing as h

    db = isolated_db
    _staff(db)

    async def _status(*_a, **_k):
        return printing.PrinterStatus(True, state="idle", text="printer Canon is idle.")

    monkeypatch.setattr(h.printing, "printer_status", _status)

    msg = _FakeStatusMessage(1)
    _run(h.cmd_printer(msg, bot=None))
    assert msg.answers and "Готов" in msg.answers[0]


def test_printer_command_reports_a_stopped_queue(isolated_db, monkeypatch):
    """Ради этого команда и нужна: бот сказал «отправлено», а бумага не вышла."""
    from handlers import printing as h

    db = isolated_db
    _staff(db)

    async def _status(*_a, **_k):
        return printing.PrinterStatus(True, state="error", text="disabled since Fri - Paused")

    monkeypatch.setattr(h.printing, "printer_status", _status)

    msg = _FakeStatusMessage(1)
    _run(h.cmd_printer(msg, bot=None))
    text = msg.answers[0]
    assert "Проблема" in text
    assert "бумагу" in text  # подсказка, что делать


def test_printer_command_is_silent_for_outsiders(isolated_db, monkeypatch):
    from handlers import printing as h

    db = isolated_db
    _staff(db)
    called: list = []
    monkeypatch.setattr(h.printing, "printer_status", lambda *a, **k: called.append(1))

    msg = _FakeStatusMessage(9)  # кладовщик
    _run(h.cmd_printer(msg, bot=None))
    assert msg.answers == [] and called == []
