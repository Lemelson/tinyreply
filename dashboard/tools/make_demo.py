"""Builds data/demo.json (Russian) and data/demo.en.json (English): invented data in the shape the panel reads.
No real people, no API. Both files use the same random seed, so the numbers, names and times match exactly.

The top-level keys follow `igbot report --json` (gates, reels, attempts, uncertain, near) where the bot
already has them, so that a real snapshot can replace this file later without changing the page.
Run: python3 tools/make_demo.py
"""
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

NOW = datetime(2026, 10, 6, 11, 5, 0, tzinfo=timezone.utc)
LANG = "ru"


def L(ru, en):
    return en if LANG == "en" else ru


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(**kw):
    return iso(NOW - timedelta(**kw))


NAMES = ["katya.ux", "dima_dev", "anna.codes", "max.prompt", "lena_ai", "igor.builds", "sasha_ml", "olya.design",
         "petr.script", "nika_data", "roma.web", "vera.notes", "timur_py", "mila.pm", "gleb.ops", "yana_qa",
         "artur.js", "zoya_rnd", "kirill.ai", "dasha_ux", "egor.cloud", "polina_no_code", "stas.bot", "ira_marketing",
         "vlad.neuro", "tanya.school", "misha_startup", "alina.soft", "boris_labs", "sonya.dev", "leva_gpt",
         "rita.freelance", "oleg_arch", "nastya.prod", "fedya_tools", "liza.study", "pasha_auto", "inna.books",
         "yura_stack", "kris.motion", "denis_edu", "marta.ai", "tolya_no_sleep", "eva.design", "vanya_cli",
         "zhenya.data", "lada_tech", "seva.agent", "nelli_copy", "gosha.hack"]


