import json
import logging
import os
import re
import sqlite3
import subprocess
import time
from contextlib import closing
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from html import escape
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

BANKS = ("Т-Банк", "Озон Банк", "ВБ Банк", "Яндекс Пэй")
BASE = "все покупки"
GROUPS = {
    "супермаркеты": "супермаркеты|продукты|продуктовые магазины|пятерочка|пятерочку|пятерочке|магнит|магните|дикси|перекресток|"
                    "лента|вкусвилл|ашан|окей|глобус|купить молоко|молоко|хлеб|яйца",
    "азс": "азс|заправки|заправка|бензин|топливо|лукойл|лукойле|роснефть|газпромнефть",
    "аптеки": "аптеки|аптека|аптеку|аптеке|лекарства|горздрав|ригла|планета здоровья|еаптека",
    "кафе и рестораны": "кафе и рестораны|кафе бары и рестораны|рестораны|ресторан|бар|бары|кафе|рестораны и кафе|кофейни|кофейня",
    "фастфуд": "фастфуд|быстрое питание|вкусно и точка|бургер кинг|ростикс|kfc",
    "такси": "такси|яндекс такси|яндекс го|ситимобил",
    "маркетплейсы": "маркетплейсы|маркетплейс|ozon|озон|wildberries|вайлдберриз|вб|яндекс маркет",
    "зоотовары": "зоотовары|питомцы|зоомагазины|зоомагазин|четыре лапы|бетховен|корм для кошки",
    "одежда и обувь": "одежда и обувь|одежда|обувь|спортмастер|lamoda|ламода",
    "кино": "кино|кинотеатры|кинотеатр",
    "транспорт": "транспорт|метро|автобус|трамвай|троллейбус",
    "книги и канцтовары": "книги и канцтовары|книги|книга|книгу|канцтовары|книжный магазин",
    "подарки и творчество": "подарки и творчество|магазин подарков|товары для творчества",
    "супермаркеты в городе": "супермаркеты в городе|продукты в т городе|супермаркеты в т городе",
    "красота": "красота|косметика|золотое яблоко|летуаль|рив гош",
}
ALIASES = {alias: category for category, aliases in GROUPS.items() for alias in aliases.split("|")}
ALIASES.update({"на все": BASE, "на все покупки": BASE, "базовый": BASE})
RATE = re.compile(r"(?<![\w.,−–—-])(\d{1,3}(?:[.,]\d{1,2})?)\s*%")
LINE = re.compile(r"(.{1,80}?)\s+(\d{1,3}(?:[.,]\d{1,2})?)\s*%")
MONTH = re.compile(r"20\d{2}-(?:0[1-9]|1[0-2])")
MONEY = re.compile(r"[,\s]+(\d+(?:[ \u00a0]\d{3})*(?:[.,]\d{1,2})?)\s*(?:₽|руб(?:лей|ля|ль)?\.?)?$", re.I)
HELP = (
    "Пришли скриншот с активными категориями и выбери банк. "
    "Я покажу распознанные строки для проверки.\n\n"
    "Для подбора напиши название магазина или покупку, например «Магнит, 3000 ₽» или «корм для кота».\n"
    "/cards — категории за этот месяц\n"
    "/checkout — режим «У кассы»\n"
    "/add — обновить категории месяца\n"
    "/month ГГГГ-ММ — выбрать месяц для ввода\n"
    "/cancel — отменить редактирование"
)


def normalize(value):
    return " ".join(re.findall(r"[a-zа-я0-9]+", value.lower().replace("ё", "е").replace("+", " плюс ")))


def load_knowledge():
    data = json.loads(Path(__file__).with_name("knowledge.json").read_text(encoding="utf-8"))
    return [
        {normalize(phrase): category for category, phrases in data[group].items() for phrase in phrases}
        for group in ("merchants", "purchases")
    ]


MERCHANTS, PURCHASES = load_knowledge()


def month_now():
    return datetime.now(ZoneInfo("Europe/Moscow")).strftime("%Y-%m")


def moscow_today():
    return datetime.now(ZoneInfo("Europe/Moscow")).date().isoformat()


def percent(value):
    return Decimal(value.replace(",", "."))


