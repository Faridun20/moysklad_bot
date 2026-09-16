"""E2E: фото к заказу (B9) и статус бэкапа в «Настройки» (B10).

Загрузку фото гоняем ЖИВЫМ браузером, а не только через FastAPI TestClient
(tests/test_order_photos.py) — контракт «фронт шлёт data-URL ↔ сервер кладёт в
Telegram (здесь — FakeBot) ↔ фронт рисует thumbnail из ответа» иначе не
проверяет никто (см. `tests/e2e/conftest.py`). Telegram — `e2e.bot`
(`tests.liveserver.FakeBot`), настоящей сети нет.
"""

from __future__ import annotations

import base64

from tests.e2e.conftest import go, seed_order, settled

# Настоящий декодируемый 1×1 PNG (не просто магические байты): загрузка идёт
# через живой Chromium, а `shrinkImage()` в app.js гонит файл через
# `<canvas>`/`Image()` ДО отправки на сервер — фиктивные байты браузер не
# декодирует, и апload молча свалился бы в «не изображение» раньше, чем
# дошёл до /api/orders/photo_upload.
_PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


def test_manager_uploads_photo_visible_on_own_order_card(open_app, e2e, monkeypatch):
    monkeypatch.setenv("PHOTOS_TG_CHAT_ID", "-1009999999")
    seeded = seed_order(e2e, qty=1, price=40.0)
    oid = seeded["order_id"]

    page = open_app(e2e.ids["mgr"])
    settled(page)
    go(page, "sales")
    settled(page)

    card = page.locator(f'.order-card[data-id="{oid}"]')
    card.wait_for()
    card.locator("[data-details-toggle]").click()
    add_btn = card.locator(f"#order-photo-add-{oid}")
    add_btn.wait_for()

    with page.expect_file_chooser() as fc_info:
        add_btn.click()
    fc_info.value.set_files({
        "name": "raspiska.png", "mimeType": "image/png", "buffer": _PNG_1PX,
    })

    # Тост подтверждает загрузку, а карточка перерисовывается со снимком.
    page.wait_for_selector(f'.order-card[data-id="{oid}"] .machine-photo img')
    page.wait_for_function(
        "(id) => { const img = document.querySelector("
        "  `.order-card[data-id=\"${id}\"] .machine-photo img`); "
        "return img && img.src.startsWith('blob:'); }",
        arg=oid,
    )
    assert len(e2e.bot.photos) == 1
    assert e2e.rows(
        "SELECT COUNT(*) AS n FROM order_photos WHERE order_id = ?", (oid,)
    )[0]["n"] == 1


def test_manager_does_not_see_upload_button_on_someone_elses_order(open_app, e2e, monkeypatch):
    """Видимость фото-ленты повторяет видимость самого заказа: чужой заказ
    менеджер вообще не видит в списке — кнопке рисоваться негде."""
    monkeypatch.setenv("PHOTOS_TG_CHAT_ID", "-1009999999")
    seeded = seed_order(e2e, qty=1, price=40.0)
    oid = seeded["order_id"]

    other_mgr = 777
    e2e.db.set_role(other_mgr, "other_mgr", "Other Manager", "manager")
    page = open_app(other_mgr)
    settled(page)
    go(page, "sales")
    settled(page)
    assert page.locator(f'.order-card[data-id="{oid}"]').count() == 0


def test_boss_settings_shows_backup_status(open_app, e2e):
    e2e.db.record_cron_run(
        task_name="backup", status="ok",
        started_at="2026-09-15 03:00:00", finished_at="2026-09-15 03:00:12",
        duration_ms=12000,
    )
    page = open_app(e2e.ids["boss"])
    settled(page)
    go(page, "settings")
    settled(page)

    section = page.locator("#content").locator("text=Резервные копии")
    section.wait_for()
    row_text = page.locator("#content .card-row-sub").all_inner_texts()
    assert any("успешно" in t and "2026-09-15" in t for t in row_text)