def build():
    rnd = random.Random(1914)
    phrases = L(
        ["Отправил в директ 📩", "Лови в личке 🔥", "Скинул в директ! Если не видно, загляни в «Запросы» 📬",
         "Готово, проверь сообщения ✌️", "Уже в директе 👀", "Написал тебе в личку 🚀", "Ссылка ждет в директе ⚡️",
         "Отправил! Проверь директ, может лежать в «Запросах» 🙌", "Держи, все в личке 🤝", "Есть! Глянь директ 💬"],
        ["Sent you a DM 📩", "Check your DMs 🔥", "Sent! If you can't see it, look in Message requests 📬",
         "Done, check your messages ✌️", "It's in your DMs 👀", "Just messaged you 🚀", "The link is waiting in your DMs ⚡️",
         "Sent! It may be in Message requests 🙌", "Here you go, check your inbox 🤝", "Got it! Check your DMs 💬"])

    arena_a = L("{greeting} Держи Arena Skill для Claude Code 👇 Кинь ссылку Клоду, он сам все поставит. "
                "Дальше пиши /arena и задачу, и получишь лучший ответ из кучи вариантов.\n\n"
                "Я только запускаю блог про Claude и нейросети, подписывайся, будет много полезного 🙌",
                "{greeting} Here's Arena Skill for Claude Code 👇 Send the link to Claude and it installs itself. "
                "Then type /arena and your task, and you get the best answer out of many tries.\n\n"
                "I'm just starting a blog about Claude and AI, follow along, there's a lot more coming 🙌")
    arena_b = L("{greeting} Вот Arena Skill 👇 Просто отправь эту ссылку Клоду.\n\n"
                "Напиши потом, получилось ли запустить? Мне правда интересно 🙌",
                "{greeting} Here's Arena Skill 👇 Just send this link to Claude.\n\n"
                "Tell me later if you got it running? I'd really like to know 🙌")
    guide_text = L("{greeting} Вот гайд по настройке Claude Code 👇 Пять шагов, занимает минут десять.\n\n"
                   "Если будет полезно, напиши пару слов в ответ, мне важно знать 🙌",
                   "{greeting} Here's the Claude Code setup guide 👇 Five steps, about ten minutes.\n\n"
                   "If it helps, reply with a couple of words, it means a lot 🙌")
    prompts_text = L("{greeting} Лови шаблоны промптов 👇", "{greeting} Here are the prompt templates 👇")

    automations = [
        {
            "id": "arena", "name": "Arena Skill", "media_id": "17900000000000101",
            "permalink": "https://www.instagram.com/reel/demo-arena/", "active": True,
            "caption": L("Как получать лучший ответ от Claude из кучи вариантов. Напиши «Арена» — пришлю ссылку",
                         "How to get Claude's best answer out of many tries. Comment “Arena” and I'll send the link"),
            "posted_at": ago(days=6, hours=2), "hue": 262,
            "keywords": L(["арена", "arena"], ["arena"]), "also_match": L(["арееа", "areba", "arrna"], ["areena", "areba", "arrna"]),
            "max_words": 5,
            "link": "https://github.com/Jakeschincariol/arena-skill",
            "text": arena_a,
            "stats": {"comments": 1842, "asked": 1163, "sent": 1068, "waiting": 23, "expired": 41, "uncertain": 2,
                      "refused": 9, "already": 20, "public_done": 1051, "replied": 214, "clicked": 612},
            "ab": {"enabled": True, "started_at": ago(days=5, hours=20)},
            "variants": [
                {"id": "A", "share": 50, "sent": 536, "replied": 91, "clicked": 281, "text": arena_a},
                {"id": "B", "share": 50, "sent": 532, "replied": 123, "clicked": 331, "text": arena_b},
            ],
            "modified_at": ago(days=1, hours=3),
        },
        {
            "id": "guide", "name": L("Гайд по Claude Code", "Claude Code guide"), "media_id": "18099120455123407",
            "permalink": "https://www.instagram.com/reel/demo-guide/", "active": True,
            "caption": L("5 настроек Claude Code, которые экономят часы. Напиши «Гайд» — отправлю подборку",
                         "5 Claude Code settings that save hours. Comment “Guide” and I'll send the list"),
            "posted_at": ago(days=2, hours=5), "hue": 205,
            "keywords": L(["гайд", "guide"], ["guide"]), "also_match": L(["гаид", "гайт"], ["giude", "gude"]), "max_words": 5,
            "link": "https://t.me/example_channel",
            "text": guide_text,
            "stats": {"comments": 611, "asked": 449, "sent": 444, "waiting": 0, "expired": 0, "uncertain": 0,
                      "refused": 3, "already": 2, "public_done": 440, "replied": 58, "clicked": 301},
            "ab": {"enabled": False},
            "variants": [{"id": "A", "share": 100, "sent": 444, "replied": 58, "clicked": 301, "text": guide_text}],
            "modified_at": ago(days=2, hours=4),
        },
        {
            "id": "prompts", "name": L("Шаблоны промптов", "Prompt templates"), "media_id": "18064410233987120",
            "permalink": "https://www.instagram.com/reel/demo-prompts/", "active": False,
            "caption": L("10 промптов, которыми я пользуюсь каждый день. Напиши «Шаблон»",
                         "10 prompts I use every day. Comment “Template”"),
            "posted_at": ago(days=19), "hue": 24,
            "keywords": L(["шаблон", "шаблоны"], ["template", "templates"]), "also_match": L(["шаблн"], ["templte"]),
            "max_words": 5,
            "link": "https://example.com/prompt-templates",
            "text": prompts_text,
            "stats": {"comments": 96, "asked": 36, "sent": 0, "waiting": 0, "expired": 36, "uncertain": 0,
                      "refused": 0, "already": 0, "public_done": 0, "replied": 0, "clicked": 0},
            "ab": {"enabled": False},
            "variants": [{"id": "A", "share": 100, "sent": 0, "replied": 0, "clicked": 0, "text": prompts_text}],
            "modified_at": ago(days=12),
        },
    ]

    asks = L({"arena": ["Арена", "арена 🔥", "Arena", "АРЕНА!!", "арена пожалуйста", "Арена 🙏", "хочу арена", "arena pls",
                        "арееа", "Арена, спасибо за контент"],
              "guide": ["Гайд", "гайд 🙌", "Guide", "гаид", "Гайд плиз", "гайд, очень нужно"]},
             {"arena": ["Arena", "arena 🔥", "Arena", "ARENA!!", "arena please", "Arena 🙏", "I want arena", "arena pls",
                        "areena", "Arena, thanks for the content"],
              "guide": ["Guide", "guide 🙌", "Guide", "giude", "Guide pls", "guide, really need it"]})

    people = []

    def person(i, auto, status, reason="", minutes=0):
        ts = NOW - timedelta(minutes=minutes)
        row = {"comment_id": f"1790{rnd.randrange(10**11, 10**12)}", "username": NAMES[i % len(NAMES)],
               "automation": auto, "text": rnd.choice(asks[auto]), "status": status, "reason": reason, "ts": iso(ts)}
        row["variant"] = rnd.choice("AB") if auto == "arena" else "A"
        if status == "sent":
            row["clicked"] = rnd.random() < 0.58
            row["replied"] = rnd.random() < 0.2
            dm = ts + timedelta(seconds=rnd.randrange(70, 900))
            row["dm_at"] = iso(dm)
            row["public_at"] = iso(dm + timedelta(seconds=2))
            row["public_phrase"] = rnd.choice(phrases)
        return row

    i = 0
    for minutes in (2, 4, 7, 9, 13, 16, 21, 26, 31, 38):                     # the queue
        people.append(person(i, "arena", "pending", "", minutes)); i += 1
    for minutes in (44, 52, 61, 75, 90, 104, 121, 140, 163, 188, 215, 247, 290, 340, 410, 480, 560, 655, 760, 900):
        people.append(person(i, rnd.choice(["arena", "arena", "guide"]), "sent", "", minutes)); i += 1
    people.append(person(i, "arena", "uncertain", "timeout", 96)); i += 1
    people.append(person(i, "arena", "uncertain", "http_5xx", 1320)); i += 1
    people.append(person(i, "arena", "refused", "user_unavailable", 205)); i += 1
    people.append(person(i, "guide", "refused", "user_unavailable", 1510)); i += 1
    people.append(person(i, "arena", "already", "owner_replied", 330)); i += 1
    people.append(person(i, "arena", "dup_user", "", 58)); i += 1
    people.append(person(i, "arena", "dup_user", "", 700)); i += 1
    for days in (7.1, 7.4, 7.9, 8.5):
        people.append(person(i, "arena", "expired", "window", int(days * 1440))); i += 1
    near = L((("арина", "1 буква"), ("орена", "1 буква"), ("арена???? где ссылка на курс и сколько стоит", "длиннее 5 слов")),
             (("arina", "1 letter"), ("arema", "1 letter"), ("arena???? where is the course link and how much is it", "longer than 5 words")))
    for text, why in near:
        row = person(i, "arena", "near", why, rnd.randrange(60, 2000)); row["text"] = text
        people.append(row); i += 1
    people.sort(key=lambda r: r["ts"], reverse=True)
    doubt = [r for r in people if r["status"] == "uncertain"]
    doubt[0]["username"], doubt[1]["username"] = "yana_qa", "vera.notes"
    closed = next(r for r in people if r["status"] == "refused")
    closed["username"] = "gleb.ops"
    last_sent = [r for r in people if r["status"] == "sent"][:2]
    last_sent[0]["username"], last_sent[1]["username"] = "igor.builds", "olya.design"

    hours = []
    for back in range(23, -1, -1):
        start = (NOW - timedelta(hours=back)).replace(minute=0, second=0)
        local = (start.hour + 3) % 24                                      # Moscow
        load = 0.15 if 1 <= local < 8 else 0.55 if local < 12 else 1.0 if local < 23 else 0.6
        asked = int(rnd.uniform(18, 70) * load)
        hours.append({"hour": iso(start), "asked": asked, "sent": min(60, int(asked * rnd.uniform(0.82, 1.0)))})

    days = []
    for back, (asked, sent, replied, clicked) in zip(range(6, -1, -1), (
            (212, 0, 0, 0), (318, 1, 0, 1), (297, 10, 2, 6), (254, 248, 41, 139), (231, 402, 77, 236),
            (203, 439, 83, 262), (133, 412, 69, 269))):
        days.append({"day": iso((NOW - timedelta(days=back)).replace(hour=0, minute=0, second=0)),
                     "asked": asked, "sent": sent, "replied": replied, "clicked": clicked})

    guide_name = automations[1]["name"]
    activity = [
        {"at": ago(minutes=1), "kind": "dm", "level": "ok", "text": L("Сообщение отправлено @igor.builds", "Message sent to @igor.builds"), "automation": "arena"},
        {"at": ago(minutes=1), "kind": "public", "level": "ok", "text": L("Публичный ответ под комментарием @igor.builds: «Лови в личке 🔥»", "Public reply under @igor.builds's comment: “Check your DMs 🔥”"), "automation": "arena"},
        {"at": ago(minutes=3), "kind": "dm", "level": "ok", "text": L("Сообщение отправлено @olya.design", "Message sent to @olya.design"), "automation": "arena"},
        {"at": ago(minutes=3), "kind": "public", "level": "ok", "text": L("Публичный ответ под комментарием @olya.design: «Отправил в директ 📩»", "Public reply under @olya.design's comment: “Sent you a DM 📩”"), "automation": "arena"},
        {"at": ago(minutes=6), "kind": "collect", "level": "info", "text": L("Прочитаны комментарии: 14 новых, из них 9 с кодовым словом", "Read comments: 14 new, 9 with the keyword"), "automation": "arena"},
        {"at": ago(minutes=31), "kind": "collect", "level": "info", "text": L("Прочитаны комментарии: 3 новых, из них 2 с кодовым словом", "Read comments: 3 new, 2 with the keyword"), "automation": "guide"},
        {"at": ago(minutes=96), "kind": "dm", "level": "warn", "text": L("@yana_qa: Instagram не ответил вовремя. Неизвестно, дошло ли сообщение — второй раз бот не напишет", "@yana_qa: Instagram did not answer in time. The message may or may not have arrived, and the bot will not send it twice"), "automation": "arena"},
        {"at": ago(minutes=97), "kind": "system", "level": "warn", "text": L("Отправка приостановлена на 20 минут после неясного ответа Instagram", "Sending paused for 20 minutes after an unclear answer from Instagram")},
        {"at": ago(minutes=117), "kind": "system", "level": "ok", "text": L("Отправка возобновлена", "Sending resumed")},
        {"at": ago(minutes=205), "kind": "dm", "level": "bad", "text": L("@gleb.ops: Instagram отклонил сообщение — человек закрыл личные сообщения", "@gleb.ops: Instagram rejected the message: this person has closed their DMs"), "automation": "arena"},
        {"at": ago(hours=5, minutes=5), "kind": "alert", "level": "info", "text": L("Ежедневная сводка отправлена в Telegram", "Daily summary sent to Telegram")},
        {"at": ago(hours=5, minutes=30), "kind": "system", "level": "ok", "text": L("Вы разрешили 50 отправок (ступень запуска 3 из 4)", "You allowed 50 sends (launch step 3 of 4)")},
        {"at": ago(hours=6), "kind": "system", "level": "ok", "text": L("Ступень на 10 отправок выполнена: 10 отправлено, 0 отказов", "Step of 10 sends done: 10 sent, 0 refused")},
        {"at": ago(hours=7, minutes=20), "kind": "system", "level": "ok", "text": L("Вы разрешили 10 отправок (ступень запуска 2 из 4)", "You allowed 10 sends (launch step 2 of 4)")},
        {"at": ago(days=1, hours=2), "kind": "alert", "level": "bad", "text": L("4 человека не успели получить ссылку: прошло 7 дней с их комментария", "4 people missed the link: 7 days have passed since their comment")},
        {"at": ago(days=1, hours=3), "kind": "system", "level": "info", "text": L("Изменены настройки автоматизации «Arena Skill»: добавлена опечатка «arrna»", "Settings of “Arena Skill” changed: typo “arrna” added")},
        {"at": ago(days=1, hours=3, minutes=1), "kind": "system", "level": "warn", "text": L("Настройки изменились — отправка остановлена, пока вы не разрешите её заново", "Settings changed: sending is stopped until you allow it again")},
        {"at": ago(days=2, hours=5), "kind": "system", "level": "ok", "text": L(f"Добавлена автоматизация «{guide_name}»", f"Automation “{guide_name}” added")},
    ]

    telegram = [
        {"at": ago(minutes=96), "text": L("⚠ Нужна проверка: 1 человек\n@yana_qa (Arena Skill) — Instagram не ответил вовремя, неизвестно, дошло ли сообщение.\nЧто сделать: откройте директ и посмотрите, есть ли там сообщение. Второй раз бот не напишет.",
                                          "⚠ Needs a check: 1 person\n@yana_qa (Arena Skill): Instagram did not answer in time, the message may not have arrived.\nWhat to do: open your DMs and see if the message is there. The bot will not send it twice.")},
        {"at": ago(minutes=97), "text": L("⏸ Отправка на паузе на 20 минут: неясный ответ Instagram. Возобновится сама.",
                                          "⏸ Sending paused for 20 minutes: unclear answer from Instagram. It resumes by itself.")},
        {"at": ago(minutes=117), "text": L("✓ Отправка возобновлена", "✓ Sending resumed")},
        {"at": ago(hours=5, minutes=5), "text": L(f"Сводка за сутки\nОтправлено: 412 · в очереди: 23 · не успели: 0\nArena Skill — 371 · {guide_name} — 41\nПодписчики: 12 480 (+212 за сутки)",
                                                  f"Daily summary\nSent: 412 · in queue: 23 · missed: 0\nArena Skill: 371 · {guide_name}: 41\nFollowers: 12,480 (+212 today)")},
        {"at": ago(hours=6), "text": L("✓ Ступень на 10 отправок выполнена: 10 отправлено, 0 отказов.\nЧтобы продолжить, разрешите следующую ступень.",
                                       "✓ Step of 10 sends done: 10 sent, 0 refused.\nAllow the next step to continue.")},
        {"at": ago(days=1, hours=2), "text": L("⚠ 4 человека не успели получить ссылку: прошло 7 дней с их комментария. Всего не успели: 41.",
                                               "⚠ 4 people missed the link: 7 days have passed since their comment. Missed in total: 41.")},
    ]

    scenarios = {
        "attention": {
            "label": L("Нужна проверка", "Needs a check"),
            "service": {"running": True, "last_cycle_sec": 40, "sending": True, "budget": 38, "armed_n": 50,
                        "account_hold": None, "send_hold": None, "meta_ok": True, "telegram_ok": True},
            "problems": [
                {"key": "uncertain_manual", "level": "warn", "title": L("2 человека: неизвестно, дошло ли сообщение", "2 people: not sure the message arrived"),
                 "text": L("Instagram не подтвердил доставку. Бот не пишет второй раз, чтобы никто не получил два сообщения.",
                           "Instagram did not confirm delivery. The bot never sends twice, so nobody gets two messages."),
                 "action": L("Откройте директ и проверьте переписку с @yana_qa и @vera.notes, затем отметьте «Проверено».",
                             "Open your DMs, check the chats with @yana_qa and @vera.notes, then mark them “Checked”."),
                 "link": "#/people?status=uncertain", "link_text": L("Показать этих людей", "Show these people")},
                {"key": "expiring", "level": "info", "title": L("23 человека ждут ссылку", "23 people are waiting for the link"),
                 "text": L("Очередь идёт с разрешённой скоростью 60 сообщений в час. Все успеют: до конца окна в 7 дней у ближайшего 6 дней 23 часа.",
                           "The queue moves at the allowed 60 messages an hour. Everyone will make it: the earliest 7-day window closes in 6 days 23 hours."),
                 "action": L("Ничего делать не нужно.", "Nothing to do."), "link": "#/people?status=pending",
                 "link_text": L("Показать очередь", "Show the queue")},
            ],
        },
        "ok": {
            "label": L("Всё хорошо", "All good"),
            "service": {"running": True, "last_cycle_sec": 40, "sending": True, "budget": "unlimited", "armed_n": "unlimited",
                        "account_hold": None, "send_hold": None, "meta_ok": True, "telegram_ok": True},
            "problems": [],
        },
        "blocked": {
            "label": L("Instagram закрыл доступ", "Instagram blocked access"),
            "service": {"running": True, "last_cycle_sec": 55, "sending": True, "budget": 38, "armed_n": 50,
                        "account_hold": "blocked", "send_hold": None, "meta_ok": False, "telegram_ok": True},
            "problems": [
                {"key": "hold:account", "level": "bad", "title": L("Instagram отклоняет запросы аккаунта", "Instagram is rejecting the account's requests"),
                 "text": L("Meta отвечает «API access blocked». Бот ничего не читает и никому не пишет; он сам проверяет доступ каждые 30 минут и продолжит, когда доступ вернётся.",
                           "Meta answers “API access blocked”. The bot reads nothing and messages nobody; it checks access every 30 minutes and carries on once access is back."),
                 "action": L("Зайдите на developers.facebook.com и подтвердите аккаунт разработчика (Confirm Account).",
                             "Go to developers.facebook.com and confirm your developer account (Confirm Account)."),
                 "link": "#/settings", "link_text": L("Открыть настройки", "Open settings")},
                {"key": "waiting", "level": "warn", "title": L("23 человека ждут ссылку и пока её не получат", "23 people are waiting and will not get the link yet"),
                 "text": L("У ближайшего окно в 7 дней закрывается через 2 дня 4 часа. После этого Instagram не разрешит написать.",
                           "The earliest 7-day window closes in 2 days 4 hours. After that Instagram will not allow a message."),
                 "action": L("Восстановите доступ к Meta раньше этого срока.", "Restore access to Meta before then."), "link": "#/people?status=pending",
                 "link_text": L("Показать очередь", "Show the queue")},
            ],
        },
    }

    return {
        "demo": True, "generated_at": iso(NOW),
        "account": {"username": L("your_account", "your.account"), "name": L("Alex", "Alex"),
                    "followers": {"now": 12480, "day_ago": 12268, "week_ago": 10911}},
        "limits": {"max_sends_per_hour": 60, "window_days": 7, "poll_interval_sec": 90, "late_after_minutes": 60,
                   "greeting": L("Привет! 👋", "Hi! 👋"), "late_greeting": L("Привет! 👋 Лучше поздно, чем никогда 😄", "Hi! 👋 Better late than never 😄"),
                   "public_reply": True, "public_phrases": phrases,
                   "alerts": {"repeat_min": 120, "digest_hour_msk": 9},
                   "token_expires_in_days": 52, "version": L("демо", "demo"), "stages": [1, 10, 50, "unlimited"]},
        "scenarios": scenarios, "automations": automations, "people": people, "hours": hours,
        "days": days, "activity": activity, "telegram": telegram,
    }


for LANG, name in (("ru", "demo.json"), ("en", "demo.en.json")):
    data = build()
    out = Path(__file__).resolve().parent.parent / "data" / name
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(out, len(data["people"]), "people")
