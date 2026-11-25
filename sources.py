import re
from datetime import datetime
from decimal import Decimal
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree

import httpx
from bs4 import BeautifulSoup

ORIGIN = "https://tcgbalance.com"
FX_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
RUB_FX_URL = "https://www.cbr.ru/scripts/XML_daily.asp"
PRODUCT_PATH = re.compile(r"/sets/[a-z0-9][a-z0-9-]*/[a-z0-9][a-z0-9-]*")
MONEY = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
PERCENT = r"(?:[<>]\s*)?\d+(?:[.,]\d+)?\s*%"


class SourceError(ValueError):
    pass


def source_url(path):
    return ORIGIN + path + "?market=eu"


def fetch(url):
    with httpx.Client(timeout=25, follow_redirects=False, headers={
        "User-Agent": "PersonalBoosterMonitor/1.0",
        "Accept": "text/html,application/xml;q=0.9",
    }) as client, client.stream("GET", url) as result:
        result.raise_for_status()
        chunks = bytearray()
        for chunk in result.iter_bytes():
            chunks.extend(chunk)
            if len(chunks) > 12_000_000:
                raise SourceError("Ответ источника превышает 12 МБ")
        return bytes(chunks)


def euro(text):
    if "$" in text:
        raise SourceError("Источник вернул USD вместо EUR")
    match = re.search(r"€\s*(" + MONEY + r")(?![\d.,])", text)
    if match:
        return match[1].replace(",", "")
    if text.strip() in ("", "–", "—", "-"):
        return None
    raise SourceError(f"Не распознана цена EUR: {text[:80]}")


def parse_catalog(html):
    page = BeautifulSoup(html, "html.parser")
    products = {}
    has_price = False
    for table in page.select("main table, table"):
        columns = [cell.get_text(" ", strip=True) for cell in table.select("thead th")]
        if not any("booster" in col.casefold() for col in columns):
            continue
        for row in table.select("tbody tr"):
            cells = row.find_all(["td", "th"], recursive=False)
            if not cells or not (link := cells[0].find("a")):
                continue
            set_code = re.fullmatch(r"(.+?)\s+([A-Z0-9]{2,8})", link.get_text(" ", strip=True))
            if not set_code:
                continue
            name, code = set_code.groups()
            for kind, cell in zip(columns[2:], cells[2:]):
                product_link = cell.find("a", href=True)
                if not product_link:
                    continue
                target = urlsplit(urljoin(ORIGIN, product_link["href"]))
                if target.netloc != urlsplit(ORIGIN).netloc or not PRODUCT_PATH.fullmatch(target.path):
                    continue
                ev = euro(product_link.get_text(" ", strip=True))
                has_price |= ev is not None
                rate = re.search(PERCENT, cell.get_text(" ", strip=True))
                products[target.path] = dict(path=target.path, code=code, name=name,
                                             kind=kind.title(), ev=ev, market_return=rate[0] if rate else None)
    if not products or not has_price:
        raise SourceError("Не удалось подтвердить каталог бустеров в EUR")
    return list(products.values())


def parse_pack(html, product):
    page = BeautifulSoup(html, "html.parser")
    main = page.find("main")
    if not main:
        raise SourceError("Содержимое страницы бустера отсутствует")
    heading = main.find("h1")
    if not heading or heading.get_text(" ", strip=True).casefold() != product["name"].casefold():
        raise SourceError("Выпуск страницы отличается от ожидаемого")
    active_tabs = main.select('a.tab[aria-current="page"][href]')
    if not any(urlsplit(link["href"]).path == product["path"] for link in active_tabs):
        raise SourceError("Активная вкладка страницы отличается от ожидаемого типа бустера")
    content = main.get_text(" ", strip=True)
    date = re.search(r"Cardmarket prices from\s+(\w{3,9} \d{1,2}, \d{4})", content)
    if not date and not re.search(r"Cardmarket market price", content, flags=re.I):
        raise SourceError("Не удалось подтвердить рынок Cardmarket")
    if date:
        month, rest = date[1].split(" ", 1)
        price_date = datetime.strptime(f"{month[:3]} {rest}", "%b %d, %Y").date().isoformat()
    else:
        price_date = None
    tiles = {}
    for tile in main.select(".stats .tile"):
        label, value, note = (tile.select_one(selector) for selector in (".label", ".value", ".note"))
        if label and value:
            tiles[label.get_text(" ", strip=True)] = (
                value.get_text(" ", strip=True), note.get_text(" ", strip=True) if note else ""
            )
    if "Expected value per pack" not in tiles or "Typical pack (median)" not in tiles:
        raise SourceError("Формат блока основных показателей изменился")
    hit = {}
    for label, (value, note) in tiles.items():
        if match := re.fullmatch(r"Chance of a €\s*(" + MONEY + r")\+ card", label):
            if re.fullmatch(PERCENT, value):
                hit[match[1]] = value
            for amount, percent in re.findall(r"€\s*(" + MONEY + r")\+\s*:\s*(" + PERCENT + ")", note):
                hit[amount] = percent
    coverage = re.search(r"Prices found for\s*(" + PERCENT + r")", content)
    sizes = re.search(r"box holds (\d+) packs of (\d+) cards", content)
    returns = re.search(r"(" + PERCENT + r")\s+of the pack price comes back", content)
    different, versions = tiles.get("Different cards", (None, ""))
    version_count = re.match(r"[\d,]+", versions)
    return dict(ev=euro(tiles["Expected value per pack"][0]),
                median=euro(tiles["Typical pack (median)"][0]),
                market_price=euro(tiles.get("Price of a pack", ("", ""))[0]),
                hit=hit, coverage=coverage[1] if coverage else None,
                top10=tiles.get("Top 10 share of value", (None, ""))[0],
                different=different, versions=version_count[0] if version_count else None,
                packs_per_box=int(sizes[1]) if sizes else None, cards_per_pack=int(sizes[2]) if sizes else None,
                market_return=returns[1] if returns else None,
                source_date=price_date)


def parse_fx(xml):
    root = ElementTree.fromstring(xml)
    result = []
    for block in root.iter():
        if day := block.get("time"):
            datetime.strptime(day, "%Y-%m-%d")
            for item in block:
                code, rate = item.attrib["currency"], Decimal(item.attrib["rate"])
                if re.fullmatch(r"[A-Z]{3}", code) and rate.is_finite() and rate > 0:
                    result.append((code, day, str(rate)))
    if not result:
        raise SourceError("ЕЦБ не вернул валютных курсов")
    return result


def parse_rub_fx(xml):
    root = ElementTree.fromstring(xml)
    if root.tag != "ValCurs":
        raise SourceError("Не распознан документ Банка России")
    rate_date = datetime.strptime(root.attrib["Date"], "%d.%m.%Y").date().isoformat()
    for currency in root.findall("Valute"):
        if currency.findtext("CharCode") == "EUR":
            value = Decimal(currency.findtext("Value", "").replace(",", "."))
            nominal = Decimal(currency.findtext("Nominal", ""))
            if value.is_finite() and nominal.is_finite() and value > 0 and nominal > 0:
                return [("RUB", rate_date, str(value / nominal))]
    raise SourceError("Банк России не вернул курс EUR к RUB")
