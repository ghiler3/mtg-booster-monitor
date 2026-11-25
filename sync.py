import argparse
import asyncio
import logging
import time

import httpx

import sources
import storage

log = logging.getLogger("uvicorn.error.boosters")


def update_product(product):
    try:
        result = sources.parse_pack(sources.fetch(sources.source_url(product["path"])), product)
        storage.save_snapshot(product["path"], result)
        log.info("%s · %s · EU · EV %s · цены %s",
                 product["code"], product["kind"], result["ev"], result["source_date"])
        return True
    except (httpx.HTTPError, ValueError) as exc:
        msg = str(exc)[:350]
        log.warning("%s · %s", product["path"], msg)
        with storage.connect() as db:
            db.execute("UPDATE products SET last_error=? WHERE path=?", (msg, product["path"]))
        return False


def update_fx():
    providers = (
        ("ЕЦБ", sources.FX_URL, sources.parse_fx),
        ("Банка России", sources.RUB_FX_URL, sources.parse_rub_fx),
    )
    success = True
    for provider, url, parse in providers:
        try:
            rates = parse(sources.fetch(url))
            with storage.connect() as db:
                db.executemany("INSERT OR REPLACE INTO fx VALUES (?, ?, ?)", rates)
            log.info("Курсы %s · %s · %s валют", provider, rates[0][1], len(rates))
        except (httpx.HTTPError, ValueError, KeyError, SyntaxError, ArithmeticError) as exc:
            log.error("Курсы %s · %s", provider, exc)
            success = False
    return success


async def refresh_fx_periodically():
    while True:
        success = await asyncio.to_thread(update_fx)
        await asyncio.sleep(6 * 60 * 60 if success else 15 * 60)


def main():
    parser = argparse.ArgumentParser(description="Обновить данные бустеров в EUR")
    parser.add_argument("--only", choices=("catalog", "rates", "saved"))
    parser.add_argument("--product", help="Путь из каталога, например /sets/outlaws-of-thunder-junction/play")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    storage.init()
    if args.product:
        with storage.connect() as db:
            product = db.execute("SELECT * FROM products WHERE path=?", (args.product,)).fetchone()
        if not product:
            parser.error("Продукт отсутствует в каталоге. Обновите каталог.")
        return 0 if update_product(product) else 1
    success = True
    if args.only in (None, "rates"):
        success = update_fx()
    if args.only in (None, "catalog"):
        try:
            products = sources.parse_catalog(sources.fetch(sources.source_url("/sets")))
            storage.save_catalog(products)
            log.info("Каталог EU · %s продуктов", len(products))
        except (httpx.HTTPError, ValueError) as exc:
            log.error("Каталог EU · %s", exc)
            success = False
    if args.only in (None, "saved"):
        with storage.connect() as db:
            products = db.execute("""
                SELECT * FROM products WHERE pinned=1 OR EXISTS (
                    SELECT 1 FROM memberships WHERE memberships.path=products.path
                ) ORDER BY path
            """).fetchall()
        for index, product in enumerate(products):
            if index:
                time.sleep(1)
            if not update_product(product):
                success = False
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