def parse_rates(value):
    if len(value) > 2500:
        raise ValueError("Слишком много текста. Пришли до 30 строк.")
    rates = {}
    for line in value.strip().splitlines():
        match = LINE.fullmatch(line.strip())
        if not match or "%" in match[1] or not normalize(match[1]) or percent(match[2]) > 100:
            raise ValueError("В каждой строке укажи категорию и процент, например «Аптеки 5%».")
        name = normalize(match[1])
        if name in rates:
            raise ValueError("Повторяется категория. Оставь для неё одну строку.")
        rates[name] = str(percent(match[2]))
    if not 1 <= len(rates) <= 30:
        raise ValueError("Укажи от 1 до 30 категорий.")
    return rates


def rate_lines(rates):
    return "\n".join(f"{name.capitalize()} {rate.replace('.', ',')}%" for name, rate in rates.items())


def recognize(image):
    local_ocr = os.environ.get("CASHBACK_OCR")
    command = [local_ocr] if local_ocr else ["tesseract", "stdin", "stdout", "-l", "rus+eng", "--psm", "11"]
    result = subprocess.run(
        command,
        input=image, capture_output=True, timeout=25, check=True,
        env={**os.environ, "OMP_THREAD_LIMIT": "1"},
    )
    lines = [line.strip() for line in result.stdout.decode().splitlines() if line.strip()]
    rates = {}
    in_yandex_taxi = False
    for index, line in enumerate(lines):
        heading = normalize(line)
        if "кешбэк в яндекс такси" in heading or "кэшбэк в яндекс такси" in heading:
            in_yandex_taxi = True
        if heading.startswith(("больше кешбэка от партнеров", "выгода от партнеров")):
            break
        matches = list(RATE.finditer(line))
        if len(matches) != 1 or percent(matches[0][1]) > 100:
            continue
        match = matches[0]
        if re.search(r"[-−–—]\s*$", line[:match.start()]):
            continue
        name = normalize(line[:match.start()] + " " + line[match.end():])
        if not name:
            nearby = [
                normalize(lines[position])
                for position in (index + 1, index - 1)
                if 0 <= position < len(lines) and "%" not in lines[position]
            ]
            name = next((label for label in nearby if label in ALIASES), "")
            if not name and index + 1 < len(lines) and "%" not in lines[index + 1]:
                name = normalize(lines[index + 1])
        name = re.sub(r"\bперейти$", "", name).strip()
        name = re.sub(r"^(?:p|0) ", "", name)
        name = {"на все 0": "на все", "питомцы о": "питомцы"}.get(name, name)
        if name == "кафе бары и рестораны плюс":
            name = "кафе бары и рестораны"
        if name == "кафе и рестораны" and index + 1 < len(lines) and normalize(lines[index + 1]).startswith("за уровень"):
            continue
        if name == "супермаркеты" and index + 1 < len(lines) and normalize(lines[index + 1]) == "в городе":
            name = "супермаркеты в городе"
        if name.startswith("пятерочка доставка"):
            continue
        if name in {"комфорт", "комфорт плюс", "ultima"}:
            if not in_yandex_taxi:
                continue
            name = "яндекс такси " + name
        if name and len(name) < 80 and re.search(r"[а-яa-z]", name):
            rates[name] = str(percent(match[1]))
    return dict(list(rates.items())[:30])


def has_phrase(text, phrase):
    return f" {phrase} " in f" {text} "


def split_purchase(query):
    money = MONEY.search(query)
    if money and len(money[1]) <= 20 and (
        money[0].strip().startswith(",") or re.search(r"₽|руб", money[0], re.I)
    ):
        amount = percent(re.sub(r"\s", "", money[1]))
        return query[:money.start()].strip(), amount
    return query, None


