"""
Демо-данные для показа панели на встрече: python app.py --demo

Заполняет ИЗОЛИРОВАННУЮ demo.db (реальную monitor.db не трогает) одной
фейковой клиникой с 6-месячной историей рейтинга, отзывами (включая свежий
негатив без ответа) и готовым аналитическим отчётом — чтобы графики, алерты и
отчёт были видны сразу, без скрапинга и без чужих данных.

Отчёт собирается из синтетических агрегатов через insights.render_report_html —
без обращения к LLM. Демо-компания (firm_id="demo") не скрапится (см. monitor).
"""
from datetime import datetime, timedelta, timezone

import db
import insights

DEMO_FIRM = "demo"
DEMO_NAME = "Демо · Стоматология «Ак Тіс»"


def _reviews():
    """~30 синтетических отзывов: позитив с вкраплением свежего негатива."""
    now = datetime.now(timezone.utc)

    def d(days_ago):
        return (now - timedelta(days=days_ago)).isoformat(timespec="seconds")

    pos = [
        "Прекрасная клиника, доктор всё объяснил, лечение без боли. Рекомендую!",
        "Поставила пломбу — аккуратно и быстро. Спасибо за внимательность.",
        "Лучшая стоматология в районе, ходим всей семьёй.",
        "Сделали чистку, всё понравилось, цены адекватные.",
        "Врач золотые руки, совсем не было больно. Буду советовать знакомым.",
        "Записали быстро, приняли вовремя, без очередей.",
        "Имплант поставили отлично, всё прижилось. Благодарю команду.",
        "Очень внимательный администратор, помогли с рассрочкой.",
    ]
    out = []
    rid = 1
    # позитивный поток за полгода
    for i in range(22):
        out.append({"review_id": f"demo{rid}", "rating": 5 if i % 4 else 4,
                    "date_created": d(8 + i * 7), "author": f"Клиент {i + 1}",
                    "text": pos[i % len(pos)],
                    "answered": i % 3 != 0})  # на большинство — ответ
        rid += 1
    # нейтральные
    for i, days in enumerate((40, 70, 120)):
        out.append({"review_id": f"demo{rid}", "rating": 3, "date_created": d(days),
                    "author": f"Гость {i + 1}",
                    "text": "Лечение нормальное, но ждал дольше записи. "
                            "Администратор не сразу перезвонил.",
                    "answered": days > 60})
        rid += 1
    # СВЕЖИЙ негатив без ответа — он и прилетит как алерт на демо
    out.append({"review_id": f"demo{rid}", "rating": 1, "date_created": d(2),
                "author": "Айгерим", "text": "Записалась по акции на чистку, "
                "а на кассе озвучили в два раза больше. Неприятный осадок, "
                "больше не приду.", "answered": False})
    rid += 1
    out.append({"review_id": f"demo{rid}", "rating": 2, "date_created": d(5),
                "author": "Данияр", "text": "Долго ждал в очереди, хотя была запись. "
                "Врач хороший, но организация хромает.", "answered": False})
    return out


