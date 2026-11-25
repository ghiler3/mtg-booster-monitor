import asyncio
import hmac
import json
import os
import re
import sqlite3
from contextlib import asynccontextmanager, suppress
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

import model
import storage
import sync
from sources import source_url

ROOT = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(_app):
    storage.init()
    if os.environ.get("BOOSTER_AUTO_FX") != "1":
        yield
        return
    task = asyncio.create_task(sync.refresh_fx_periodically())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="Бустеры MTG", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
templates = Jinja2Templates(directory=ROOT / "templates")
templates.env.globals["source_url"] = source_url
templates.env.filters["money"] = lambda value: "—" if value is None else f"€{Decimal(value):.2f}"
templates.env.filters["percent"] = lambda value: "—" if value is None else f"{Decimal(value):.1f}%"

SORTS = {
    "name": "Бустер", "market_price": "Рынок", "shop_eur": "Магазин",
    "ev": "EV", "median": "Медиана", "market_return": "Возврат рынка",
    "shop_return": "Возврат магазина", "hit5": "Карта €5+", "hit20": "Карта €20+",
    "hit50": "Карта €50+", "breakeven": "Шанс окупить",
}


def token(db):
    return db.execute("SELECT value FROM settings WHERE name='csrf'").fetchone()[0]


def verify(db, csrf):
    if not csrf.isascii() or not hmac.compare_digest(csrf, token(db)):
        raise HTTPException(403, "Обнови страницу и повтори действие")


def product_by_path(db, path):
    product = db.execute("SELECT * FROM products WHERE path=?", (path,)).fetchone()
    if not product:
        raise HTTPException(404, "Бустер не найден")
    return product


def redirect(view="pinned", folder=0, q="", sort="name", direction="asc"):
    if view not in ("catalog", "pinned", "folder"):
        view = "pinned"
    if sort not in SORTS or direction not in ("asc", "desc"):
        sort, direction = "name", "asc"
    return RedirectResponse("/?" + urlencode(dict(view=view, folder=folder, q=q, sort=sort,
                                                direction=direction)), status_code=303)


def currency_choices(rates, product):
    return sorted(set(rates) | {"EUR", "USD", "GBP", "PLN", "CHF", "RUB"}
                  | {product["shop_currency"] or "EUR"})


def booster_redirect(path):
    return RedirectResponse("/booster?" + urlencode(dict(path=path)), status_code=303)


def reference_rates(db):
    return {row["currency"]: (Decimal(row["rate"]), row["rate_date"]) for row in db.execute("""
        SELECT fx.* FROM fx JOIN (
            SELECT currency, max(rate_date) AS day FROM fx GROUP BY currency
        ) latest ON latest.currency=fx.currency AND latest.day=fx.rate_date
    """)}


def load_metrics(db, path):
    snapshot = db.execute("SELECT * FROM snapshots WHERE path=? ORDER BY captured_at DESC LIMIT 1",
                          (path,)).fetchone()
    return (json.loads(snapshot["metrics"]), snapshot["captured_at"]) if snapshot else ({}, None)


def load_model(db, path):
    row = db.execute("SELECT * FROM models WHERE path=?", (path,)).fetchone()
    return (json.loads(row["payload"]), row["created_at"]) if row else (None, None)


def number(value):
    return Decimal(value) if value not in (None, "") else None


def prepare_row(product, metrics, stamped, rates, payload=None, modeled_at=None):
    row = dict(product)
    row.update(metrics)
    row["captured_at"] = stamped
    row["ev"] = number(metrics.get("ev") if stamped else product["catalog_ev"])
    row["market_return"] = metrics.get("market_return") if stamped else product["catalog_return"]
    row["rate"], row["rate_date"] = rates.get(row["shop_currency"], (None, None))
    if row["shop_currency"] == "EUR":
        row["rate"], row["rate_date"], row["rate_source"] = Decimal(1), None, "EUR"
    elif row["shop_currency"] == "RUB" and row["rate"] is not None:
        row["rate_source"] = "Банк России"
    elif row["manual_fx"]:
        row["rate"], row["rate_date"], row["rate_source"] = number(row["manual_fx"]), None, "ручной курс"
    else:
        row["rate_source"] = "ЕЦБ"
    row["shop_eur"] = number(row["shop_amount"]) / row["rate"] if row["shop_amount"] and row["rate"] else None
    row["shop_return"] = 100 * row["ev"] / row["shop_eur"] if row["ev"] is not None and row["shop_eur"] else None
    row["market_price"] = number(metrics.get("market_price"))
    row["breakeven_market"] = model.chance(payload, row["market_price"])
    row["breakeven_shop"] = model.chance(payload, row["shop_eur"])
    row["breakeven"] = row["breakeven_shop"] if row["shop_amount"] else row["breakeven_market"]
    row["modeled_at"] = modeled_at
    limit = date.today() - timedelta(days=2)
    row["stale"] = bool(row["last_error"] or any(
        date.fromisoformat(timestamp[:10]) < limit
        for timestamp in (stamped, metrics.get("source_date")) if timestamp
    ))
    for key in ("5", "20", "50"):
        row["hit" + key] = metrics.get("hit", {}).get(key)
    return row