def recommend(query, cards, offers=(), today=None, *, override=None, compare=False):
    if not cards:
        return "Сначала добавь категории на текущий месяц. Пришли скриншот или используй /add."
    purchase, amount = split_purchase(query)
    query = normalize(purchase)
    applicable_offers = [
        offer for offer in offers if offer["bank"] in cards
        and (not offer.get("through") or (today or moscow_today()) <= offer["through"])
    ]
    merchant_offers = [
        offer for offer in applicable_offers
        if any(has_phrase(query, phrase) for phrase in offer.get("phrases", ()))
    ]
    labels = {name for rates in cards.values() for name in rates if ALIASES.get(name, name) != BASE}
    matches = {
        phrase for phrase in set(ALIASES) | labels | MERCHANTS.keys() | PURCHASES.keys()
        if has_phrase(query, phrase)
    }
    matches = {phrase for phrase in matches if not any(
        phrase != other and has_phrase(other, phrase) for other in matches
    )}
    known = {ALIASES[phrase] for phrase in matches if phrase in ALIASES} - {BASE}
    merchants = {MERCHANTS[phrase] for phrase in matches if phrase in MERCHANTS}
    purchases = {PURCHASES[phrase] for phrase in matches if phrase in PURCHASES}
    exact = {phrase for phrase in matches if phrase in labels}
    if exact and all(phrase.startswith("яндекс такси ") for phrase in exact):
        known.discard("такси")
        merchants.discard("такси")
    categories = (
        (known & {"супермаркеты в городе"})
        or merchants or known or exact or purchases
    )
    if override:
        categories = {override}
        exact = set()
    if len(categories) > 1:
        return "Уточни магазин или одну категорию: " + ", ".join(escape(category) for category in sorted(categories)) + "."
    category = next(iter(categories), None)
    if category is None and merchant_offers:
        category = merchant_offers[0]["label"]
    fallback = category is None and not any(ALIASES.get(phrase, phrase) == BASE for phrase in matches)
    category = category or BASE

    choices = []
    for bank, rates in cards.items():
        applicable = [
            Decimal(rate) for name, rate in rates.items()
            if ALIASES.get(name, name) in {BASE, category} or name in exact
        ]
        best = max(applicable, default=Decimal(0))
        if compare or best > 0:
            choices.append((best, bank))

    promos = [
        offer for offer in applicable_offers
        if offer in merchant_offers or offer.get("category") == category
    ]
    if not choices and not promos:
        if category == BASE:
            return "Категорию определить не удалось. Ставки «на всё» пока не добавлены." if fallback else "Ставки «на всё» пока не добавлены."
        return f"Для категории «{escape(category)}» пока нет сохранённых ставок."

    if compare:
        lines = ["Сравнение карт · " + escape(category), ""]
    elif fallback:
        lines = ["По ставке «на всё»", ""]
    else:
        lines = [escape(category[:1].upper() + category[1:]), ""]

    for offer in sorted(promos, key=lambda item: (-Decimal(item["rate"]), item["bank"])):
        discount = offer.get("discount", False)
        kind = "скидка" if discount else "кэшбэк"
        lines.append(format_rate(offer["bank"], Decimal(offer["rate"]), amount, kind, discount))
        lines.append(escape(offer["condition"][0].upper() + offer["condition"][1:].rstrip(".")) + ".")
        lines.append("")

    if choices:
        best_rate = max(rate for rate, _ in choices)
        if promos:
            lines.append("Без акции")
        lines.extend(
            format_rate(bank, rate, amount) for rate, bank in sorted(choices, key=lambda item: (-item[0], item[1]))
            if compare or rate == best_rate
        )
    if category == "зоотовары" and (merchants == {"зоотовары"} or override):
        lines.extend(["", "Ставка зависит от категории операции у банка."])
    return "\n".join(lines).strip()


