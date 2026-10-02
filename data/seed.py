"""
Builds data/retail.db — a tiny retail "data platform" that stands in for BigQuery.

Two layers, like a real Modern Data Stack:
  raw_*  tables  -> messy source data. Agents are NOT allowed to read these.
  cm_*   views   -> "certified metrics": definitions the data team has validated.
                    This is the ONLY layer the agents can query.

Run:  python data/seed.py
"""
import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parent / "retail.db"
TODAY = date(2026, 10, 1)          # frozen "today" so every run is reproducible
DAYS_OF_HISTORY = 56

STORES = [
    ("PAR01", "Paris Rivoli", "Paris"),
    ("LYO01", "Lyon Part-Dieu", "Lyon"),
    ("LIL01", "Lille Centre", "Lille"),
]

# product_id, name, category, unit_cost, price, supplier, lead_time_days, base_daily_units
PRODUCTS = [
    ("SKU-101", "Organic oat milk 1L",        "Grocery",  1.10,  2.49, "NordFarm",    4, 22),
    ("SKU-102", "Arabica coffee beans 500g",  "Grocery",  4.20,  8.90, "CafeSud",     7, 9),
    ("SKU-103", "Dark chocolate 70% 100g",    "Grocery",  0.95,  2.20, "CacaoPlus",   5, 14),
    ("SKU-201", "Stainless water bottle",     "Home",     5.50, 14.90, "EcoGoods",   10, 4),
    ("SKU-202", "Bamboo cutting board",       "Home",     6.00, 17.50, "EcoGoods",   10, 2),
    ("SKU-203", "Scented candle - fig",       "Home",     3.10,  9.90, "LumiCraft",  12, 3),
    ("SKU-301", "Wireless earbuds",           "Tech",    18.00, 49.90, "SoundAsia",  21, 3),
    ("SKU-302", "USB-C charger 30W",          "Tech",     7.50, 24.90, "SoundAsia",  21, 5),
    ("SKU-303", "Phone case - clear",         "Tech",     1.80, 12.90, "CaseCo",     14, 6),
    ("SKU-401", "Rain jacket - unisex",       "Apparel", 19.00, 59.90, "TextilNord", 18, 2),
    ("SKU-402", "Merino socks (2-pack)",      "Apparel",  4.00, 15.90, "TextilNord", 18, 5),
    ("SKU-403", "Summer linen shirt",         "Apparel", 11.00, 39.90, "TextilNord", 18, 2),
]

# Hand-placed "stories" in the data, so the agents have real problems to find.
# (store, product) -> on-hand stock. Everything else gets a healthy stock.
STOCK_STORIES = {
    ("PAR01", "SKU-101"): 30,    # oat milk sells ~25/day in Paris -> ~1 day of cover, lead time 4 -> STOCKOUT RISK
    ("LYO01", "SKU-302"): 20,    # chargers, 21-day lead time -> STOCKOUT RISK
    ("LIL01", "SKU-403"): 140,   # linen shirts in October, ~0.3/day -> huge OVERSTOCK
    ("PAR01", "SKU-203"): 420,   # candles, ~120 days of cover -> OVERSTOCK
}
# A one-off spike that should NOT be read as a trend (a trap for a careless agent).
PROMO_SPIKE = ("LIL01", "SKU-102", TODAY - timedelta(days=40), 120)  # 120 units in a single day, 40 days ago