def sort_rows(rows, field, direction):
    if field == "name":
        return sorted(rows, key=lambda row: (row["name"].casefold(), row["kind"]), reverse=direction == "desc")
    known, missing = [], []
    for row in rows:
        value = row.get(field)
        if isinstance(value, str):
            match = re.search(r"\d+(?:[.,]\d+)?", value)
            value = Decimal(match[0].replace(",", ".")) if match else None
        if value is None:
            missing.append(row)
        else:
            known.append((value, row))
    known.sort(key=lambda item: item[0], reverse=direction == "desc")
    return [row for _, row in known] + missing


@app.get("/", response_class=HTMLResponse)
def index(request: Request, view="pinned", folder: int = 0, q="", sort="name", direction="asc"):
    if view == "saved":
        return redirect("pinned", q=q, sort=sort, direction=direction)
    if view not in ("pinned", "folder", "catalog") or sort not in SORTS or direction not in ("asc", "desc"):
        raise HTTPException(400, "Недопустимые параметры таблицы")
    q = q.strip()[:100]
    with storage.connect() as db:
        folders = db.execute("SELECT * FROM folders ORDER BY name").fetchall()
        current = next((item for item in folders if item["id"] == folder), None)
        if view == "folder" and not current:
            raise HTTPException(404, "Папка не найдена")
        filters = {"catalog": ("1", ()), "pinned": ("pinned=1", ()),
                   "folder": ("path IN (SELECT path FROM memberships WHERE folder_id=?)", (folder,))}
        condition, parameters = filters[view]
        products = db.execute("SELECT * FROM products WHERE " + condition + " ORDER BY name, kind",
                              parameters).fetchall()
        rates = reference_rates(db)
        rows = []
        for product in products:
            searchable = f"{product['code']} {product['name']} {product['kind']}".casefold()
            if q.casefold() not in searchable:
                continue
            metrics, captured = load_metrics(db, product["path"])
            payload, modeled = load_model(db, product["path"]) if product["saved"] else (None, None)
            rows.append(prepare_row(product, metrics, captured, rates, payload, modeled))
        return templates.TemplateResponse(request, "index.html", dict(
            rows=sort_rows(rows, sort, direction), sorts=SORTS, sort=sort, direction=direction,
            folders=folders, current=current, view=view, folder=folder, q=q, csrf=token(db),
        ))


@app.get("/booster", response_class=HTMLResponse)
def detail(request: Request, path: str, card_q: str = "", page: int = 1):
    with storage.connect() as db:
        product = product_by_path(db, path)
        metrics, captured = load_metrics(db, path)
        payload, modeled = load_model(db, path)
        rates = reference_rates(db)
        row = prepare_row(product, metrics, captured, rates, payload, modeled)
        folders = db.execute("SELECT * FROM folders ORDER BY name").fetchall()
        membership = {x[0] for x in db.execute("SELECT folder_id FROM memberships WHERE path=?", (path,))}
        currencies = currency_choices(rates, row)
        card_q = card_q.strip()[:100]
        cards = [card for card in (payload or {}).get("cards", [])
                 if card_q.casefold() in card["name"].casefold()]
        pages = max(1, (len(cards) + 49) // 50)
        page = min(max(page, 1), pages)
        return templates.TemplateResponse(request, "detail.html", dict(
            row=row, csrf=token(db), folders=folders, membership=membership, currencies=currencies,
            model=payload, cards=cards[(page - 1) * 50:page * 50], card_q=card_q, page=page, pages=pages,
        ))


@app.post("/collection")
def collection(background: BackgroundTasks, path: str = Form(), action: str = Form(),
               csrf: str = Form(), view: str = Form("pinned"), folder: int = Form(0),
               q: str = Form(""), sort: str = Form("name"), direction: str = Form("asc")):
    with storage.connect() as db:
        verify(db, csrf)
        product = product_by_path(db, path)
        if action == "pin":
            db.execute("UPDATE products SET saved=1, pinned=1 WHERE path=?", (path,))
            if not product["pinned"]:
                background.add_task(sync.update_product, dict(product))
        elif action == "unpin":
            db.execute("UPDATE products SET pinned=0 WHERE path=?", (path,))
        else:
            raise HTTPException(400, "Неизвестное действие")
    if view == "detail":
        return booster_redirect(path)
    return redirect(view, folder, q, sort, direction)


@app.post("/folders")
def manage_folder(csrf: str = Form(), name: str = Form(""), folder: int = Form(0),
                  action: str = Form("create")):
    name = name.strip()
    with storage.connect() as db:
        verify(db, csrf)
        if action == "delete":
            db.execute("DELETE FROM folders WHERE id=?", (folder,))
            return redirect()
        if action not in ("create", "rename") or not 1 <= len(name) <= 70:
            raise HTTPException(400, "Введи название папки длиной от 1 до 70 символов")
        try:
            if action == "rename":
                db.execute("UPDATE folders SET name=? WHERE id=?", (name, folder))
            else:
                folder = db.execute("INSERT INTO folders(name) VALUES (?)", (name,)).lastrowid
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "Папка с таким названием уже есть") from exc
    return redirect("folder", folder)


