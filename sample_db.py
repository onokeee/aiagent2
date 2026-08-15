"""デモ用DBの生成スクリプト（5DB × 各5テーブル）。

  python sample_db.py

業務システムが分かれている状況を模した hub-and-spoke 構成:

    demo_master (マスタ) ◀── demo_sales / demo_inventory / demo_hr / demo_support

| DB | テーブル |
|---|---|
| demo_master    | departments, employees, suppliers, customers, products |
| demo_sales     | orders, order_items, quotes, invoices, payments |
| demo_inventory | warehouses, stocks, stock_movements, purchase_orders, purchase_items |
| demo_hr        | attendances, leaves, evaluations, salaries, certifications |
| demo_support   | tickets, ticket_messages, satisfactions, escalations, faq_articles |

列名の方針: **参照名と被参照名を一致させる**。
- 主キーは必ず `<エンティティ単数形>_id`（`employees.employee_id`, `attendances.attendance_id`）
- 外部キーは参照先の主キーと同じ列名にする（`orders.employee_id` → `employees.employee_id`）
これにより JOIN 条件が `USING (employee_id)` で書けるようになり、
LLMが結合キーを取り違える余地が減る。業務キー（`emp_code` 等）は代理キーと別に残してある。

DB内の参照は FOREIGN KEY として宣言してある（PRAGMA foreign_key_list で自動検出される）。
**DBをまたぐ参照は SQLite では宣言できない**ため、メタ情報(.meta.yaml)の relationships に
`demo_master.customers.customer_id` の3要素形式で記述する。これがカタログ層の存在意義そのもの。

.meta.yaml は既存なら上書きしない（人間の編集を守るため）。作り直したい場合は削除してから実行。
"""
from __future__ import annotations

import random
import sqlite3
from datetime import date, datetime, timedelta
from datetime import time as dtime

import catalog
import config

MASTER_DB = config.DATA_DIR / "demo_master.db"
SALES_DB = config.DATA_DIR / "demo_sales.db"
INVENTORY_DB = config.DATA_DIR / "demo_inventory.db"
HR_DB = config.DATA_DIR / "demo_hr.db"
SUPPORT_DB = config.DATA_DIR / "demo_support.db"

TODAY = date.today()
DAYS = 180  # 生成する期間（日）

# --- マスタの元ネタ -----------------------------------------------------------

DEPARTMENTS = [
    (1, "営業部", "東京", "SALES"),
    (2, "サポート部", "東京", "SUPPORT"),
    (3, "購買部", "大阪", "PURCHASE"),
    (4, "物流部", "大阪", "LOGI"),
    (5, "開発部", "福岡", "DEV"),
    (6, "管理部", "東京", "ADMIN"),
]

SURNAMES = ["佐藤", "鈴木", "高橋", "田中", "伊藤", "渡辺", "山本", "中村", "小林", "加藤",
            "吉田", "山田", "佐々木", "山口", "松本", "井上", "木村", "林", "斎藤", "清水"]
GIVENS = ["翔太", "美咲", "健一", "彩", "大輔", "遥", "涼子", "拓也", "亜紀", "直樹"]

N_CUSTOMERS = 60
N_EMPLOYEES = 40


def _employee_names(n: int) -> list[str]:
    """一意な社員名を n 件作る。

    姓×名の全組合せ(200通り)から重複なく選ぶ。名前が重複すると
    「担当者ごとの集計」で別人が合算されてしまうため、一意性を保証する。
    """
    combos = [f"{s} {g}" for g in GIVENS for s in SURNAMES]
    random.Random(99).shuffle(combos)
    names = combos[:n]
    assert len(set(names)) == n, "社員名が重複している"
    return names


EMPLOYEE_NAMES = _employee_names(N_EMPLOYEES)

REGIONS = ["関東", "関西", "中部", "九州", "東北"]
INDUSTRIES = ["製造", "小売", "建設", "情報通信", "運輸", "医療"]

CUSTOMER_BASE = [
    "青葉商事", "港北電機", "みなみ産業", "北斗物産", "若葉工業", "大和商会", "曙金属",
    "ひかり食品", "桜井製作所", "浜風貿易", "山彦運輸", "琥珀堂", "常盤精機", "白樺紙業",
    "朝霧化学", "潮見鋼材", "菖蒲園芸", "雷鳥電子", "深緑木材", "小金井硝子",
]
CUSTOMER_SUFFIX = ["", "東日本", "西日本", "ホールディングス"]

SUPPLIERS = [
    "第一マテリアル", "中央パーツ", "東海樹脂", "北陸金属", "南海包装", "山陽電材",
    "京浜化成", "信州製紙", "近江工機", "瀬戸内塗料", "越後鋼業", "筑紫繊維",
]

PRODUCTS = [
    ("ボールペン(黒)", "文具", 120), ("ボールペン(赤)", "文具", 120), ("ノートA4", "文具", 250),
    ("ノートB5", "文具", 210), ("クリアファイル", "文具", 80), ("付箋セット", "文具", 320),
    ("修正テープ", "文具", 240), ("蛍光ペン5色", "文具", 480),
    ("コピー用紙A4 500枚", "用紙", 480), ("コピー用紙A3 500枚", "用紙", 880),
    ("感熱ロール紙", "用紙", 350), ("上質紙A3", "用紙", 780), ("再生紙A4", "用紙", 420),
    ("名刺用紙100枚", "用紙", 560),
    ("USBメモリ32GB", "電子機器", 1280), ("USBメモリ64GB", "電子機器", 1980),
    ("ワイヤレスマウス", "電子機器", 1980), ("有線キーボード", "電子機器", 2480),
    ("USB-Cケーブル", "電子機器", 890), ("HDMIケーブル", "電子機器", 1180),
    ("モニタースタンド", "電子機器", 3480), ("Webカメラ", "電子機器", 4280),
    ("USBハブ4ポート", "電子機器", 1680), ("ノートPCスタンド", "電子機器", 2980),
    ("段ボール(小)", "梱包材", 110), ("段ボール(中)", "梱包材", 150),
    ("段ボール(大)", "梱包材", 220), ("緩衝材ロール", "梱包材", 980),
    ("梱包用テープ", "梱包材", 280), ("エアクッション", "梱包材", 1480),
    ("トナーカートリッジ", "消耗品", 8800), ("インクカートリッジ", "消耗品", 3200),
    ("乾電池単3(20本)", "消耗品", 980), ("除菌シート", "消耗品", 420),
    ("ゴミ袋90L(50枚)", "消耗品", 1180), ("手袋(100枚)", "消耗品", 780),
    ("会議用テーブル", "什器", 24800), ("オフィスチェア", "什器", 18800),
    ("書庫(3段)", "什器", 32800), ("パーティション", "什器", 14800),
]

WAREHOUSES = [
    (1, "東京第一倉庫", "関東", 12000),
    (2, "神奈川物流センター", "関東", 20000),
    (3, "大阪南倉庫", "関西", 15000),
    (4, "名古屋倉庫", "中部", 8000),
    (5, "福岡倉庫", "九州", 6000),
]

TICKET_CATEGORIES = ["納期照会", "不良品", "請求", "操作方法", "見積依頼", "その他"]
CERTS = ["基本情報技術者", "簿記2級", "TOEIC800", "危険物取扱者", "フォークリフト",
         "衛生管理者", "販売士2級"]