def build():
    random.seed(42)
    if DB_PATH.exists():
        DB_PATH.unlink()
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    # ---------- RAW LAYER ----------
    cur.executescript("""
    CREATE TABLE raw_stores    (store_id TEXT PRIMARY KEY, name TEXT, city TEXT);
    CREATE TABLE raw_products  (product_id TEXT PRIMARY KEY, name TEXT, category TEXT,
                                unit_cost REAL, price REAL, supplier TEXT, lead_time_days INTEGER);
    CREATE TABLE raw_sales     (store_id TEXT, product_id TEXT, sale_date TEXT, units INTEGER,
                                unit_price REAL, is_test_transaction INTEGER);
    CREATE TABLE raw_inventory (store_id TEXT, product_id TEXT, snapshot_date TEXT, on_hand INTEGER);
    """)
    cur.executemany("INSERT INTO raw_stores VALUES (?,?,?)", STORES)
    cur.executemany("INSERT INTO raw_products VALUES (?,?,?,?,?,?,?)", [p[:7] for p in PRODUCTS])

    store_factor = {"PAR01": 1.15, "LYO01": 1.0, "LIL01": 0.8}
    rows = []
    for store_id, _, _ in STORES:
        for pid, _, category, _, price, _, _, base in PRODUCTS:
            for d in range(DAYS_OF_HISTORY):
                day = TODAY - timedelta(days=DAYS_OF_HISTORY - d)
                weekend = 1.35 if day.weekday() >= 5 else 1.0
                seasonal = 1.0
                if pid == "SKU-403":                     # summer shirt fades out as autumn arrives
                    seasonal = max(0.25, 1.0 - d / DAYS_OF_HISTORY)
                mean = base * store_factor[store_id] * weekend * seasonal
                units = max(0, round(random.gauss(mean, mean * 0.25)))
                rows.append((store_id, pid, day.isoformat(), units, price, 0))
    s, p, spike_day, spike_units = PROMO_SPIKE
    rows.append((s, p, spike_day.isoformat(), spike_units, 5.90, 0))
    # Dirty data the certified layer must filter out: test transactions from the POS team.
    rows += [("PAR01", "SKU-301", (TODAY - timedelta(days=3)).isoformat(), 500, 0.0, 1)]
    cur.executemany("INSERT INTO raw_sales VALUES (?,?,?,?,?,?)", rows)

    inv = []
    for store_id, _, _ in STORES:
        for pid, *_rest in PRODUCTS:
            base = _rest[-1]
            on_hand = STOCK_STORIES.get((store_id, pid), int(base * store_factor[store_id] * random.uniform(18, 30)))
            inv.append((store_id, pid, TODAY.isoformat(), on_hand))
    cur.executemany("INSERT INTO raw_inventory VALUES (?,?,?,?)", inv)

    # ---------- CERTIFIED LAYER (what the agents see) ----------
    cur.executescript(f"""
    -- Product master data
    CREATE VIEW cm_product_catalog AS
    SELECT product_id, name, category, unit_cost, price, supplier, lead_time_days
    FROM raw_products;

    CREATE VIEW cm_stores AS SELECT store_id, name, city FROM raw_stores;

    -- Daily sales, test transactions removed (a classic data-quality rule)
    CREATE VIEW cm_daily_sales AS
    SELECT store_id, product_id, sale_date, SUM(units) AS units, ROUND(SUM(units * unit_price), 2) AS revenue
    FROM raw_sales
    WHERE is_test_transaction = 0
    GROUP BY store_id, product_id, sale_date;

    -- Stock cover: how many days the current stock lasts at the recent sales pace.
    -- Uses the MEDIAN-like robust pace (avg of last 28 days, excluding single-day spikes > 4x normal)
    CREATE VIEW cm_stock_cover AS
    WITH recent AS (
        SELECT store_id, product_id, units
        FROM cm_daily_sales
        WHERE sale_date >= date('{TODAY.isoformat()}', '-28 day')
    ),
    pace AS (
        SELECT store_id, product_id, AVG(units) AS avg_units FROM recent GROUP BY store_id, product_id
    ),
    clean_pace AS (
        SELECT r.store_id, r.product_id, ROUND(AVG(r.units), 2) AS avg_daily_units_28d
        FROM recent r JOIN pace p USING (store_id, product_id)
        WHERE r.units <= 4 * p.avg_units + 1
        GROUP BY r.store_id, r.product_id
    )
    SELECT i.store_id, i.product_id, c.name AS product_name, c.category,
           i.on_hand, cp.avg_daily_units_28d, c.lead_time_days,
           CASE WHEN cp.avg_daily_units_28d > 0
                THEN ROUND(i.on_hand / cp.avg_daily_units_28d, 1) END AS days_of_cover,
           c.unit_cost, c.price
    FROM raw_inventory i
    JOIN cm_product_catalog c USING (product_id)
    LEFT JOIN clean_pace cp USING (store_id, product_id);
    """)

    # Metric dictionary: tells agents what each certified view means (like a data catalog).
    cur.executescript("""
    CREATE TABLE cm_metric_dictionary (view_name TEXT, description TEXT);
    INSERT INTO cm_metric_dictionary VALUES
     ('cm_stores', 'One row per store: store_id, name, city.'),
     ('cm_product_catalog', 'One row per product: id, name, category, unit_cost, price (EUR), supplier, lead_time_days (supplier delivery delay).'),
     ('cm_daily_sales', 'Units and revenue per store, product and day. Test transactions already removed.'),
     ('cm_stock_cover', 'Current stock per store/product with avg_daily_units_28d (spikes removed) and days_of_cover = on_hand / avg_daily_units_28d. Stockout risk when days_of_cover < lead_time_days. Overstock when days_of_cover > 90.');
    """)
    con.commit()
    con.close()
    print(f"Built {DB_PATH} ({len(rows)} sales rows)")


if __name__ == "__main__":
    build()
