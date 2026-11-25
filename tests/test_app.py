import re
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app
import model
import sources
import storage

PATH = "/sets/example/play"
CATALOG = """
<main><table><thead><tr><th>Set</th><th>Released</th><th>Play Booster</th></tr></thead>
<tbody><tr><td><a href="/sets/example">Example <span>XYZ</span></a></td><td>Nov 22</td>
<td><a href="/sets/example/play?market=eu">€6.00</a> 120% of price</td></tr></tbody>
</table></main>
"""
PACK = """
<main><h1>Example</h1><p>Cardmarket prices from Nov 22, 2026</p>
<a class="tab" aria-current="page" href="/sets/example/play">Play Booster</a>
<p>A Play Booster box holds 36 packs of 14 cards.</p><p>120% of the pack price comes back</p>
<div class="stats">
  <div class="tile"><span class="label">Expected value per pack</span><span class="value">€6.00</span></div>
  <div class="tile"><span class="label">Typical pack (median)</span><span class="value">€4.00</span></div>
  <div class="tile"><span class="label">Price of a pack</span><span class="value">€5.00</span></div>
  <div class="tile"><span class="label">Chance of a €5+ card</span><span class="value">20%</span>
  <span class="note">€20+: &lt;0.1% · €50+: 0% per pack</span></div>
</div><p>Prices found for &gt;99% of the cards you can expect.</p></main>
"""


class SourceTests(unittest.TestCase):
    def test_pack_and_catalog(self):
        product = sources.parse_catalog(CATALOG)[0]
        self.assertEqual(product["code"], "XYZ")
        data = sources.parse_pack(PACK, product)
        self.assertEqual(data["hit"], {"5": "20%", "20": "<0.1%", "50": "0%"})
        self.assertEqual(data["coverage"], ">99%")
        self.assertEqual(data["source_date"], "2026-11-22")
        self.assertEqual(data["market_return"], "120%")
        with self.assertRaises(sources.SourceError):
            sources.parse_pack(PACK.replace("€", "$"), product)
        with self.assertRaises(sources.SourceError):
            sources.parse_pack(PACK.replace('aria-current="page"', 'aria-current="false"'), product)

    def test_ecb_namespace(self):
        xml = '<root xmlns="urn:ecb"><Cube><Cube time="2026-11-20"><Cube currency="USD" rate="1.20"/></Cube></Cube></root>'
        self.assertEqual(sources.parse_fx(xml), [("USD", "2026-11-20", "1.20")])

    def test_rub_rate_by_nominal_and_date(self):
        xml = ('<ValCurs Date="21.11.2026"><Valute><CharCode>EUR</CharCode>'
               '<Nominal>10</Nominal><Value>905,00</Value></Valute></ValCurs>')
        self.assertEqual(sources.parse_rub_fx(xml), [("RUB", "2026-11-21", "90.50")])

    def test_model_boundary_and_missing_price(self):
        config = dict(boosters=[dict(weight=1, contents={"one": 2})],
                      sheets={"one": dict(foil=False, cards={"a": 1})})
        payload = model.simulate(config, {"a": ("A", 250, None, "1", "XYZ")}, size=100)
        self.assertEqual(payload["cards"][0]["probability"], 100)
        self.assertEqual(model.chance(payload, Decimal("5")), 100)
        self.assertEqual(model.chance(payload, Decimal("5.0001")), 0)
        unavailable = model.simulate(config, {"a": ("A", None, None, "1", "XYZ")}, size=10)
        self.assertEqual(unavailable["priced"], 0)
        self.assertEqual(unavailable["totals"], [0])