def _reset(path):
    path.unlink(missing_ok=True)


# --- demo_master --------------------------------------------------------------

def build_master():
    _reset(MASTER_DB)
    conn = sqlite3.connect(str(MASTER_DB))
    conn.executescript("""
        CREATE TABLE departments (
            department_id INTEGER PRIMARY KEY,
            name          TEXT NOT NULL,
            location      TEXT NOT NULL,
            code          TEXT NOT NULL
        );
        CREATE TABLE suppliers (
            supplier_id    INTEGER PRIMARY KEY,
            name           TEXT NOT NULL,
            region         TEXT NOT NULL,
            lead_time_days INTEGER NOT NULL,
            rank           TEXT NOT NULL          -- 'A'/'B'/'C'
        );
        CREATE TABLE employees (
            employee_id   INTEGER PRIMARY KEY,
            emp_code      TEXT NOT NULL UNIQUE,   -- 社員番号(業務キー) 'E0001'
            name          TEXT NOT NULL,
            department_id INTEGER NOT NULL,
            hire_date     TEXT NOT NULL,
            role          TEXT NOT NULL,          -- '1'=一般, '2'=主任, '3'=課長, '4'=部長
            active_flag   INTEGER NOT NULL,       -- 1=在籍, 0=退職
            FOREIGN KEY (department_id) REFERENCES departments(department_id)
        );
        CREATE TABLE customers (
            customer_id INTEGER PRIMARY KEY,
            cust_code   TEXT NOT NULL UNIQUE,     -- 顧客コード(業務キー) 'C0001'
            name        TEXT NOT NULL,
            region      TEXT NOT NULL,
            industry    TEXT NOT NULL,
            rank        TEXT NOT NULL,            -- 'A'/'B'/'C'
            opened_date TEXT NOT NULL
        );
        CREATE TABLE products (
            product_id  INTEGER PRIMARY KEY,
            prod_code   TEXT NOT NULL UNIQUE,     -- 商品コード(業務キー) 'P0001'
            name        TEXT NOT NULL,
            category    TEXT NOT NULL,
            unit_price  INTEGER NOT NULL,
            supplier_id INTEGER NOT NULL,
            disc_flag   INTEGER NOT NULL,         -- 1=廃番, 0=現行
            FOREIGN KEY (supplier_id) REFERENCES suppliers(supplier_id)
        );
    """)
    rng = random.Random(42)

    conn.executemany("INSERT INTO departments VALUES(?,?,?,?)", DEPARTMENTS)

    for i, name in enumerate(SUPPLIERS, start=1):
        conn.execute("INSERT INTO suppliers VALUES(?,?,?,?,?)",
                     (i, name, rng.choice(REGIONS), rng.choice([3, 5, 7, 10, 14]),
                      rng.choices(["A", "B", "C"], weights=[3, 5, 2])[0]))

    for i in range(1, N_EMPLOYEES + 1):
        nm = EMPLOYEE_NAMES[i - 1]
        hire = TODAY - timedelta(days=rng.randint(200, 4000))
        conn.execute("INSERT INTO employees VALUES(?,?,?,?,?,?,?)",
                     (i, f"E{i:04d}", nm, rng.randint(1, len(DEPARTMENTS)), hire.isoformat(),
                      rng.choices(["1", "2", "3", "4"], weights=[60, 22, 13, 5])[0],
                      1 if rng.random() > 0.08 else 0))

    for i in range(1, N_CUSTOMERS + 1):
        base = CUSTOMER_BASE[(i - 1) % len(CUSTOMER_BASE)]
        suf = CUSTOMER_SUFFIX[(i - 1) // len(CUSTOMER_BASE) % len(CUSTOMER_SUFFIX)]
        nm = base + (f" {suf}" if suf else "")
        opened = TODAY - timedelta(days=rng.randint(400, 5000))
        conn.execute("INSERT INTO customers VALUES(?,?,?,?,?,?,?)",
                     (i, f"C{i:04d}", nm, rng.choice(REGIONS), rng.choice(INDUSTRIES),
                      rng.choices(["A", "B", "C"], weights=[2, 5, 3])[0], opened.isoformat()))

    for i, (nm, cat, price) in enumerate(PRODUCTS, start=1):
        conn.execute("INSERT INTO products VALUES(?,?,?,?,?,?,?)",
                     (i, f"P{i:04d}", nm, cat, price, rng.randint(1, len(SUPPLIERS)),
                      1 if rng.random() < 0.1 else 0))

    conn.commit()
    conn.close()
    print(f"OK  {MASTER_DB.name}: departments={len(DEPARTMENTS)}, suppliers={len(SUPPLIERS)}, "
          f"employees={N_EMPLOYEES}, customers={N_CUSTOMERS}, products={len(PRODUCTS)}")


# --- demo_sales ---------------------------------------------------------------

def build_sales():
    _reset(SALES_DB)
    conn = sqlite3.connect(str(SALES_DB))
    conn.executescript("""
        CREATE TABLE orders (
            order_id    INTEGER PRIMARY KEY,
            order_no    TEXT NOT NULL UNIQUE,
            order_date  TEXT NOT NULL,            -- 'YYYY-MM-DD'
            customer_id INTEGER NOT NULL,         -- → demo_master.customers.customer_id
            employee_id INTEGER NOT NULL,         -- → demo_master.employees.employee_id (営業担当)
            status      TEXT NOT NULL,            -- '1'受付 '2'出荷準備 '3'出荷済 '9'キャンセル
            kbn         TEXT NOT NULL             -- '1'通常 '2'サンプル '3'社内
        );
        CREATE TABLE order_items (
            order_item_id INTEGER PRIMARY KEY,
            order_id      INTEGER NOT NULL,
            product_id    INTEGER NOT NULL,       -- → demo_master.products.product_id
            qty           INTEGER NOT NULL,
            unit_price    INTEGER NOT NULL,
            discount_rate REAL NOT NULL,
            FOREIGN KEY (order_id) REFERENCES orders(order_id)
        );
        CREATE TABLE quotes (
            quote_id    INTEGER PRIMARY KEY,
            quote_date  TEXT NOT NULL,
            customer_id INTEGER NOT NULL,         -- → demo_master.customers.customer_id
            employee_id INTEGER NOT NULL,         -- → demo_master.employees.employee_id
            amount      INTEGER NOT NULL,
            result      TEXT NOT NULL             -- 'W'受注 'L'失注 'P'進行中
        );
        CREATE TABLE invoices (
            invoice_id INTEGER PRIMARY KEY,
            order_id   INTEGER NOT NULL,
            issue_date TEXT NOT NULL,
            due_date   TEXT NOT NULL,
            amount     INTEGER NOT NULL,
            paid_flag  INTEGER NOT NULL,          -- 1=入金済, 0=未入金
            FOREIGN KEY (order_id) REFERENCES orders(order_id)
        );
        CREATE TABLE payments (
            payment_id INTEGER PRIMARY KEY,
            invoice_id INTEGER NOT NULL,
            paid_date  TEXT NOT NULL,
            amount     INTEGER NOT NULL,
            method     TEXT NOT NULL,             -- 'BANK'/'CARD'/'CASH'
            FOREIGN KEY (invoice_id) REFERENCES invoices(invoice_id)
        );
        CREATE INDEX idx_orders_date ON orders(order_date);
        CREATE INDEX idx_orders_cust ON orders(customer_id);
        CREATE INDEX idx_items_order ON order_items(order_id);
    """)
    rng = random.Random(7)
    oid = order_item_id = inv_id = pay_id = 0
    shipped_orders = []

    for d in range(DAYS, -1, -1):
        day = TODAY - timedelta(days=d)
        n = rng.randint(5, 12) if day.weekday() < 5 else rng.randint(0, 3)
        for _ in range(n):
            oid += 1
            status = rng.choices(["1", "2", "3", "9"], weights=[10, 12, 71, 7])[0]
            kbn = rng.choices(["1", "2", "3"], weights=[90, 7, 3])[0]
            conn.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?)",
                         (oid, f"SO{day.strftime('%y%m%d')}-{oid:05d}", day.isoformat(),
                          rng.randint(1, N_CUSTOMERS), rng.randint(1, N_EMPLOYEES), status, kbn))
            total = 0
            for _ in range(rng.randint(1, 4)):
                order_item_id += 1
                pid = rng.randint(1, len(PRODUCTS))
                price = PRODUCTS[pid - 1][2]
                qty = rng.randint(1, 30)
                disc = rng.choices([0.0, 0.05, 0.1, 0.15], weights=[70, 15, 10, 5])[0]
                conn.execute("INSERT INTO order_items VALUES(?,?,?,?,?,?)",
                             (order_item_id, oid, pid, qty, price, disc))
                total += int(qty * price * (1 - disc))
            if status == "3" and kbn == "1":
                shipped_orders.append((oid, day, total))

    # 請求と入金（出荷済の通常受注から）
    for order_id, day, total in shipped_orders:
        inv_id += 1
        issue = day + timedelta(days=rng.randint(1, 5))
        due = issue + timedelta(days=30)
        paid = rng.random() < 0.82
        conn.execute("INSERT INTO invoices VALUES(?,?,?,?,?,?)",
                     (inv_id, order_id, issue.isoformat(), due.isoformat(), total, 1 if paid else 0))
        if paid:
            pay_id += 1
            pd = issue + timedelta(days=rng.randint(5, 45))
            conn.execute("INSERT INTO payments VALUES(?,?,?,?,?)",
                         (pay_id, inv_id, min(pd, TODAY).isoformat(), total,
                          rng.choices(["BANK", "CARD", "CASH"], weights=[75, 20, 5])[0]))

    # 見積
    qid = 0
    for d in range(DAYS, -1, -1):
        day = TODAY - timedelta(days=d)
        for _ in range(rng.randint(0, 5)):
            qid += 1
            conn.execute("INSERT INTO quotes VALUES(?,?,?,?,?,?)",
                         (qid, day.isoformat(), rng.randint(1, N_CUSTOMERS),
                          rng.randint(1, N_EMPLOYEES), rng.randint(20, 800) * 1000,
                          rng.choices(["W", "L", "P"], weights=[45, 40, 15])[0]))

    conn.commit()
    conn.close()
    print(f"OK  {SALES_DB.name}: orders={oid}, order_items={order_item_id}, quotes={qid}, "
          f"invoices={inv_id}, payments={pay_id}")