def _demo_report_html():
    """Готовый отчёт болей из синтетических агрегатов (без LLM)."""
    agg = {
        "themes": {
            "цены и навязывание услуг": {
                "count": 7, "fresh": 3, "prior": 1, "score": 9.5, "trend": "растёт",
                "quotes": [
                    {"q": "на кассе озвучили в два раза больше, чем по акции",
                     "rating": 1, "date": "2026-06-11", "sev": 3},
                    {"q": "цена выросла уже на месте, неприятный осадок",
                     "rating": 2, "date": "2026-05-30", "sev": 2}]},
            "очереди и ожидание": {
                "count": 5, "fresh": 2, "prior": 2, "score": 6.0, "trend": "стабильно",
                "quotes": [
                    {"q": "долго ждал в очереди несмотря на запись",
                     "rating": 2, "date": "2026-06-08", "sev": 2}]},
            "запись и дозвон": {
                "count": 3, "fresh": 1, "prior": 0, "score": 3.5, "trend": "растёт",
                "quotes": [
                    {"q": "администратор не сразу перезвонил по записи",
                     "rating": 3, "date": "2026-05-02", "sev": 1}]},
        },
        "praises": {"врачи и общение": 14, "качество лечения": 11,
                    "чистота и комфорт": 4},
        "reaction": {"total": 60, "answered": 53, "answered_pct": 88,
                     "negative": 5, "neg_answered": 1, "neg_answered_pct": 20},
        "period": "2025-12-15 — 2026-06-11",
    }
    summary = {
        "summary": "Рейтинг вырос с 3,8 до 4,6 за полгода — поток положительных "
        "отзывов работает. Но главная боль «цены и навязывание услуг» (7 жалоб, "
        "тренд растёт) бьёт в самое чувствительное: люди приходят по акции и "
        "сталкиваются с другой суммой. При 88% ответов на отзывы в целом — на "
        "негатив отвечают лишь в 20% случаев, и свежие плохие отзывы висят первыми.",
        "actions": [
            "Зафиксировать финальную цену акции письменно до визита (SMS/WhatsApp "
            "подтверждение) — проверить через месяц: жалоб на «цена на кассе» должно "
            "стать 0.",
            "Назначить ответственного за ответы на негатив в течение 24 часов — "
            "цель: 100% негатива с ответом (сейчас 20%).",
            "Ввести talon-очередь по времени и СМС-напоминание — замерить долю жалоб "
            "на ожидание (сейчас 5 за период)."],
        "recommendations": {
            "цены и навязывание услуг": "Публиковать итоговую смету до процедуры и "
            "обучить администраторов проговаривать цену по телефону — это снимет "
            "большинство свежих негативов и удержит рейтинг 4,6+.",
            "очереди и ожидание": "Записывать по интервалам и предупреждать о "
            "задержках — недовольство ожиданием обычно даёт −0,2 звезды.",
            "запись и дозвон": "Настроить обратный звонок в течение 15 минут — "
            "снизит потерю первичных пациентов."},
    }
    return insights.render_report_html(DEMO_NAME, "медицина", agg, summary, [],
                                       reviews_used=60, brand="Bedel")


def seed(conn) -> bool:
    """Заполнить демо-БД, если пусто. Возвращает True, если что-то создано."""
    if db.list_companies(conn):
        return False
    cid = db.add_company(conn, DEMO_FIRM, "https://2gis.kz", "almaty",
                         "https://2gis.kz/almaty")
    conn.execute("UPDATE companies SET name=?,category=?,rating=?,reviews_count=?,"
                 "status=?,last_checked=?,tone=?,reply_signature=? WHERE id=?",
                 (DEMO_NAME, "стоматологическая клиника", 4.6, 258, db.ST_OK,
                  db.now_iso(), "тёплый и заботливый",
                  "С заботой, клиника «Ак Тіс» · WhatsApp +7 700 000 00 00", cid))
    conn.commit()

    base = datetime.now(timezone.utc) - timedelta(days=180)
    series = [(3.8, 120, 28), (3.9, 135, 33), (3.9, 150, 40), (4.0, 165, 48),
              (4.1, 182, 55), (4.2, 198, 62), (4.3, 212, 68), (4.4, 226, 74),
              (4.5, 238, 80), (4.5, 247, 83), (4.6, 253, 85), (4.6, 258, 88)]
    for i, (rt, cnt, ans) in enumerate(series):
        ts = (base + timedelta(days=15 * i)).isoformat(timespec="seconds")
        db.add_rating_snapshot(conn, cid, rt, cnt, ans, ts=ts)

    db.save_reviews(conn, cid, _reviews(), mark_new=False)
    html = _demo_report_html()
    db.save_report(conn, cid, "2025-12-15 — 2026-06-11", 60,
                   '{"themes":{"цены и навязывание услуг":{"count":7}}}', html)
    return True


if __name__ == "__main__":
    import sys
    import tempfile
    from pathlib import Path
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass
    with tempfile.TemporaryDirectory() as td:
        conn = db.connect(Path(td) / "demo_test.db")
        checks = []
        checks.append(("seed создаёт", seed(conn) is True))
        checks.append(("повторный seed пропускает", seed(conn) is False))
        comp = db.list_companies(conn)
        checks.append(("одна компания", len(comp) == 1 and comp[0]["firm_id"] == "demo"))
        checks.append(("история есть", len(db.rating_history(conn, comp[0]["id"])) == 12))
        checks.append(("отзывы есть", len(db.recent_reviews(conn, comp[0]["id"], 100)) >= 25))
        checks.append(("отчёт готов", len(db.list_reports(conn, comp[0]["id"])) == 1))
        rep = db.get_report(conn, db.list_reports(conn, comp[0]["id"])[0]["id"])
        checks.append(("отчёт — HTML с болями",
                       "Карта болей" in rep["html"] and "цены" in rep["html"]))
        conn.close()
        ok = 0
        for label, passed in checks:
            print(f"  [{'OK ' if passed else 'FAIL'}] {label}")
            ok += passed
        print(f"{ok}/{len(checks)} passed")
