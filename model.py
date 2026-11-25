"""Build a reproducible EUR distribution from MTGJSON sheets and Scryfall prices."""
import argparse
import bisect
import gzip
import json
import random
from collections import Counter
from decimal import Decimal
from itertools import accumulate
from pathlib import Path

import storage

SAMPLE_SIZE = 100_000


def chance(payload, price_eur):
    if not payload or price_eur is None:
        return None
    totals, cumulative = payload["totals"], payload["cumulative"]
    index = bisect.bisect_left(totals, price_eur * 100)
    below = cumulative[index - 1] if index else 0
    return Decimal(100 * (payload["size"] - below)) / payload["size"]


def simulate(config, cards, size=SAMPLE_SIZE):
    if size < 1:
        raise ValueError("Размер выборки должен быть положительным")
    layouts = config["boosters"]
    weights = [layout["weight"] for layout in layouts]
    if not weights or any(weight <= 0 for weight in weights):
        raise ValueError("Вес каждой конфигурации должен быть положительным")
    sheet_names = {name for pack in layouts for name in pack["contents"]}
    sheets = {}
    for name in sorted(sheet_names):
        sheet = config["sheets"][name]
        ids, card_weights = zip(*sheet["cards"].items())
        if any(weight <= 0 for weight in card_weights):
            raise ValueError(f"Некорректные веса листа {name}")
        missing = set(ids) - cards.keys()
        if missing:
            ids_preview = ", ".join(sorted(missing)[:4])
            raise ValueError(f"Не найдены UUID листа {name}: {ids_preview} ({len(missing)}). Добавь JSON связанных выпусков.")
        foil = sheet["foil"]
        versions = [(card_id, foil) for card_id in ids]
        prices = [cards[card_id][2 if foil else 1] for card_id in ids]
        sheets[name] = (versions, card_weights, prices)
    for layout in layouts:
        for name, count in layout["contents"].items():
            if not isinstance(count, int) or count < 1:
                raise ValueError(f"Некорректное число слотов листа {name}")

    # The analytical at-least-one probabilities use the same independent slot model
    # as the simulated total price distribution.
    per_card = Counter()
    total_weight = sum(weights)
    for layout, layout_weight in zip(layouts, weights):
        misses = {}
        for name, count in layout["contents"].items():
            versions, card_weights, _ = sheets[name]
            sheet_weight = sum(card_weights)
            for version, weight in zip(versions, card_weights):
                misses[version] = misses.get(version, 1) * (1 - weight / sheet_weight) ** count
        for version, miss in misses.items():
            per_card[version] += (1 - miss) * layout_weight / total_weight

    accumulated = {name: list(accumulate(sheet[1])) for name, sheet in sheets.items()}
    priced_sheets = {name: [price or 0 for price in sheet[2]] for name, sheet in sheets.items()}
    rng = random.Random(451)
    choices = rng.choices(layouts, weights, k=size)
    totals = Counter()
    for layout in choices:
        value = 0
        for name, count in layout["contents"].items():
            value += sum(rng.choices(priced_sheets[name], cum_weights=accumulated[name], k=count))
        totals[value] += 1

    rows = []
    missing_prices = 0
    for (card_id, foil), probability in per_card.items():
        name, regular, foil_price, number, code = cards[card_id]
        price = foil_price if foil else regular
        missing_prices += price is None
        rows.append(dict(name=name, foil=foil, code=code, number=number,
                         probability=round(100 * probability, 10), cents=price))
    rows.sort(key=lambda card: (-(card["cents"] if card["cents"] is not None else -1),
                                card["name"], card["code"], card["number"]))
    distinct = sorted(totals)
    return dict(totals=distinct, cumulative=list(accumulate(totals[t] for t in distinct)),
                size=size, cards=rows, priced=len(rows) - missing_prices)


def open_json(path):
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def iter_scryfall(path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        first = stream.read(1)
        stream.seek(0)
        if first == "[":
            yield from json.load(stream)
        else:
            for line in stream:
                if line.strip():
                    yield json.loads(line)


def cents(value):
    if value is None:
        return None
    price = Decimal(value)
    if not price.is_finite() or price < 0 or price * 100 != (price * 100).to_integral_value():
        raise ValueError(f"Некорректная цена карты: {value}")
    return int(price * 100)


def main():
    parser = argparse.ArgumentParser(description="Оценить шанс окупаемости по MTGJSON и Scryfall")
    parser.add_argument("product", help="Путь продукта из каталога TCG Balance")
    parser.add_argument("set_json", type=Path, help="JSON основного выпуска MTGJSON")
    parser.add_argument("prices_json", type=Path, help="Scryfall Default Cards (JSON или JSONL, включая .gz)")
    parser.add_argument("--extra-set", action="append", default=[], type=Path)
    parser.add_argument("--model-key", help="Точный ключ booster в MTGJSON. По умолчанию последний сегмент пути.")
    args = parser.parse_args()
    storage.init()
    with storage.connect() as db:
        product = db.execute("SELECT * FROM products WHERE path=?", (args.product,)).fetchone()
    if not product:
        parser.error("Продукт отсутствует в каталоге")
    source = open_json(args.set_json)
    set_data = source["data"]
    if set_data["code"].upper() != product["code"]:
        parser.error("Код выпуска в MTGJSON отличается от кода продукта")
    key = args.model_key or args.product.rsplit("/", 1)[-1]
    configs = set_data.get("booster", {})
    if key not in configs:
        parser.error("Доступные модели: " + (", ".join(configs) or "отсутствуют"))
    set_files = [set_data, *(open_json(path)["data"] for path in args.extra_set)]
    card_index = {card["uuid"]: card for set_file in set_files for card in set_file["cards"]}
    config = configs[key]
    required = {card_id for layout in config["boosters"] for sheet in layout["contents"]
                for card_id in config["sheets"][sheet]["cards"]}
    ids = {card_index[i].get("identifiers", {}).get("scryfallId") for i in required if i in card_index}
    price_guide = {}
    for card in iter_scryfall(args.prices_json):
        if card["id"] in ids:
            price_guide[card["id"]] = card["prices"]
    cards = {}
    for card_id in required & card_index.keys():
        card = card_index[card_id]
        prices = price_guide.get(card.get("identifiers", {}).get("scryfallId"), {})
        cards[card_id] = (card["name"], cents(prices.get("eur")), cents(prices.get("eur_foil")),
                          str(card["number"]), card["setCode"])
    if not any(regular is not None or foil is not None for _, regular, foil, _, _ in cards.values()):
        parser.error("В файле Scryfall не нашлось цен EUR для модели")
    # Missing prices contribute zero to the simulation, but stay missing in the card list.
    payload = simulate(config, cards)
    if not payload["priced"]:
        parser.error("В файле Scryfall отсутствуют цены EUR для нужных версий карт")
    payload.update(model_key=key, mtgjson_date=source.get("meta", {}).get("date"),
                   prices_file=args.prices_json.name)
    with storage.connect() as db:
        db.execute("INSERT OR REPLACE INTO models VALUES (?, ?, ?)",
                   (args.product, storage.now(), json.dumps(payload, ensure_ascii=False)))
    print(f"Сохранено: {len(payload['cards'])} версий карт, {payload['size']} симуляций")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError, ArithmeticError) as exc:
        raise SystemExit(str(exc)) from exc