# --- demo_inventory -----------------------------------------------------------

def build_inventory():
    _reset(INVENTORY_DB)
    conn = sqlite3.connect(str(INVENTORY_DB))
    conn.executescript("""
        CREATE TABLE warehouses (
            warehouse_id INTEGER PRIMARY KEY,
            name         TEXT NOT NULL,
            region       TEXT NOT NULL,
            capacity     INTEGER NOT NULL
        );
        CREATE TABLE stocks (
            stock_id     INTEGER PRIMARY KEY,
            warehouse_id INTEGER NOT NULL,
            product_id   INTEGER NOT NULL,        -- → demo_master.products.product_id
            qty          INTEGER NOT NULL,
            safety_qty   INTEGER NOT NULL,        -- 安全在庫。qty < safety_qty が「在庫不足」
            updated_at   TEXT NOT NULL,
            FOREIGN KEY (warehouse_id) REFERENCES warehouses(warehouse_id)
        );
        CREATE TABLE stock_movements (
            stock_movement_id INTEGER PRIMARY KEY,
            moved_at          TEXT NOT NULL,
            warehouse_id      INTEGER NOT NULL,
            product_id        INTEGER NOT NULL,   -- → demo_master.products.product_id
            move_type         TEXT NOT NULL,      -- 'IN'入庫 'OUT'出庫 'ADJ'棚卸調整
            qty               INTEGER NOT NULL,
            order_id          INTEGER,            -- → demo_sales.orders.order_id (OUT時のみ)
            FOREIGN KEY (warehouse_id) REFERENCES warehouses(warehouse_id)
        );
        CREATE TABLE purchase_orders (
            purchase_order_id INTEGER PRIMARY KEY,
            po_no             TEXT NOT NULL UNIQUE,
            po_date           TEXT NOT NULL,
            supplier_id       INTEGER NOT NULL,   -- → demo_master.suppliers.supplier_id
            warehouse_id      INTEGER NOT NULL,
            status            TEXT NOT NULL,      -- '1'発注済 '2'入荷済 '9'中止
            expected_date     TEXT NOT NULL,
            FOREIGN KEY (warehouse_id) REFERENCES warehouses(warehouse_id)
        );
        CREATE TABLE purchase_items (
            purchase_item_id  INTEGER PRIMARY KEY,
            purchase_order_id INTEGER NOT NULL,
            product_id        INTEGER NOT NULL,   -- → demo_master.products.product_id
            qty               INTEGER NOT NULL,
            unit_cost         INTEGER NOT NULL,
            FOREIGN KEY (purchase_order_id) REFERENCES purchase_orders(purchase_order_id)
        );
        CREATE INDEX idx_mv_date ON stock_movements(moved_at);
    """)
    rng = random.Random(11)
    conn.executemany("INSERT INTO warehouses VALUES(?,?,?,?)", WAREHOUSES)

    sid = 0
    for w in WAREHOUSES:
        for pid in range(1, len(PRODUCTS) + 1):
            if rng.random() < 0.15:   # 全倉庫に全商品があるわけではない
                continue
            sid += 1
            safety = rng.choice([20, 30, 50, 80])
            qty = max(0, int(rng.gauss(safety * 2.2, safety)))
            upd = TODAY - timedelta(days=rng.randint(0, 3))
            conn.execute("INSERT INTO stocks VALUES(?,?,?,?,?,?)",
                         (sid, w[0], pid, qty, safety, upd.isoformat()))

    mid = 0
    for d in range(DAYS, -1, -1):
        day = TODAY - timedelta(days=d)
        if day.weekday() >= 5:
            continue
        for _ in range(rng.randint(8, 20)):
            mid += 1
            mt = rng.choices(["OUT", "IN", "ADJ"], weights=[62, 33, 5])[0]
            conn.execute("INSERT INTO stock_movements VALUES(?,?,?,?,?,?,?)",
                         (mid, day.isoformat(), rng.randint(1, len(WAREHOUSES)),
                          rng.randint(1, len(PRODUCTS)), mt, rng.randint(1, 60),
                          rng.randint(1, 1500) if mt == "OUT" else None))

    poid = pitem = 0
    for d in range(DAYS, -1, -1):
        day = TODAY - timedelta(days=d)
        if rng.random() > 0.45:
            continue
        poid += 1
        lead = rng.choice([3, 5, 7, 10, 14])
        conn.execute("INSERT INTO purchase_orders VALUES(?,?,?,?,?,?,?)",
                     (poid, f"PO{day.strftime('%y%m%d')}-{poid:04d}", day.isoformat(),
                      rng.randint(1, len(SUPPLIERS)), rng.randint(1, len(WAREHOUSES)),
                      rng.choices(["1", "2", "9"], weights=[18, 78, 4])[0],
                      (day + timedelta(days=lead)).isoformat()))
        for _ in range(rng.randint(1, 5)):
            pitem += 1
            pid = rng.randint(1, len(PRODUCTS))
            cost = int(PRODUCTS[pid - 1][2] * rng.uniform(0.55, 0.75))
            conn.execute("INSERT INTO purchase_items VALUES(?,?,?,?,?)",
                         (pitem, poid, pid, rng.randint(10, 300), cost))

    conn.commit()
    conn.close()
    print(f"OK  {INVENTORY_DB.name}: warehouses={len(WAREHOUSES)}, stocks={sid}, "
          f"stock_movements={mid}, purchase_orders={poid}, purchase_items={pitem}")