@app.get("/add-to-folder", response_class=HTMLResponse)
def add_to_folder_page(request: Request, path: str, folder: int = 0, back: str = "pinned"):
    with storage.connect() as db:
        product = product_by_path(db, path)
        folders = db.execute("SELECT * FROM folders ORDER BY name").fetchall()
        return templates.TemplateResponse(request, "add_to_folder.html", dict(
            row=product, csrf=token(db), folders=folders, folder=folder,
            currencies=currency_choices(reference_rates(db), product),
            back_to_detail=back == "detail",
        ))


@app.post("/membership")
def edit_membership(background: BackgroundTasks, csrf: str = Form(), path: str = Form(),
                    folder: int = Form(0), action: str = Form(), new_folder: str = Form(""),
                    amount: str = Form(""), currency: str = Form("EUR"), manual_fx: str = Form("")):
    with storage.connect() as db:
        verify(db, csrf)
        product = product_by_path(db, path)
        if action not in ("add", "skip_price", "save_price", "remove"):
            raise HTTPException(400, "Неизвестное действие")
        if folder:
            if not db.execute("SELECT 1 FROM folders WHERE id=?", (folder,)).fetchone():
                raise HTTPException(404, "Папка не найдена")
        elif action in ("skip_price", "save_price") and 1 <= len(new_folder.strip()) <= 70:
            db.execute("INSERT OR IGNORE INTO folders(name) VALUES (?)", (new_folder.strip(),))
            folder = db.execute("SELECT id FROM folders WHERE name=?", (new_folder.strip(),)).fetchone()[0]
        else:
            raise HTTPException(400, "Выбери папку или введи её название")
        if action == "remove":
            db.execute("DELETE FROM memberships WHERE folder_id=? AND path=?", (folder, path))
        else:
            if action == "save_price":
                save_shop_price(db, path, amount, currency, manual_fx)
            db.execute("UPDATE products SET saved=1 WHERE path=?", (path,))
            db.execute("INSERT OR IGNORE INTO memberships VALUES (?, ?)", (folder, path))
            if not product["saved"]:
                background.add_task(sync.update_product, dict(product))
    if action in ("skip_price", "save_price"):
        return redirect("folder", folder)
    return booster_redirect(path)


def positive_decimal(text):
    text = text.strip().replace(",", ".")
    if not re.fullmatch(r"\d{1,12}(?:\.\d{1,10})?", text):
        raise HTTPException(400, "Введи положительное число с точкой или запятой")
    value = Decimal(text)
    if value <= 0:
        raise HTTPException(400, "Сумма и курс должны быть больше нуля")
    return str(value)


def save_shop_price(db, path, amount, currency, manual_fx):
    currency = currency.strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise HTTPException(400, "Введи трёхбуквенный код валюты")
    value = positive_decimal(amount)
    rate = positive_decimal(manual_fx) if manual_fx.strip() and currency != "EUR" else None
    db.execute("UPDATE products SET shop_amount=?, shop_currency=?, manual_fx=? WHERE path=?",
               (value, currency, rate, path))


@app.post("/price")
def edit_price(csrf: str = Form(), path: str = Form(), amount: str = Form(""),
               currency: str = Form("EUR"), manual_fx: str = Form(""), action: str = Form("save")):
    with storage.connect() as db:
        verify(db, csrf)
        product_by_path(db, path)
        if action == "clear":
            db.execute("UPDATE products SET shop_amount=NULL, shop_currency=NULL, manual_fx=NULL WHERE path=?", (path,))
        elif action == "save":
            save_shop_price(db, path, amount, currency, manual_fx)
        else:
            raise HTTPException(400, "Неизвестное действие")
    return booster_redirect(path)


@app.exception_handler(HTTPException)
def error(request: Request, exc: HTTPException):
    return templates.TemplateResponse(request, "error.html", {"message": exc.detail}, status_code=exc.status_code)