class AppTests(unittest.TestCase):
    def test_shopping_flow(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(storage, "DATABASE", Path(tmp) / "test.db"):
            with TestClient(app.app, base_url="http://127.0.0.1") as client:
                product = sources.parse_catalog(CATALOG)[0]
                storage.save_catalog([product])
                with storage.connect() as db:
                    db.execute("INSERT INTO fx VALUES ('USD', '2026-11-20', '1.20')")
                response = client.get("/?view=catalog")
                self.assertIn("Example", response.text)
                self.assertIn("€6.00", response.text)
                self.assertIn("Не закреплён", response.text)
                self.assertNotIn("Мои бустеры", response.text)
                csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', response.text)[1]
                self.assertEqual(client.post("/folders", data={"name": "Магазин", "csrf": "invalid"}).status_code, 403)
                client.post("/folders", data={"name": "Магазин", "csrf": csrf})
                with patch.object(app.sync, "update_product", side_effect=lambda _: storage.save_snapshot(PATH, sources.parse_pack(PACK, product))):
                    client.post("/collection", data={"path": PATH, "action": "pin", "csrf": csrf})
                wizard = client.get("/add-to-folder", params={"path": PATH})
                self.assertIn("Магазин", wizard.text)
                self.assertIn("Пропустить цену и добавить", wizard.text)
                added = client.post("/membership", data={"path": PATH, "folder": 1, "action": "save_price",
                                                        "amount": "6,00", "currency": "USD", "csrf": csrf})
                self.assertIn("120.0%", added.text)
                price = client.post("/price", data={"path": PATH, "amount": "6,00", "currency": "USD", "csrf": csrf})
                self.assertEqual(price.status_code, 200)
                self.assertIn("120.0%", price.text)
                self.assertIn("2026-11-20", price.text)
                pinned = client.get("/?view=pinned")
                self.assertIn("Example", pinned.text)
                self.assertIn("Закреплён", pinned.text)
                self.assertIn("Добавить в папку", pinned.text)
                self.assertIn("Example", client.get("/?view=folder&folder=1&sort=shop_eur").text)
                rub = client.post("/price", data={"path": PATH, "amount": "550", "currency": "RUB",
                                                "manual_fx": "100", "csrf": csrf})
                self.assertIn("€5.50", rub.text)
                self.assertIn("ручной курс", rub.text)
                null = client.post("/price", data={"path": PATH, "amount": "550", "currency": "RUB", "csrf": csrf})
                self.assertIn("Курс RUB ещё не получен", null.text)
                with storage.connect() as db:
                    db.execute("INSERT INTO fx VALUES ('RUB', '2026-11-21', '110')")
                skipped = client.post("/membership", data={"path": PATH, "folder": 1, "action": "skip_price",
                                                           "amount": "999", "currency": "EUR", "csrf": csrf})
                self.assertIn("€5.00", skipped.text)
                self.assertIn("550 RUB", skipped.text)
                rub = client.post("/price", data={"path": PATH, "amount": "550", "currency": "RUB",
                                                "manual_fx": "100", "csrf": csrf})
                self.assertIn("Банк России", rub.text)
                self.assertIn("€5.00", rub.text)
                client.post("/collection", data={"path": PATH, "action": "unpin", "csrf": csrf})
                self.assertNotIn("Example", client.get("/").text)
                self.assertIn("Example", client.get("/?view=folder&folder=1").text)
                self.assertEqual(client.get("/", headers={"Host": "public.example"}).status_code, 400)


    def test_existing_saved_product_is_moved_once(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(storage, "DATABASE", Path(tmp) / "test.db"):
            storage.init()
            storage.save_catalog(sources.parse_catalog(CATALOG))
            with storage.connect() as db:
                db.execute("UPDATE products SET saved=1 WHERE path=?", (PATH,))
                db.execute("DELETE FROM settings WHERE name='pinned_navigation'")
            storage.init()
            with storage.connect() as db:
                self.assertEqual(db.execute("SELECT pinned FROM products").fetchone()[0], 1)
                db.execute("UPDATE products SET pinned=0 WHERE path=?", (PATH,))
            storage.init()
            with storage.connect() as db:
                self.assertEqual(db.execute("SELECT pinned FROM products").fetchone()[0], 0)