# --- demo_hr ------------------------------------------------------------------

def build_hr():
    _reset(HR_DB)
    conn = sqlite3.connect(str(HR_DB))
    conn.executescript("""
        CREATE TABLE attendances (
            attendance_id INTEGER PRIMARY KEY,
            employee_id   INTEGER NOT NULL,       -- → demo_master.employees.employee_id
            work_date     TEXT NOT NULL,
            check_in      TEXT,
            check_out     TEXT,
            overtime_min  INTEGER NOT NULL,
            status        TEXT NOT NULL           -- '1'出勤 '2'遅刻 '3'早退 '4'欠勤
        );
        CREATE TABLE leaves (
            leave_id    INTEGER PRIMARY KEY,
            employee_id INTEGER NOT NULL,         -- → demo_master.employees.employee_id
            leave_date  TEXT NOT NULL,
            leave_type  TEXT NOT NULL,            -- '01'年休 '02'特別休暇 '03'病休 '04'代休
            days        REAL NOT NULL
        );
        CREATE TABLE evaluations (
            evaluation_id INTEGER PRIMARY KEY,
            employee_id   INTEGER NOT NULL,       -- → demo_master.employees.employee_id
            period        TEXT NOT NULL,          -- 'YYYY-H1' / 'YYYY-H2'
            score         INTEGER NOT NULL,       -- 0-100
            grade         TEXT NOT NULL           -- 'S'/'A'/'B'/'C'
        );
        CREATE TABLE salaries (
            salary_id    INTEGER PRIMARY KEY,
            employee_id  INTEGER NOT NULL,        -- → demo_master.employees.employee_id
            pay_month    TEXT NOT NULL,           -- 'YYYY-MM'
            base         INTEGER NOT NULL,
            overtime_pay INTEGER NOT NULL,
            total        INTEGER NOT NULL
        );
        CREATE TABLE certifications (
            certification_id INTEGER PRIMARY KEY,
            employee_id      INTEGER NOT NULL,    -- → demo_master.employees.employee_id
            cert_name        TEXT NOT NULL,
            acquired_date    TEXT NOT NULL
        );
        CREATE INDEX idx_att_emp ON attendances(employee_id, work_date);
    """)
    rng = random.Random(23)
    aid = lid = eid = sid = cid = 0

    for d in range(DAYS, -1, -1):
        day = TODAY - timedelta(days=d)
        if day.weekday() >= 5:
            continue
        for emp in range(1, N_EMPLOYEES + 1):
            aid += 1
            st = rng.choices(["1", "2", "3", "4"], weights=[92, 4, 2, 2])[0]
            if st == "4":
                conn.execute("INSERT INTO attendances VALUES(?,?,?,?,?,?,?)",
                             (aid, emp, day.isoformat(), None, None, 0, st))
                continue
            cin_m = 9 * 60 + (rng.randint(11, 40) if st == "2" else rng.randint(-20, 8))
            ot = max(0, int(rng.gauss(45, 50))) if rng.random() < 0.55 else 0
            cout_m = 18 * 60 + ot - (rng.randint(60, 180) if st == "3" else 0)
            conn.execute("INSERT INTO attendances VALUES(?,?,?,?,?,?,?)",
                         (aid, emp, day.isoformat(),
                          f"{cin_m // 60:02d}:{cin_m % 60:02d}",
                          f"{cout_m // 60:02d}:{cout_m % 60:02d}", ot, st))

    for emp in range(1, N_EMPLOYEES + 1):
        for _ in range(rng.randint(2, 9)):
            lid += 1
            day = TODAY - timedelta(days=rng.randint(0, DAYS))
            conn.execute("INSERT INTO leaves VALUES(?,?,?,?,?)",
                         (lid, emp, day.isoformat(),
                          rng.choices(["01", "02", "03", "04"], weights=[70, 10, 12, 8])[0],
                          rng.choice([0.5, 1.0, 1.0, 1.0])))
        for period in (f"{TODAY.year - 1}-H2", f"{TODAY.year}-H1"):
            eid += 1
            score = max(30, min(100, int(rng.gauss(72, 12))))
            grade = "S" if score >= 90 else "A" if score >= 78 else "B" if score >= 60 else "C"
            conn.execute("INSERT INTO evaluations VALUES(?,?,?,?,?)", (eid, emp, period, score, grade))
        base = rng.choice([260, 280, 310, 340, 380, 420, 480]) * 1000
        for m in range(6):
            sid += 1
            mo = (TODAY.replace(day=1) - timedelta(days=30 * m))
            ot_pay = rng.randint(0, 90) * 1000
            conn.execute("INSERT INTO salaries VALUES(?,?,?,?,?,?)",
                         (sid, emp, mo.strftime("%Y-%m"), base, ot_pay, base + ot_pay))
        for cert in rng.sample(CERTS, rng.randint(0, 3)):
            cid += 1
            conn.execute("INSERT INTO certifications VALUES(?,?,?,?)",
                         (cid, emp, cert, (TODAY - timedelta(days=rng.randint(100, 3000))).isoformat()))

    conn.commit()
    conn.close()
    print(f"OK  {HR_DB.name}: attendances={aid}, leaves={lid}, evaluations={eid}, "
          f"salaries={sid}, certifications={cid}")


# --- demo_support -------------------------------------------------------------