def format_rate(bank, rate, amount, kind="", discount=False):
    points = bank == "Яндекс Пэй" and not discount
    rate_text = format(rate.normalize(), "f").replace(".", ",")
    label = f"{kind} {rate_text}%".strip()
    line = f"<b>{escape(bank)} — {label}</b>"
    if amount is not None:
        estimate = (rate * amount / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        formatted = f"{estimate:,.2f}".replace(",", " ").replace(".", ",")
        unit = "баллов Плюса" if points else "₽"
        line += f" · ≈ {formatted} {unit}"
    elif points:
        line += " · баллы Плюса"
    return line


class Telegram:
    def __init__(self, token):
        self.api_url = f"https://api.telegram.org/bot{token}/"
        self.file_url = f"https://api.telegram.org/file/bot{token}/"

    def call(self, method, **payload):
        request = Request(self.api_url + method, json.dumps(payload).encode(),
                          {"Content-Type": "application/json"})
        with urlopen(request, timeout=45) as response:
            data = json.load(response)
        if not data.get("ok"):
            raise RuntimeError("Telegram API error")
        return data["result"]

    def image(self, file_id):
        info = self.call("getFile", file_id=file_id)
        limit = 8 * 1024 * 1024
        if info.get("file_size", 0) > limit:
            raise ValueError("Пришли скриншот размером до 8 МБ.")
        with urlopen(self.file_url + info["file_path"], timeout=25) as response:
            content = response.read(limit + 1)
        if len(content) > limit:
            raise ValueError("Пришли скриншот размером до 8 МБ.")
        return content


class Bot:
    def __init__(self, api, db, owner):
        self.api, self.db, self.owner = api, db, owner
        db.executescript("""
            CREATE TABLE IF NOT EXISTS cards (
                month TEXT, bank TEXT, rates TEXT, PRIMARY KEY (month, bank)
            );
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS personal_categories (
                purchase TEXT PRIMARY KEY, category TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS recent_purchases (
                purchase TEXT PRIMARY KEY, label TEXT NOT NULL, position INTEGER NOT NULL
            );
        """)
        seed_path = Path(__file__).with_name("november-2026.json")
        self.seed = json.loads(seed_path.read_text(encoding="utf-8")) if seed_path.is_file() else {}
        with db:
            self.seed_rates()

    def seed_rates(self):
        if not self.seed or self.get("november_2026_imported"):
            return
        self.db.executemany(
            "INSERT OR IGNORE INTO cards VALUES (?, ?, ?)",
            [(self.seed["month"], bank, json.dumps(rates))
             for bank, rates in self.seed["cards"].items() if bank in BANKS],
        )
        self.put("november_2026_imported", True)

    def get(self, key):
        row = self.db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, json.dumps(value)))

    def cards(self, month):
        return {
            bank: json.loads(rates) for bank, rates in self.db.execute(
                "SELECT bank, rates FROM cards WHERE month = ? ORDER BY bank", (month,)
            ) if bank in BANKS
        }

    def reply(self, text, buttons=None, *, html=False, columns=1):
        markup = {"remove_keyboard": True}
        if buttons:
            markup = {
                "keyboard": [buttons[index:index + columns] for index in range(0, len(buttons), columns)],
                "resize_keyboard": True,
            }
        self.api.call("sendMessage", chat_id=self.owner, text=text, reply_markup=markup,
                      **({"parse_mode": "HTML"} if html else {}))

    def lookup(self, query, *, compare=False):
        current = month_now()
        offers = self.seed.get("offers", ()) if current == self.seed.get("month") else ()
        label, _ = split_purchase(query)
        key = normalize(label)
        row = self.db.execute(
            "SELECT category FROM personal_categories WHERE purchase = ?", (key,),
        ).fetchone()
        self.put("last_purchase", query)
        if key and len(label) <= 80 and not compare:
            self.db.execute("""
                INSERT OR REPLACE INTO recent_purchases VALUES (
                    ?, ?, (SELECT COALESCE(MAX(position), 0) + 1 FROM recent_purchases)
                )
            """, (key, label))
            self.db.execute("""
                DELETE FROM recent_purchases WHERE purchase NOT IN (
                    SELECT purchase FROM recent_purchases ORDER BY position DESC LIMIT 5
                )
            """)
        text = recommend(query, self.cards(current), offers, override=row[0] if row else None,
                         compare=compare)
        self.reply(text, ["Сравнить карты", "Уточнить категорию", "У кассы"], html=True)

    def checkout(self):
        recent = [
            label for (label,) in self.db.execute(
                "SELECT label FROM recent_purchases ORDER BY position DESC LIMIT 5"
            )
        ]
        buttons = list(dict.fromkeys(["Продукты", "Аптека", "Кафе", *recent, "Обновить месяц"]))
        self.reply("У кассы\nВыбери покупку или напиши магазин.", buttons, columns=2)

    def month_menu(self, month):
        saved = self.cards(month)
        self.put("draft", {"month": month})
        self.reply(
            f"Категории · {month}\nГотово {len(saved)} из {len(BANKS)}. Выбери банк.",
            [f"{'✅' if bank in saved else '⬜'} {bank}" for bank in BANKS] + ["/cancel"],
        )

    def import_screenshots(self, draft, files):
        combined = dict(draft.get("rates", {}))
        failed = False
        for file_id in files:
            try:
                rates = recognize(self.api.image(file_id))
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
                logging.warning("Screenshot recognition failed")
                rates = {}
            if len(combined.keys() | rates.keys()) > 30:
                self.reply("В списке уже 30 категорий. Сохрани его или исправь вручную.")
                failed = True
                continue
            combined.update(rates)
            failed |= not bool(rates)
        if combined:
            draft["rates"] = combined
            draft.pop("editing", None)
            self.review(draft, failed=failed)
        else:
            self.put("draft", draft)
            self.reply("Категории не считались. Пришли ещё скриншот или введи ставки строками: Аптеки 5%.")

    def review(self, draft, *, failed=False):
        self.put("draft", draft)
        self.reply(
            f"{draft['bank']} · {draft['month']}\n\n{rate_lines(draft['rates'])}\n\n"
            + ("Часть скриншотов не добавилась. Проверь список.\n" if failed else "")
            + "Можно прислать ещё скриншоты. Сохранение заменит список этой карты за месяц.",
            ["Сохранить", "Исправить", "/cancel"],
        )

    def handle(self, message):
        if message["chat"]["type"] != "private":
            return
        sender = message.get("from", {}).get("id")
        if sender != self.owner:
            if message.get("text") == "/start" and sender:
                self.api.call("sendMessage", chat_id=sender, text=f"Твой Telegram ID: {sender}")
            return
        text = message.get("text", "").strip()
        draft = self.get("draft") or {}
        if draft.get("bank") and draft["bank"] not in BANKS:
            self.put("draft", None)
            draft = {}
        month = self.get("input_month") or month_now()
        if text in {"/help", "/start"}:
            self.reply(HELP, ["У кассы", "Обновить месяц"])
        elif text == "/cancel":
            self.put("draft", None)
            self.put("correction", None)
            self.reply("Редактирование отменено.")
        elif text == "/cards":
            current_month = month_now()
            notes = self.seed["notes"] if current_month == self.seed.get("month") else {}
            blocks = []
            for bank, rates in self.cards(current_month).items():
                block = f"{bank}\n{rate_lines(rates)}"
                note = notes.get(bank)
                if bank == "ВБ Банк" and any(ALIASES.get(name) == "кафе и рестораны" for name in rates):
                    note = None
                blocks.append(f"{block}\n\n{note}" if note else block)
            self.reply(f"Категории за {month_now()}" if blocks else "На текущий месяц категорий пока нет.")
            for block in blocks:
                self.reply(block)
        elif text == "/month" or text.startswith("/month "):
            value = text.removeprefix("/month").strip()
            if value and not MONTH.fullmatch(value):
                self.reply("Введи месяц в формате /month ГГГГ-ММ. /month без даты вернёт текущий месяц.")
                return
            self.put("input_month", value or None)
            self.put("draft", None)
            self.put("correction", None)
            self.reply(f"Месяц для ввода: {value or month_now()}. Пришли скриншот или используй /add.")
        elif text in {"/add", "Обновить месяц"}:
            self.put("correction", None)
            self.month_menu(month)
        elif text in {"/checkout", "У кассы"} and not draft:
            self.put("correction", None)
            self.checkout()
        elif text == "Сравнить карты" and not draft:
            last = self.get("last_purchase")
            if last:
                self.lookup(last, compare=True)
        elif text == "Уточнить категорию" and not draft:
            last = self.get("last_purchase")
            if last and normalize(split_purchase(last)[0]):
                self.put("correction", last)
                categories = sorted({
                    *GROUPS, *MERCHANTS.values(), *PURCHASES.values(),
                    *(ALIASES.get(name, name) for rates in self.cards(month_now()).values() for name in rates),
                    BASE,
                })
                self.reply("Выбери категорию для этой покупки или введи её название.", categories + ["/cancel"], columns=2)
        elif text and not draft and self.get("correction"):
            last = self.get("correction")
            category = normalize(text)
            if not category or len(category) > 80:
                self.reply("Введи название категории длиной до 80 символов.")
                return
            category = ALIASES.get(category, category)
            self.db.execute("INSERT OR REPLACE INTO personal_categories VALUES (?, ?)",
                            (normalize(split_purchase(last)[0]), category))
            self.put("correction", None)
            self.lookup(last)
        elif message.get("photo") or message.get("document", {}).get("mime_type", "") in ("image/png", "image/jpeg"):
            self.put("correction", None)
            photo = message["photo"][-1] if message.get("photo") else message["document"]
            draft = draft or {"month": month}
            if draft.get("bank"):
                self.import_screenshots(draft, [photo["file_id"]])
            else:
                files = draft.setdefault("files", [])
                if len(files) >= 10:
                    self.reply("Выбери банк для уже полученных скриншотов.", BANKS)
                    return
                files.append(photo["file_id"])
                self.put("draft", draft)
                self.reply(f"Скриншотов: {len(files)}. Выбери банк для {draft['month']}.", BANKS)
        elif text.removeprefix("✅ ").removeprefix("⬜ ") in BANKS and draft and "bank" not in draft:
            draft["bank"] = text.removeprefix("✅ ").removeprefix("⬜ ")
            files = draft.pop("files", [])
            old_file = draft.pop("file", None)
            if old_file:
                files.insert(0, old_file)
            if files:
                self.import_screenshots(draft, files)
            else:
                self.put("draft", draft)
                self.reply("Пришли скриншоты или введи ставки строками: Аптеки 5%.")
        elif text == "Сохранить" and draft.get("rates") and not draft.get("editing"):
            self.db.execute("INSERT OR REPLACE INTO cards VALUES (?, ?, ?)",
                            (draft["month"], draft["bank"], json.dumps(draft["rates"])))
            self.put("draft", None)
            count = len(self.cards(draft["month"]))
            self.reply(
                f"Сохранено · {draft['bank']} · {draft['month']}\nГотово {count} из {len(BANKS)}.",
                ["Обновить месяц", "У кассы"],
            )
        elif text == "Исправить" and draft.get("rates"):
            draft["editing"] = True
            self.put("draft", draft)
            self.reply("Пришли весь исправленный список, по одной категории на строку.\n\n"
                       + rate_lines(draft["rates"]))
        elif draft.get("bank") and text and (draft.get("editing") or not draft.get("rates")):
            try:
                draft["rates"] = parse_rates(text)
            except ValueError as error:
                self.reply(str(error))
                return
            draft.pop("editing", None)
            self.review(draft)
        elif draft and "bank" not in draft:
            self.reply("Выбери банк или нажми /cancel.", BANKS)
        elif draft.get("rates"):
            self.reply("Подтверди список или нажми /cancel.", ["Сохранить", "Исправить", "/cancel"])
        elif text:
            self.lookup(text)
        else:
            self.reply("Пришли скриншот с категориями или напиши название магазина.")


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token):
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in the service configuration")
    api = Telegram(token)
    owner = int(os.environ.get("TELEGRAM_OWNER_ID", "0"))
    with closing(sqlite3.connect(os.environ.get("CASHBACK_DB", "cashback.sqlite3"))) as db:
        bot = Bot(api, db, owner)
        bot_info = api.call("getMe")
        logging.info("Polling as @%s", bot_info["username"])
        offset = bot.get("offset")
        while True:
            try:
                updates = api.call("getUpdates", offset=offset, timeout=25, allowed_updates=["message"])
                for update in updates:
                    with db:
                        if "message" in update:
                            bot.handle(update["message"])
                        next_offset = update["update_id"] + 1
                        bot.put("offset", next_offset)
                    offset = next_offset
            except (OSError, ValueError, RuntimeError) as error:
                logging.warning("Telegram request failed (%s, HTTP %s)",
                                type(error).__name__, getattr(error, "code", "—"))
                time.sleep(5)


if __name__ == "__main__":
    main()