def build_support():
    _reset(SUPPORT_DB)
    conn = sqlite3.connect(str(SUPPORT_DB))
    conn.executescript("""
        CREATE TABLE tickets (
            ticket_id   INTEGER PRIMARY KEY,
            ticket_no   TEXT NOT NULL UNIQUE,
            opened_at   TEXT NOT NULL,
            closed_at   TEXT,
            customer_id INTEGER NOT NULL,         -- → demo_master.customers.customer_id
            employee_id INTEGER NOT NULL,         -- → demo_master.employees.employee_id (担当者)
            category    TEXT NOT NULL,
            priority    TEXT NOT NULL,            -- 'P1'最優先 〜 'P4'低
            status      TEXT NOT NULL             -- 'open'/'pending'/'closed'
        );
        CREATE TABLE ticket_messages (
            ticket_message_id INTEGER PRIMARY KEY,
            ticket_id         INTEGER NOT NULL,
            posted_at         TEXT NOT NULL,
            sender_type       TEXT NOT NULL,      -- 'customer'/'agent'
            body_len          INTEGER NOT NULL,
            FOREIGN KEY (ticket_id) REFERENCES tickets(ticket_id)
        );
        CREATE TABLE satisfactions (
            satisfaction_id INTEGER PRIMARY KEY,
            ticket_id       INTEGER NOT NULL UNIQUE,
            answered_at     TEXT NOT NULL,
            score           INTEGER NOT NULL,     -- 1-5。4以上が「満足」
            FOREIGN KEY (ticket_id) REFERENCES tickets(ticket_id)
        );
        CREATE TABLE escalations (
            escalation_id INTEGER PRIMARY KEY,
            ticket_id     INTEGER NOT NULL,
            escalated_at  TEXT NOT NULL,
            department_id INTEGER NOT NULL,       -- → demo_master.departments.department_id (連携先)
            reason        TEXT NOT NULL,
            FOREIGN KEY (ticket_id) REFERENCES tickets(ticket_id)
        );
        CREATE TABLE faq_articles (
            faq_article_id INTEGER PRIMARY KEY,
            title          TEXT NOT NULL,
            category       TEXT NOT NULL,
            view_count     INTEGER NOT NULL,
            helpful_count  INTEGER NOT NULL,
            published_at   TEXT NOT NULL
        );
        CREATE INDEX idx_tk_opened ON tickets(opened_at);
    """)
    rng = random.Random(31)
    tid = mid = sid = eid = 0
    fmt = "%Y-%m-%d %H:%M:%S"

    for d in range(DAYS, -1, -1):
        day = TODAY - timedelta(days=d)
        n = rng.randint(2, 7) if day.weekday() < 5 else rng.randint(0, 2)
        for _ in range(n):
            tid += 1
            prio = rng.choices(["P1", "P2", "P3", "P4"], weights=[6, 20, 50, 24])[0]
            # 受付日時を先に確定させ、以降の時刻はすべてこれを起点にする
            # （0時起点で計算すると closed_at < opened_at になり対応時間が負になる）
            opened_at = datetime.combine(day, dtime(rng.randint(9, 18), rng.randint(0, 59)))
            closed = rng.random() < (0.95 if d > 14 else 0.6)
            hours = max(1, int(rng.gauss({"P1": 6, "P2": 20, "P3": 48, "P4": 90}[prio], 20)))
            closed_at = (opened_at + timedelta(hours=hours)) if closed else None
            status = "closed" if closed else rng.choice(["open", "pending"])
            conn.execute("INSERT INTO tickets VALUES(?,?,?,?,?,?,?,?,?)",
                         (tid, f"TK{day.strftime('%y%m%d')}-{tid:05d}",
                          opened_at.strftime(fmt),
                          closed_at.strftime(fmt) if closed_at else None,
                          rng.randint(1, N_CUSTOMERS), rng.randint(1, N_EMPLOYEES),
                          rng.choice(TICKET_CATEGORIES), prio, status))
            n_msg = rng.randint(2, 8)
            span = (closed_at - opened_at) if closed_at else timedelta(hours=hours)
            for k in range(n_msg):
                mid += 1
                posted = opened_at + span * (k / max(1, n_msg))
                conn.execute("INSERT INTO ticket_messages VALUES(?,?,?,?,?)",
                             (mid, tid, posted.strftime(fmt),
                              "customer" if k % 2 == 0 else "agent", rng.randint(20, 800)))
            if closed and rng.random() < 0.65:
                sid += 1
                answered = closed_at + timedelta(hours=rng.randint(1, 48))
                conn.execute("INSERT INTO satisfactions VALUES(?,?,?,?)",
                             (sid, tid, answered.strftime(fmt),
                              rng.choices([1, 2, 3, 4, 5], weights=[4, 7, 19, 40, 30])[0]))
            if prio in ("P1", "P2") and rng.random() < 0.35:
                eid += 1
                esc = opened_at + timedelta(hours=max(1, int(hours * 0.3)))
                conn.execute("INSERT INTO escalations VALUES(?,?,?,?,?)",
                             (eid, tid, esc.strftime(fmt),
                              rng.choice([1, 3, 4, 5]),
                              rng.choice(["技術判断が必要", "在庫確認が必要", "価格交渉", "品質調査"])))

    for i, cat in enumerate([c for c in TICKET_CATEGORIES for _ in range(5)], start=1):
        v = rng.randint(50, 5000)
        conn.execute("INSERT INTO faq_articles VALUES(?,?,?,?,?,?)",
                     (i, f"{cat}に関するFAQ #{i}", cat, v, int(v * rng.uniform(0.05, 0.4)),
                      (TODAY - timedelta(days=rng.randint(30, 900))).isoformat()))

    conn.commit()
    conn.close()
    print(f"OK  {SUPPORT_DB.name}: tickets={tid}, ticket_messages={mid}, satisfactions={sid}, "
          f"escalations={eid}, faq_articles={len(TICKET_CATEGORIES) * 5}")


# --- メタ情報（人間の知識） ------------------------------------------------------

META = {
    "demo_master": (MASTER_DB, {
        "title": "マスタDB",
        "description": "全システム共通のマスタ。受注・在庫・人事・サポートの各DBから参照される中心（ハブ）。"
                       "主キーは <エンティティ>_id で、参照側の外部キーも同じ列名なので "
                       "JOIN ... USING (employee_id) のように書ける。",
        "caveats": ["退職者も employees に残る。現役だけを見るなら active_flag = 1 で絞る。",
                    "各マスタには代理キー(xxx_id)とは別に業務キー(emp_code/cust_code/prod_code)がある。"
                    "結合には xxx_id を使い、業務キーは人が読む識別子として扱う。"],
        "tables": {
            "departments": {"description": "部門マスタ。1行 = 1部門。"},
            "suppliers": {
                "description": "仕入先マスタ。1行 = 1仕入先。",
                "columns": {
                    "lead_time_days": {"description": "発注から入荷までの標準日数"},
                    "rank": {"description": "取引ランク", "values": {"A": "重要", "B": "通常", "C": "スポット"}},
                },
            },
            "employees": {
                "description": "社員マスタ。1行 = 1社員。営業担当・サポート担当として各DBから参照される。"
                               "employee_id は社員1人につき1つ（ユニーク）。",
                "columns": {
                    "employee_id": {"description": "社員ID（代理キー）。各DBの employee_id はこれを指す"},
                    "emp_code": {"description": "社員番号（業務キー）。'E0001' 形式。人が読む識別子"},
                    "role": {"description": "役職", "values": {"1": "一般", "2": "主任", "3": "課長", "4": "部長"}},
                    "active_flag": {"description": "在籍フラグ", "values": {"1": "在籍", "0": "退職"}},
                },
                "glossary": {
                    "現役社員": {"description": "退職していない、いま在籍している社員。"
                                              "単に「社員」と言われたときも通常はこちらを指す。",
                                 "sql": "active_flag = 1"},
                },
            },
            "customers": {
                "description": "顧客マスタ。1行 = 1顧客企業。",
                "columns": {
                    "cust_code": {"description": "顧客コード（業務キー）。'C0001' 形式"},
                    "rank": {"description": "顧客ランク", "values": {"A": "重要顧客", "B": "通常", "C": "小口"}},
                    "opened_date": {"description": "取引開始日"},
                },
            },
            "products": {
                "description": "商品マスタ。1行 = 1商品。unit_price は定価（円・税抜）。",
                "columns": {
                    "prod_code": {"description": "商品コード（業務キー）。'P0001' 形式"},
                    "disc_flag": {"description": "廃番フラグ", "values": {"1": "廃番", "0": "現行品"}},
                },
                "glossary": {
                    "現行品": {"description": "廃番になっておらず、いま売っている商品。",
                               "sql": "disc_flag = 0"},
                    "高額商品": {"description": "定価が5万円以上の商品のこと。"},   # SQL式なしの例
                },
            },
        },
        "relationships": [
            {"from": "employees.department_id", "to": "departments.department_id", "cardinality": "N:1"},
            {"from": "products.supplier_id", "to": "suppliers.supplier_id", "cardinality": "N:1"},
        ],
    }),
    "demo_sales": (SALES_DB, {
        "title": "販売管理DB",
        "description": "受注・見積・請求・入金。顧客と商品と営業担当は demo_master 側にある。直近約6ヶ月分。",
        "caveats": ["金額列を持つのは order_items（明細）。orders に金額列は無いので必ず明細を集計する。"],
        "tables": {
            "orders": {
                "description": "受注ヘッダ。1行 = 1受注（金額は order_items を集計する）。",
                "columns": {
                    "status": {"description": "受注状態",
                               "values": {"1": "受付", "2": "出荷準備", "3": "出荷済", "9": "キャンセル"}},
                    "kbn": {"description": "取引区分。売上集計では通常のみを対象にする",
                            "values": {"1": "通常", "2": "サンプル出荷", "3": "社内利用"}},
                    "employee_id": {"description": "営業担当の社員ID（demo_master.employees.employee_id）"},
                },
                "glossary": {
                    "有効な受注": {"description": "キャンセルされておらず、取引区分が通常の受注。"
                                                "売上を数えるときは必ずこれで絞る"
                                                "（サンプル出荷や社内利用は売上ではないため）。",
                                   "sql": "orders.status != '9' AND orders.kbn = '1'"},
                    "出荷済": {"description": "商品を出荷し終えた受注。", "sql": "orders.status = '3'"},
                },
            },
            "order_items": {
                "description": "受注明細。1行 = 受注1行分の商品。売上金額 = qty * unit_price * (1 - discount_rate)。",
                "columns": {
                    "discount_rate": {"description": "値引率。0.1 なら10%引き"},
                    "unit_price": {"description": "受注時点の単価（円）。定価と異なる場合があるのでこちらを使う"},
                },
                "glossary": {
                    "売上金額": {"description": "値引後の金額。数量×単価×(1-値引率) を合計したもの。"
                                              "orders と結合して「有効な受注」だけを対象にする。",
                                 "sql": "SUM(order_items.qty * order_items.unit_price "
                                        "* (1 - order_items.discount_rate))"},
                },
            },
            "quotes": {
                "description": "見積。1行 = 1見積。受注に至ったかは result で判断する。",
                "columns": {"result": {"description": "見積結果",
                                       "values": {"W": "受注", "L": "失注", "P": "進行中"}}},
                "glossary": {
                    "受注率": {"description": "決着した見積のうち受注できた割合。"
                                            "進行中(P)は決着していないので分母に入れない。",
                               "sql": "SUM(CASE WHEN result = 'W' THEN 1 ELSE 0 END) * 1.0 "
                                      "/ NULLIF(SUM(CASE WHEN result IN ('W','L') THEN 1 ELSE 0 END), 0)"},
                },
            },
            "invoices": {
                "description": "請求。出荷済かつ通常区分の受注に対して発行される。1行 = 1請求。",
                "columns": {"paid_flag": {"description": "入金済フラグ", "values": {"1": "入金済", "0": "未入金"}}},
                "glossary": {
                    "未回収": {"description": "請求したのにまだ入金されていないもの。", "sql": "paid_flag = 0"},
                },
            },
            "payments": {
                "description": "入金。1行 = 1入金。",
                "columns": {"method": {"description": "入金方法",
                                       "values": {"BANK": "銀行振込", "CARD": "クレジット", "CASH": "現金"}}},
            },
        },
        "relationships": [
            {"from": "order_items.order_id", "to": "orders.order_id", "cardinality": "N:1"},
            {"from": "invoices.order_id", "to": "orders.order_id", "cardinality": "1:1"},
            {"from": "payments.invoice_id", "to": "invoices.invoice_id", "cardinality": "N:1"},
            {"from": "orders.customer_id", "to": "demo_master.customers.customer_id", "cardinality": "N:1"},
            {"from": "orders.employee_id", "to": "demo_master.employees.employee_id", "cardinality": "N:1"},
            {"from": "order_items.product_id", "to": "demo_master.products.product_id", "cardinality": "N:1"},
            {"from": "quotes.customer_id", "to": "demo_master.customers.customer_id", "cardinality": "N:1"},
            {"from": "quotes.employee_id", "to": "demo_master.employees.employee_id", "cardinality": "N:1"},
        ],
        "glossary": {   # 複数テーブルにまたがる用語だけをDB全体に置く
            "客単価": {"description": "顧客1社あたりの売上金額。"
                                    "有効な受注の売上金額を、受注のあった顧客数で割ったもの。"},
        },
        "examples": [
            {"q": "月別の売上金額の推移を教えて",
             "sql": "SELECT strftime('%Y-%m', o.order_date) AS 月, "
                    "CAST(SUM(i.qty*i.unit_price*(1-i.discount_rate)) AS INTEGER) AS 売上金額 "
                    "FROM demo_sales.orders o JOIN demo_sales.order_items i USING (order_id) "
                    "WHERE o.status != '9' AND o.kbn = '1' GROUP BY 1 ORDER BY 1"},
            {"q": "顧客ランク別の売上を教えて",
             "sql": "SELECT c.rank AS 顧客ランク, "
                    "CAST(SUM(i.qty*i.unit_price*(1-i.discount_rate)) AS INTEGER) AS 売上金額 "
                    "FROM demo_sales.orders o "
                    "JOIN demo_sales.order_items i USING (order_id) "
                    "JOIN demo_master.customers c USING (customer_id) "
                    "WHERE o.status != '9' AND o.kbn = '1' GROUP BY 1 ORDER BY 1"},
        ],
        # 相互検証（検算）。同じ数字を別の経路で数えて突き合わせる。
        # 明細と請求はわざと食い違う（未請求の受注がある）——検算が差を
        # 見つけて内訳を出すところまでがデモになっている。
        "checks": [
            {"name": "入金と請求（入金済）の一致",
             "left": {"label": "入金の合計（payments）",
                      "sql": "SELECT SUM(amount) FROM demo_sales.payments"},
             "right": {"label": "請求のうち入金済み（invoices.paid_flag=1）",
                       "sql": "SELECT SUM(amount) FROM demo_sales.invoices "
                              "WHERE paid_flag = 1"},
             "tolerance_pct": 0.1,
             "drilldown": "SELECT i.invoice_id AS 請求ID, i.amount AS 請求額 "
                          "FROM demo_sales.invoices i WHERE i.paid_flag = 1 "
                          "AND NOT EXISTS (SELECT 1 FROM demo_sales.payments p "
                          "WHERE p.invoice_id = i.invoice_id) ORDER BY i.amount DESC",
             "enabled": True},
            {"name": "売上明細と請求額の一致",
             "left": {"label": "明細の売上（数量×単価×(1-値引)、キャンセル除く）",
                      "sql": "SELECT CAST(SUM(i.qty * i.unit_price * (1 - i.discount_rate)) "
                             "AS INTEGER) FROM demo_sales.orders o "
                             "JOIN demo_sales.order_items i USING (order_id) "
                             "WHERE o.status != '9'"},
             "right": {"label": "請求書の合計（invoices.amount）",
                       "sql": "SELECT SUM(amount) FROM demo_sales.invoices"},
             "tolerance_pct": 1.0,
             "drilldown": "SELECT o.order_id AS 受注ID, o.order_date AS 受注日, "
                          "o.status AS 状態, CAST(SUM(i.qty * i.unit_price * "
                          "(1 - i.discount_rate)) AS INTEGER) AS 金額 "
                          "FROM demo_sales.orders o "
                          "JOIN demo_sales.order_items i USING (order_id) "
                          "WHERE o.status != '9' AND NOT EXISTS "
                          "(SELECT 1 FROM demo_sales.invoices v WHERE v.order_id = o.order_id) "
                          "GROUP BY o.order_id ORDER BY 金額 DESC",
             "enabled": True},
        ],
    }),
    "demo_inventory": (INVENTORY_DB, {
        "title": "在庫・購買DB",
        "description": "倉庫在庫と入出庫、仕入先への発注。商品と仕入先は demo_master 側にある。",
        "caveats": ["stocks は現在庫のスナップショット（履歴ではない）。推移を見るなら stock_movements を使う。"],
        "tables": {
            "warehouses": {"description": "倉庫マスタ。1行 = 1倉庫。"},
            "stocks": {
                "description": "現在庫。1行 = 倉庫×商品の在庫。全倉庫に全商品があるわけではない。",
                "columns": {"safety_qty": {"description": "安全在庫数。qty がこれを下回ると在庫不足"}},
                "glossary": {
                    "在庫不足": {"description": "在庫数が安全在庫を下回っている状態。補充が必要。",
                                 "sql": "stocks.qty < stocks.safety_qty"},
                },
            },
            "stock_movements": {
                "description": "入出庫履歴。1行 = 1回の入出庫。",
                "columns": {
                    "move_type": {"description": "区分",
                                  "values": {"IN": "入庫", "OUT": "出庫", "ADJ": "棚卸調整"}},
                    "order_id": {"description": "出庫の元になった受注ID（demo_sales.orders.order_id）。"
                                                "OUT以外はNULL"},
                },
            },
            "purchase_orders": {
                "description": "発注ヘッダ。1行 = 1発注。",
                "columns": {"status": {"description": "発注状態",
                                       "values": {"1": "発注済", "2": "入荷済", "9": "中止"}}},
            },
            "purchase_items": {
                "description": "発注明細。1行 = 発注1行分の商品。仕入金額 = qty * unit_cost。",
                "columns": {"unit_cost": {"description": "仕入単価（円）。定価とは異なる"}},
                "glossary": {
                    "仕入金額": {"description": "発注した数量×仕入単価の合計。",
                                 "sql": "SUM(purchase_items.qty * purchase_items.unit_cost)"},
                },
            },
        },
        "relationships": [
            {"from": "stocks.warehouse_id", "to": "warehouses.warehouse_id", "cardinality": "N:1"},
            {"from": "stock_movements.warehouse_id", "to": "warehouses.warehouse_id", "cardinality": "N:1"},
            {"from": "purchase_items.purchase_order_id", "to": "purchase_orders.purchase_order_id",
             "cardinality": "N:1"},
            {"from": "purchase_orders.warehouse_id", "to": "warehouses.warehouse_id", "cardinality": "N:1"},
            {"from": "stocks.product_id", "to": "demo_master.products.product_id", "cardinality": "N:1"},
            {"from": "stock_movements.product_id", "to": "demo_master.products.product_id", "cardinality": "N:1"},
            {"from": "purchase_items.product_id", "to": "demo_master.products.product_id", "cardinality": "N:1"},
            {"from": "purchase_orders.supplier_id", "to": "demo_master.suppliers.supplier_id",
             "cardinality": "N:1"},
            {"from": "stock_movements.order_id", "to": "demo_sales.orders.order_id", "cardinality": "N:1"},
        ],
        "glossary": {
            "在庫回転": {"description": "その商品がどれだけ動いているか。"
                                      "一定期間の出庫数量(stock_movements)を現在庫(stocks)で割った値。"},
        },
        "examples": [
            {"q": "在庫不足の商品を倉庫ごとに教えて",
             "sql": "SELECT w.name AS 倉庫, p.name AS 商品, s.qty AS 在庫数, s.safety_qty AS 安全在庫 "
                    "FROM demo_inventory.stocks s "
                    "JOIN demo_inventory.warehouses w USING (warehouse_id) "
                    "JOIN demo_master.products p USING (product_id) "
                    "WHERE s.qty < s.safety_qty ORDER BY w.name, s.qty"},
        ],
    }),
    "demo_hr": (HR_DB, {
        "title": "人事DB",
        "description": "勤怠・休暇・評価・給与・資格。社員の氏名や部門は demo_master.employees 側にある。",
        "caveats": ["attendances は平日のみ記録される（土日祝の行は無い）。"],
        "tables": {
            "attendances": {
                "description": "日次勤怠。1行 = 社員1人の1日。平日のみ。",
                "columns": {
                    "status": {"description": "勤怠区分",
                               "values": {"1": "出勤", "2": "遅刻", "3": "早退", "4": "欠勤"}},
                    "overtime_min": {"description": "残業時間（分）"},
                    "check_in": {"description": "出勤打刻 'HH:MM'。欠勤日はNULL"},
                },
                "glossary": {
                    "月間残業時間": {"description": "1ヶ月の残業を時間で表したもの。"
                                                  "記録は分単位なので60で割る。",
                                     "sql": "SUM(attendances.overtime_min) / 60.0"},
                    "出勤率": {"description": "欠勤しなかった割合。遅刻・早退は出勤に含める。",
                               "sql": "SUM(CASE WHEN status IN ('1','2','3') THEN 1 ELSE 0 END) "
                                      "* 1.0 / COUNT(*)"},
                },
            },
            "leaves": {
                "description": "休暇取得。1行 = 1回の休暇取得。",
                "columns": {"leave_type": {"description": "休暇種別",
                                           "values": {"01": "年次有給", "02": "特別休暇",
                                                      "03": "病気休暇", "04": "代休"}},
                            "days": {"description": "取得日数。0.5 は半休"}},
            },
            "evaluations": {
                "description": "半期評価。1行 = 社員1人の1期分。",
                "columns": {"period": {"description": "評価期間。'2026-H1' は上期"},
                            "grade": {"description": "評価",
                                      "values": {"S": "卓越", "A": "優秀", "B": "標準", "C": "要改善"}}},
            },
            "salaries": {
                "description": "月次給与。1行 = 社員1人の1ヶ月分。total = base + overtime_pay。",
                "columns": {"pay_month": {"description": "支給月 'YYYY-MM'"}},
            },
            "certifications": {"description": "保有資格。1行 = 社員1人の1資格。"},
        },
        "relationships": [
            {"from": "attendances.employee_id", "to": "demo_master.employees.employee_id", "cardinality": "N:1"},
            {"from": "leaves.employee_id", "to": "demo_master.employees.employee_id", "cardinality": "N:1"},
            {"from": "evaluations.employee_id", "to": "demo_master.employees.employee_id", "cardinality": "N:1"},
            {"from": "salaries.employee_id", "to": "demo_master.employees.employee_id", "cardinality": "N:1"},
            {"from": "certifications.employee_id", "to": "demo_master.employees.employee_id", "cardinality": "N:1"},
        ],
        "glossary": {
            "働きすぎ": {"description": "残業が多く負荷が高い状態の社員。"
                                      "勤怠(attendances)の残業時間と、休暇(leaves)の取得日数を"
                                      "あわせて見る必要がある。"},
        },
        "examples": [
            {"q": "部門別の平均残業時間を教えて",
             "sql": "SELECT d.name AS 部門, ROUND(SUM(a.overtime_min)/60.0/COUNT(DISTINCT a.employee_id),1) "
                    "AS 一人あたり残業時間 FROM demo_hr.attendances a "
                    "JOIN demo_master.employees e USING (employee_id) "
                    "JOIN demo_master.departments d USING (department_id) "
                    "GROUP BY 1 ORDER BY 2 DESC"},
        ],
    }),
    "demo_support": (SUPPORT_DB, {
        "title": "サポートDB",
        "description": "問い合わせチケットと対応履歴・満足度。顧客と担当者は demo_master 側にある。",
        "caveats": ["opened_at / closed_at は 'YYYY-MM-DD HH:MM:SS' の文字列。日付だけ使うなら date() で切る。"],
        "tables": {
            "tickets": {
                "description": "問い合わせチケット。1行 = 1問い合わせ。未クローズは closed_at が NULL。",
                "columns": {
                    "priority": {"description": "優先度",
                                 "values": {"P1": "最優先", "P2": "高", "P3": "中", "P4": "低"}},
                    "status": {"description": "対応状態",
                               "values": {"open": "未対応", "pending": "保留中", "closed": "完了"}},
                    "employee_id": {"description": "サポート担当者の社員ID"
                                                   "（demo_master.employees.employee_id）"},
                },
                "glossary": {
                    "対応時間": {"description": "問い合わせを受けてから完了するまでにかかった時間。"
                                              "単位は時間。まだ終わっていないチケットは対象外。",
                                 "sql": "(julianday(closed_at) - julianday(opened_at)) * 24"},
                    "解決率": {"description": "受けた問い合わせのうち、完了まで持っていけた割合。",
                               "sql": "SUM(CASE WHEN status = 'closed' THEN 1 ELSE 0 END) "
                                      "* 1.0 / COUNT(*)"},
                    "滞留チケット": {"description": "まだ完了しておらず、開いてから時間が経っているもの。"},
                },
            },
            "ticket_messages": {
                "description": "チケット内のやりとり。1行 = 1メッセージ。",
                "columns": {"sender_type": {"description": "送信者",
                                            "values": {"customer": "顧客", "agent": "サポート担当"}},
                            "body_len": {"description": "本文の文字数（本文そのものは保持しない）"}},
            },
            "satisfactions": {
                "description": "顧客満足度アンケート。1行 = 1チケット（回答があったもののみ）。",
                "columns": {"score": {"description": "満足度 1〜5。4以上を「満足」とみなす"}},
                "glossary": {
                    "満足": {"description": "5段階のアンケートで4以上を付けてもらえたこと。",
                             "sql": "satisfactions.score >= 4"},
                },
            },
            "escalations": {
                "description": "他部門へのエスカレーション。1行 = 1エスカレーション。",
                "columns": {"department_id": {"description": "エスカレーション先の部門ID"
                                                             "（demo_master.departments.department_id）"}},
            },
            "faq_articles": {"description": "FAQ記事。チケットとは直接紐づかない。"},
        },
        "relationships": [
            {"from": "ticket_messages.ticket_id", "to": "tickets.ticket_id", "cardinality": "N:1"},
            {"from": "satisfactions.ticket_id", "to": "tickets.ticket_id", "cardinality": "1:1"},
            {"from": "escalations.ticket_id", "to": "tickets.ticket_id", "cardinality": "N:1"},
            {"from": "tickets.customer_id", "to": "demo_master.customers.customer_id", "cardinality": "N:1"},
            {"from": "tickets.employee_id", "to": "demo_master.employees.employee_id", "cardinality": "N:1"},
            {"from": "escalations.department_id", "to": "demo_master.departments.department_id",
             "cardinality": "N:1"},
        ],
        "glossary": {
            "手のかかった問い合わせ": {"description": "やりとりの回数が多かったり、"
                                                  "他部門へエスカレーションされた問い合わせ。"
                                                  "tickets と ticket_messages、escalations を"
                                                  "あわせて見る。"},
        },
        "examples": [
            {"q": "優先度別の平均対応時間を教えて",
             "sql": "SELECT priority AS 優先度, "
                    "ROUND(AVG((julianday(closed_at)-julianday(opened_at))*24),1) AS 平均対応時間 "
                    "FROM demo_support.tickets WHERE closed_at IS NOT NULL GROUP BY 1 ORDER BY 1"},
        ],
    }),
}


def write_meta():
    for name, (path, meta) in META.items():
        mp = catalog.meta_path(path)
        if mp.exists():
            print(f"SKIP {mp.name} は既存のため上書きしません（作り直すなら削除してから実行）")
            continue
        catalog.save_meta(path, meta)
        print(f"OK  {mp.name} を作成")


if __name__ == "__main__":
    build_master()
    build_sales()
    build_inventory()
    build_hr()
    build_support()
    print()
    write_meta()
    print("""
5DB × 各5テーブル を作成しました。
DB内の参照は FOREIGN KEY 宣言 → 自動検出されます。
DBをまたぐ参照は SQLite では宣言できないため .meta.yaml の relationships に登録済みです。

横断クエリの例:
  「顧客ランク別の売上金額を棒グラフで」        … sales × master (2DB)
  「部門別の平均残業時間を教えて」                … hr × master (2DB)
  「営業担当ごとの売上と、その人の残業時間を比べて」… sales × master × hr (3DB)
  「出庫が多い商品トップ10と、その在庫数」        … inventory × master (2DB)
""")
