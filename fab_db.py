"""半導体工場のデモDB生成スクリプト（5DB × 全26テーブル）。

  python fab_db.py

前工程（ウェハ処理）のラインを模した構成:

    fab_master (マスタ) ◀── fab_production / fab_equipment / fab_parts / fab_quality

| DB | テーブル |
|---|---|
| fab_master     | areas, equipments, processes, products, routes, equipment_capabilities, recipes |
| fab_production | lots, wip, process_results, lot_holds, shipments |
| fab_equipment  | equipment_states, equipment_daily, alarms, maintenances, equipment_meters |
| fab_parts      | parts, equipment_parts, part_stocks, part_movements, part_replacements, part_orders |
| fab_quality    | measurements, defects, yields |

列名の方針は sample_db.py と同じ。主キーは `<エンティティ単数形>_id`、
外部キーは参照先と同じ列名（JOINが `USING (equipment_id)` で書ける）。

データは辻褄を合わせてある:
  - 工程実績の装置は「その工程を処理できる装置」（equipment_capabilities）から選ぶ
  - 日次稼働サマリは稼働状態ログを日ごとに集計した値
  - 仕掛(wip)は、まだ完了していないロットの現在工程と一致する
  - 部品の交換履歴は保全記録と紐づき、在庫の出庫も同時に立つ
そのため「装置の稼働率と歩留まりの関係」のような横断分析が成り立つ。

.meta.yaml は既存なら上書きしない（人間の編集を守るため）。
"""
from __future__ import annotations

import random
import sqlite3
from datetime import date, datetime, timedelta

import catalog
import config

MASTER_DB = config.DATA_DIR / "fab_master.db"
PROD_DB = config.DATA_DIR / "fab_production.db"
EQP_DB = config.DATA_DIR / "fab_equipment.db"
PARTS_DB = config.DATA_DIR / "fab_parts.db"
QUAL_DB = config.DATA_DIR / "fab_quality.db"

TODAY = date(2026, 8, 11)
DAYS = 90                                   # 生成する期間
START = TODAY - timedelta(days=DAYS - 1)

rnd = random.Random(20260811)

# 装置種別 -> (工程種別, 装置の代表メーカー, 1時間あたり処理枚数の目安)
EQP_TYPES = {
    "露光":     (["露光"], ["ニコン", "キヤノン", "ASML"], (40, 70)),
    "エッチング": (["エッチング"], ["東京エレクトロン", "日立ハイテク", "Lam"], (30, 55)),
    "成膜":     (["成膜"], ["東京エレクトロン", "アプライド", "国際電気"], (25, 50)),
    "CMP":     (["CMP"], ["荏原製作所", "アプライド"], (20, 40)),
    "洗浄":     (["洗浄"], ["SCREEN", "東京エレクトロン"], (60, 110)),
    "熱処理":    (["熱処理", "拡散"], ["国際電気", "東京エレクトロン"], (15, 30)),
    "イオン注入":  (["イオン注入"], ["アルバック", "アプライド"], (25, 45)),
    "検査":     (["検査", "測定"], ["日立ハイテク", "KLA", "SCREEN"], (50, 90)),
}
# 装置の状態（SEMI E10 に倣う）
STATES = ["稼働", "待機", "段取", "故障", "計画保全", "非稼働"]
ALARM_CODES = {
    "AL-1001": ("チャンバ圧力異常", "重"),
    "AL-1002": ("温度逸脱", "中"),
    "AL-1003": ("ウェハ搬送エラー", "中"),
    "AL-1004": ("ガス流量異常", "重"),
    "AL-2001": ("パーティクル増加", "軽"),
    "AL-2002": ("レシピ不一致", "軽"),
    "AL-3001": ("インタロック作動", "重"),
    "AL-3002": ("消耗品寿命超過", "中"),
}
PART_CATEGORIES = ["消耗品", "交換部品", "治工具"]
WAREHOUSES = ["本館倉庫", "第2倉庫", "クリーンルーム前室"]
MEASURE_ITEMS = {
    "膜厚":      ("nm", 100.0, 3.0),
    "線幅":      ("nm", 45.0, 1.6),
    "オーバーレイ": ("nm", 0.0, 4.0),
    "シート抵抗":  ("Ω/sq", 12.0, 0.6),
}


def _reset(path):
    path.unlink(missing_ok=True)


def _dt(d: date, hour=0, minute=0) -> str:
    return datetime(d.year, d.month, d.day, hour, minute).isoformat(timespec="seconds")


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


# =============================================================================
# fab_master
# =============================================================================

def build_master():
    _reset(MASTER_DB)
    conn = sqlite3.connect(str(MASTER_DB))
    conn.executescript("""
        CREATE TABLE areas (
            area_id    INTEGER PRIMARY KEY,
            area_code  TEXT NOT NULL,
            name       TEXT NOT NULL,
            clean_class TEXT NOT NULL,
            floor      TEXT NOT NULL
        );
        CREATE TABLE equipments (
            equipment_id   INTEGER PRIMARY KEY,
            equipment_code TEXT NOT NULL,
            name           TEXT NOT NULL,
            equipment_type TEXT NOT NULL,
            vendor         TEXT NOT NULL,
            model          TEXT NOT NULL,
            area_id        INTEGER NOT NULL,
            chamber_count  INTEGER NOT NULL,
            install_date   TEXT NOT NULL,
            status         TEXT NOT NULL,
            FOREIGN KEY (area_id) REFERENCES areas(area_id)
        );
        CREATE TABLE processes (
            process_id       INTEGER PRIMARY KEY,
            process_code     TEXT NOT NULL,
            name             TEXT NOT NULL,
            process_type     TEXT NOT NULL,
            layer            TEXT NOT NULL,
            standard_minutes INTEGER NOT NULL
        );
        CREATE TABLE products (
            product_id     INTEGER PRIMARY KEY,
            product_code   TEXT NOT NULL,
            name           TEXT NOT NULL,
            technology_nm  INTEGER NOT NULL,
            wafer_size_mm  INTEGER NOT NULL,
            customer_name  TEXT NOT NULL,
            mask_count     INTEGER NOT NULL,
            status         TEXT NOT NULL
        );
        CREATE TABLE routes (
            route_id   INTEGER PRIMARY KEY,
            product_id INTEGER NOT NULL,
            step_no    INTEGER NOT NULL,
            process_id INTEGER NOT NULL,
            standard_minutes INTEGER NOT NULL,
            FOREIGN KEY (product_id) REFERENCES products(product_id),
            FOREIGN KEY (process_id) REFERENCES processes(process_id)
        );
        CREATE TABLE equipment_capabilities (
            capability_id  INTEGER PRIMARY KEY,
            equipment_id   INTEGER NOT NULL,
            process_id     INTEGER NOT NULL,
            wafers_per_hour INTEGER NOT NULL,
            setup_minutes  INTEGER NOT NULL,
            qualified_flag TEXT NOT NULL,
            qualified_date TEXT,
            FOREIGN KEY (equipment_id) REFERENCES equipments(equipment_id),
            FOREIGN KEY (process_id) REFERENCES processes(process_id)
        );
        CREATE TABLE recipes (
            recipe_id    INTEGER PRIMARY KEY,
            recipe_code  TEXT NOT NULL,
            name         TEXT NOT NULL,
            process_id   INTEGER NOT NULL,
            product_id   INTEGER NOT NULL,
            version      TEXT NOT NULL,
            status       TEXT NOT NULL,
            updated_at   TEXT NOT NULL,
            FOREIGN KEY (process_id) REFERENCES processes(process_id),
            FOREIGN KEY (product_id) REFERENCES products(product_id)
        );
    """)

    areas = [(i + 1, f"BAY{i + 1}", n, c, f)
             for i, (n, c, f) in enumerate([
                 ("リソベイ", "クラス1", "3F"), ("エッチベイ", "クラス10", "3F"),
                 ("成膜ベイ", "クラス10", "3F"), ("CMPベイ", "クラス100", "2F"),
                 ("洗浄ベイ", "クラス10", "2F"), ("熱処理ベイ", "クラス100", "2F"),
                 ("注入ベイ", "クラス100", "1F"), ("検査ベイ", "クラス1", "1F"),
             ])]
    conn.executemany("INSERT INTO areas VALUES(?,?,?,?,?)", areas)

    # --- 装置 ---
    area_of = {"露光": 1, "エッチング": 2, "成膜": 3, "CMP": 4, "洗浄": 5,
               "熱処理": 6, "イオン注入": 7, "検査": 8}
    counts = {"露光": 8, "エッチング": 10, "成膜": 10, "CMP": 5, "洗浄": 8,
              "熱処理": 6, "イオン注入": 5, "検査": 8}
    equipments, eid = [], 0
    for etype, n in counts.items():
        vendors = EQP_TYPES[etype][1]
        for k in range(1, n + 1):
            eid += 1
            vendor = vendors[k % len(vendors)]
            # 古い装置ほど故障しやすくなるよう、導入日をばらす
            install = date(2015, 1, 1) + timedelta(days=rnd.randint(0, 3300))
            status = "稼働中"
            if rnd.random() < 0.05:
                status = "停止中"
            elif rnd.random() < 0.02:
                status = "廃却"
            equipments.append((
                eid, f"EQP-{eid:03d}", f"{etype}装置{k}号機", etype, vendor,
                f"{vendor[:2].upper()}-{rnd.randint(100, 999)}{rnd.choice('ABCX')}",
                area_of[etype], rnd.choice([1, 1, 2, 2, 4]),
                install.isoformat(), status))
    conn.executemany("INSERT INTO equipments VALUES(?,?,?,?,?,?,?,?,?,?)", equipments)

    # --- 工程（レイヤごとに一巡する流れ）---
    layers = ["STI", "Well", "Gate", "Contact", "M1", "M2", "M3", "Passivation"]
    steps_of_layer = [
        ("洗浄", "洗浄", 25), ("成膜", "成膜", 60), ("露光", "露光", 35),
        ("エッチング", "エッチング", 45), ("洗浄", "洗浄", 20),
        ("検査", "検査", 30),
    ]
    processes, pid = [], 0
    for layer in layers:
        for name, ptype, minutes in steps_of_layer:
            pid += 1
            processes.append((pid, f"P{pid:03d}", f"{layer}-{name}", ptype, layer,
                              minutes + rnd.randint(-5, 10)))
        # レイヤによっては注入・CMP・熱処理が入る
        if layer in ("Well", "Gate"):
            pid += 1
            processes.append((pid, f"P{pid:03d}", f"{layer}-イオン注入", "イオン注入",
                              layer, 40))
            pid += 1
            processes.append((pid, f"P{pid:03d}", f"{layer}-熱処理", "熱処理", layer, 90))
        if layer in ("STI", "M1", "M2", "M3"):
            pid += 1
            processes.append((pid, f"P{pid:03d}", f"{layer}-CMP", "CMP", layer, 40))
    conn.executemany("INSERT INTO processes VALUES(?,?,?,?,?,?)", processes)
    n_proc = len(processes)

    # --- 製品 ---
    products = [
        (1, "PRD-A100", "車載マイコン A100", 40, 300, "アルファ自動車", 32, "量産"),
        (2, "PRD-A200", "車載マイコン A200", 28, 300, "アルファ自動車", 36, "量産"),
        (3, "PRD-B300", "産業用センサ B300", 65, 200, "ベータ電機", 24, "量産"),
        (4, "PRD-B310", "産業用センサ B310", 65, 200, "ベータ電機", 26, "量産"),
        (5, "PRD-C400", "民生SoC C400", 28, 300, "ガンマ通信", 40, "量産"),
        (6, "PRD-C450", "民生SoC C450", 22, 300, "ガンマ通信", 44, "立ち上げ"),
        (7, "PRD-D500", "パワーIC D500", 90, 200, "デルタ工業", 18, "量産"),
        (8, "PRD-D520", "パワーIC D520", 90, 200, "デルタ工業", 20, "量産"),
        (9, "PRD-E600", "イメージセンサ E600", 45, 300, "イプシロン光学", 38, "量産"),
        (10, "PRD-F700", "試作ロジック F700", 16, 300, "社内開発", 48, "試作"),
    ]
    conn.executemany("INSERT INTO products VALUES(?,?,?,?,?,?,?,?)", products)

    # --- 製品別の工程フロー ---
    routes, rid = [], 0
    route_of_product: dict[int, list[int]] = {}
    for p in products:
        # 製品ごとに使う工程を少しずつ変える（テクノロジによって工程数が違う）
        take = n_proc if p[3] <= 28 else int(n_proc * (0.7 if p[3] >= 65 else 0.85))
        chosen = sorted(rnd.sample(range(1, n_proc + 1), take))
        route_of_product[p[0]] = chosen
        for step_no, proc_id in enumerate(chosen, start=1):
            rid += 1
            base = processes[proc_id - 1][5]
            routes.append((rid, p[0], step_no, proc_id,
                           max(10, base + rnd.randint(-5, 5))))
    conn.executemany("INSERT INTO routes VALUES(?,?,?,?,?)", routes)

    # --- どの装置でどの工程ができるか ---
    caps, cid = [], 0
    cap_of_process: dict[int, list[int]] = {}
    for proc in processes:
        proc_id, ptype = proc[0], proc[3]
        # 同じ種別の装置のうち、7割程度が「その工程の認定済み」
        same = [e for e in equipments
                if ptype in EQP_TYPES[e[3]][0] and e[9] != "廃却"]
        if not same:
            same = [e for e in equipments if e[3] == "洗浄"]
        picked = rnd.sample(same, max(2, int(len(same) * 0.7)))
        cap_of_process[proc_id] = []
        for e in picked:
            cid += 1
            lo, hi = EQP_TYPES[e[3]][2]
            qualified = "1" if rnd.random() > 0.08 else "0"
            caps.append((cid, e[0], proc_id, rnd.randint(lo, hi),
                         rnd.choice([10, 15, 20, 30, 45]), qualified,
                         (date(2024, 1, 1) + timedelta(days=rnd.randint(0, 800))
                          ).isoformat() if qualified == "1" else None))
            if qualified == "1" and e[9] == "稼働中":
                cap_of_process[proc_id].append(e[0])
    conn.executemany("INSERT INTO equipment_capabilities VALUES(?,?,?,?,?,?,?)", caps)

    # --- レシピ ---
    recipes, rc = [], 0
    for p in products:
        for proc_id in route_of_product[p[0]][::3]:      # 代表的な工程のぶんだけ
            rc += 1
            ver = f"v{rnd.randint(1, 4)}.{rnd.randint(0, 9)}"
            recipes.append((rc, f"RCP-{rc:04d}",
                            f"{p[1]}_{processes[proc_id - 1][1]}_{ver}",
                            proc_id, p[0], ver,
                            "有効" if rnd.random() > 0.1 else "旧版",
                            _dt(TODAY - timedelta(days=rnd.randint(1, 400)),
                                rnd.randint(8, 20))))
    conn.executemany("INSERT INTO recipes VALUES(?,?,?,?,?,?,?,?)", recipes)

    conn.commit()
    conn.close()
    print(f"OK  fab_master.db  装置{len(equipments)} 工程{len(processes)} "
          f"製品{len(products)} フロー{len(routes)} 対応表{len(caps)} レシピ{len(recipes)}")
    return {"equipments": equipments, "processes": processes, "products": products,
            "routes": routes, "cap_of_process": cap_of_process,
            "route_of_product": route_of_product, "recipes": recipes}


# =============================================================================
# fab_production
# =============================================================================

def build_production(m):
    _reset(PROD_DB)
    conn = sqlite3.connect(str(PROD_DB))
    conn.executescript("""
        CREATE TABLE lots (
            lot_id      INTEGER PRIMARY KEY,
            lot_no      TEXT NOT NULL,
            product_id  INTEGER NOT NULL,
            wafer_count INTEGER NOT NULL,
            start_date  TEXT NOT NULL,
            due_date    TEXT NOT NULL,
            priority    TEXT NOT NULL,
            status      TEXT NOT NULL,
            current_step_no INTEGER,
            current_process_id INTEGER
        );
        CREATE TABLE wip (
            wip_id      INTEGER PRIMARY KEY,
            lot_id      INTEGER NOT NULL,
            process_id  INTEGER NOT NULL,
            step_no     INTEGER NOT NULL,
            wafer_count INTEGER NOT NULL,
            queued_at   TEXT NOT NULL,
            waiting_hours REAL NOT NULL,
            hold_flag   TEXT NOT NULL,
            FOREIGN KEY (lot_id) REFERENCES lots(lot_id)
        );
        CREATE TABLE process_results (
            result_id    INTEGER PRIMARY KEY,
            lot_id       INTEGER NOT NULL,
            process_id   INTEGER NOT NULL,
            equipment_id INTEGER NOT NULL,
            recipe_id    INTEGER,
            step_no      INTEGER NOT NULL,
            start_at     TEXT NOT NULL,
            end_at       TEXT NOT NULL,
            process_minutes INTEGER NOT NULL,
            input_wafers  INTEGER NOT NULL,
            output_wafers INTEGER NOT NULL,
            scrap_wafers  INTEGER NOT NULL,
            operator      TEXT NOT NULL,
            result_flag   TEXT NOT NULL,
            FOREIGN KEY (lot_id) REFERENCES lots(lot_id)
        );
        CREATE TABLE lot_holds (
            hold_id     INTEGER PRIMARY KEY,
            lot_id      INTEGER NOT NULL,
            process_id  INTEGER NOT NULL,
            reason      TEXT NOT NULL,
            hold_type   TEXT NOT NULL,
            held_at     TEXT NOT NULL,
            released_at TEXT,
            hold_hours  REAL,
            FOREIGN KEY (lot_id) REFERENCES lots(lot_id)
        );
        CREATE TABLE shipments (
            shipment_id  INTEGER PRIMARY KEY,
            lot_id       INTEGER NOT NULL,
            shipped_at   TEXT NOT NULL,
            wafer_count  INTEGER NOT NULL,
            customer_name TEXT NOT NULL,
            invoice_no   TEXT NOT NULL,
            FOREIGN KEY (lot_id) REFERENCES lots(lot_id)
        );
    """)

    operators = [f"{s}{n}" for s in ("佐藤", "鈴木", "高橋", "田中", "伊藤",
                                     "渡辺", "山本", "中村")
                 for n in ("班", "")][:12]
    products = m["products"]
    route_of = m["route_of_product"]
    cap_of = m["cap_of_process"]
    proc_by_id = {p[0]: p for p in m["processes"]}
    recipes_by = {}
    for r in m["recipes"]:
        recipes_by.setdefault((r[3], r[4]), []).append(r[0])

    lots, wip, results, holds, shipments = [], [], [], [], []
    rid = hid = sid = wid = 0
    lot_id = 0
    # 1日あたり6〜10ロット投入
    for day_offset in range(DAYS):
        d = START + timedelta(days=day_offset)
        for _ in range(rnd.randint(6, 10)):
            lot_id += 1
            p = rnd.choices(products, weights=[18, 15, 12, 10, 14, 5, 9, 8, 7, 2])[0]
            wafers = rnd.choice([25, 25, 25, 24, 12, 25])
            prio = rnd.choices(["通常", "特急", "最優先"], weights=[80, 15, 5])[0]
            route = route_of[p[0]]
            due = d + timedelta(days=len(route) // 4 + rnd.randint(3, 10))
            lots.append([lot_id, f"LOT{d:%y%m%d}-{lot_id:04d}", p[0], wafers,
                         d.isoformat(), due.isoformat(), prio, "仕掛", None, None])

    # 各ロットを流す。投入が新しいほど途中で止まっている。
    for lot in lots:
        lot_id, _no, product_id, wafers, start_s, _due, prio, *_ = lot
        route = route_of[product_id]
        start_d = date.fromisoformat(start_s)
        elapsed_days = (TODAY - start_d).days
        # 1日に進める工程数（特急ほど速い）
        speed = {"通常": 2.2, "特急": 3.2, "最優先": 4.0}[prio]
        done_steps = int(elapsed_days * speed * rnd.uniform(0.75, 1.15))
        done_steps = max(0, min(done_steps, len(route)))

        cur = datetime(start_d.year, start_d.month, start_d.day,
                       rnd.randint(7, 10), rnd.choice([0, 15, 30, 45]))
        alive = wafers
        for step_no in range(1, done_steps + 1):
            proc_id = route[step_no - 1]
            cands = cap_of.get(proc_id) or []
            if not cands:
                continue
            eqp = rnd.choice(cands)
            std = proc_by_id[proc_id][5]
            # 待ち時間 → 処理時間
            cur += timedelta(minutes=rnd.randint(10, 240))
            minutes = max(5, int(std * rnd.uniform(0.8, 1.5)))
            end = cur + timedelta(minutes=minutes)
            scrap = 0
            if rnd.random() < 0.05:
                scrap = rnd.randint(1, 2)
            out = max(1, alive - scrap)
            flag = "正常"
            if rnd.random() < 0.03:
                flag = "再処理"
            elif scrap:
                flag = "一部廃棄"
            rid += 1
            rcps = recipes_by.get((proc_id, product_id))
            results.append((rid, lot_id, proc_id, eqp,
                            rnd.choice(rcps) if rcps else None, step_no,
                            _iso(cur), _iso(end), minutes, alive, out, scrap,
                            rnd.choice(operators), flag))
            alive = out
            cur = end
            # まれに保留が入る
            if rnd.random() < 0.012:
                hid += 1
                reason = rnd.choice(["測定値NG", "装置故障待ち", "レシピ確認",
                                     "顧客指示", "部材待ち"])
                htype = "品質" if reason == "測定値NG" else "工程"
                rel = cur + timedelta(hours=rnd.uniform(2, 72))
                released = rel <= datetime(TODAY.year, TODAY.month, TODAY.day)
                holds.append((hid, lot_id, proc_id, reason, htype, _iso(cur),
                              _iso(rel) if released else None,
                              round((rel - cur).total_seconds() / 3600, 1)
                              if released else None))
                if released:
                    cur = rel

        if done_steps >= len(route):
            lot[7] = "完了"
            lot[8], lot[9] = len(route), route[-1]
            sid += 1
            ship = min(cur + timedelta(days=rnd.randint(1, 4)),
                       datetime(TODAY.year, TODAY.month, TODAY.day, 18))
            cust = next(p[5] for p in products if p[0] == product_id)
            shipments.append((sid, lot_id, _iso(ship), alive, cust,
                              f"INV-{TODAY:%Y}-{sid:04d}"))
        else:
            # まだ流れている → 次の工程で仕掛として待っている
            nxt = done_steps + 1
            lot[7] = "仕掛"
            lot[8] = nxt
            lot[9] = route[nxt - 1]
            if rnd.random() < 0.02:
                lot[7] = "保留"
            elif rnd.random() < 0.006:
                lot[7] = "廃棄"
            if lot[7] in ("仕掛", "保留"):
                wid += 1
                waited = (datetime(TODAY.year, TODAY.month, TODAY.day, 9) - cur)
                hours = max(0.2, round(waited.total_seconds() / 3600, 1))
                wip.append((wid, lot_id, route[nxt - 1], nxt, alive, _iso(cur),
                            hours, "1" if lot[7] == "保留" else "0"))

    conn.executemany("INSERT INTO lots VALUES(?,?,?,?,?,?,?,?,?,?)", lots)
    conn.executemany("INSERT INTO wip VALUES(?,?,?,?,?,?,?,?)", wip)
    conn.executemany("INSERT INTO process_results VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     results)
    conn.executemany("INSERT INTO lot_holds VALUES(?,?,?,?,?,?,?,?)", holds)
    conn.executemany("INSERT INTO shipments VALUES(?,?,?,?,?,?)", shipments)
    conn.commit()
    conn.close()
    print(f"OK  fab_production.db  ロット{len(lots)} 仕掛{len(wip)} "
          f"工程実績{len(results)} 保留{len(holds)} 出荷{len(shipments)}")
    return {"lots": lots, "results": results}


# =============================================================================
# fab_equipment
# =============================================================================

def build_equipment(m, prod):
    _reset(EQP_DB)
    conn = sqlite3.connect(str(EQP_DB))
    conn.executescript("""
        CREATE TABLE equipment_states (
            state_id     INTEGER PRIMARY KEY,
            equipment_id INTEGER NOT NULL,
            state        TEXT NOT NULL,
            started_at   TEXT NOT NULL,
            ended_at     TEXT NOT NULL,
            minutes      INTEGER NOT NULL,
            reason       TEXT,
            work_date    TEXT NOT NULL
        );
        CREATE TABLE equipment_daily (
            daily_id     INTEGER PRIMARY KEY,
            equipment_id INTEGER NOT NULL,
            work_date    TEXT NOT NULL,
            run_minutes  INTEGER NOT NULL,
            idle_minutes INTEGER NOT NULL,
            setup_minutes INTEGER NOT NULL,
            down_minutes INTEGER NOT NULL,
            pm_minutes   INTEGER NOT NULL,
            wafer_count  INTEGER NOT NULL,
            availability REAL NOT NULL,
            utilization  REAL NOT NULL
        );
        CREATE TABLE alarms (
            alarm_id     INTEGER PRIMARY KEY,
            equipment_id INTEGER NOT NULL,
            alarm_code   TEXT NOT NULL,
            message      TEXT NOT NULL,
            severity     TEXT NOT NULL,
            occurred_at  TEXT NOT NULL,
            recovered_at TEXT,
            down_minutes INTEGER,
            chamber      TEXT
        );
        CREATE TABLE maintenances (
            maintenance_id INTEGER PRIMARY KEY,
            equipment_id   INTEGER NOT NULL,
            maintenance_type TEXT NOT NULL,
            planned_date   TEXT,
            done_date      TEXT,
            down_minutes   INTEGER,
            cost_yen       INTEGER,
            technician     TEXT NOT NULL,
            note           TEXT,
            status         TEXT NOT NULL
        );
        CREATE TABLE equipment_meters (
            meter_id      INTEGER PRIMARY KEY,
            equipment_id  INTEGER NOT NULL,
            measured_at   TEXT NOT NULL,
            chamber       TEXT NOT NULL,
            temperature_c REAL NOT NULL,
            pressure_pa   REAL NOT NULL,
            rf_hours      REAL NOT NULL,
            particle_count INTEGER NOT NULL
        );
    """)

    eqps = [e for e in m["equipments"] if e[9] != "廃却"]
    # 工程実績から「その装置がその日に何分動いたか」を積み上げる
    run_by = {}
    wafer_by = {}
    for r in prod["results"]:
        d = r[6][:10]
        run_by[(r[3], d)] = run_by.get((r[3], d), 0) + r[8]
        wafer_by[(r[3], d)] = wafer_by.get((r[3], d), 0) + r[9]

    states, daily, alarms, maints, meters = [], [], [], [], []
    st = dl = al = mt = me = 0
    techs = ["保全1課 大野", "保全1課 川口", "保全2課 森", "保全2課 岩本",
             "ベンダ常駐 佐々木"]

    for e in eqps:
        eqp_id, _code, _name, etype, _vendor, _model, _area, chambers, install, status = e
        # 古い装置ほど故障が多い
        age_years = (TODAY - date.fromisoformat(install)).days / 365
        fail_rate = min(0.22, 0.03 + age_years * 0.018)
        rf_hours = round(rnd.uniform(200, 9000), 1)

        for day_offset in range(DAYS):
            d = START + timedelta(days=day_offset)
            ds = d.isoformat()
            run = run_by.get((eqp_id, ds), 0)
            wafers = wafer_by.get((eqp_id, ds), 0)
            if status == "停止中":
                run, wafers = 0, 0
            run = min(run, 1200)

            setup = int(run * rnd.uniform(0.05, 0.15)) if run else 0
            down = 0
            pm = 0
            # 計画保全は装置ごとに月1回程度
            if rnd.random() < 0.035:
                pm = rnd.choice([180, 240, 300, 480])
                mt += 1
                maints.append((mt, eqp_id, "定期保全", ds, ds, pm,
                               rnd.randint(50000, 400000), rnd.choice(techs),
                               "定期点検・消耗品交換", "完了"))
            # 故障
            if rnd.random() < fail_rate:
                down = rnd.choice([30, 45, 60, 90, 120, 180, 240, 360])
                code = rnd.choice(list(ALARM_CODES))
                msg, sev = ALARM_CODES[code]
                occ = datetime(d.year, d.month, d.day, rnd.randint(0, 23),
                               rnd.choice([0, 10, 20, 30, 40, 50]))
                al += 1
                alarms.append((al, eqp_id, code, msg, sev, _iso(occ),
                               _iso(occ + timedelta(minutes=down)), down,
                               f"CH{rnd.randint(1, chambers)}"))
                if sev == "重":
                    mt += 1
                    maints.append((mt, eqp_id, "事後保全", None, ds, down,
                                   rnd.randint(80000, 900000), rnd.choice(techs),
                                   f"{msg}への対応", "完了"))
            # 軽微なアラーム（停止を伴わない）
            for _ in range(rnd.randint(0, 3)):
                code = rnd.choice(["AL-2001", "AL-2002", "AL-3002"])
                msg, sev = ALARM_CODES[code]
                occ = datetime(d.year, d.month, d.day, rnd.randint(0, 23),
                               rnd.randint(0, 59))
                al += 1
                alarms.append((al, eqp_id, code, msg, sev, _iso(occ),
                               _iso(occ + timedelta(minutes=rnd.randint(1, 15))),
                               0, f"CH{rnd.randint(1, chambers)}"))

            busy = run + setup + down + pm
            idle = max(0, 1440 - busy)
            # 状態ログ（1日を数区間に分ける）
            t = datetime(d.year, d.month, d.day, 0, 0)
            for state, minutes, reason in (
                    ("計画保全", pm, "定期点検" if pm else None),
                    ("段取", setup, "レシピ切替" if setup else None),
                    ("稼働", run, None),
                    ("故障", down, "アラーム対応" if down else None),
                    ("待機", idle, "ロット待ち" if idle else None)):
                if minutes <= 0:
                    continue
                st += 1
                states.append((st, eqp_id, state, _iso(t),
                               _iso(t + timedelta(minutes=minutes)), minutes,
                               reason, ds))
                t += timedelta(minutes=minutes)

            # 稼働率・可用率
            avail = round((1440 - down - pm) / 1440, 4)
            util = round(run / 1440, 4)
            dl += 1
            daily.append((dl, eqp_id, ds, run, idle, setup, down, pm, wafers,
                          avail, util))

            # 計測値（1日2回）
            for hour in (9, 21):
                me += 1
                rf_hours += run / 60 * 0.9
                base_t = {"露光": 23.0, "エッチング": 60.0, "成膜": 380.0,
                          "CMP": 25.0, "洗浄": 45.0, "熱処理": 850.0,
                          "イオン注入": 30.0, "検査": 22.0}[etype]
                meters.append((me, eqp_id, _dt(d, hour), f"CH{rnd.randint(1, chambers)}",
                               round(base_t + rnd.gauss(0, base_t * 0.02), 2),
                               round(abs(rnd.gauss(1.5, 0.4)), 3),
                               round(rf_hours, 1),
                               max(0, int(rnd.gauss(12, 9) + (rf_hours / 1500)))))

        # 未実施の計画保全（これから）
        if rnd.random() < 0.5:
            mt += 1
            plan = TODAY + timedelta(days=rnd.randint(1, 30))
            maints.append((mt, eqp_id, "定期保全", plan.isoformat(), None, None,
                           None, rnd.choice(techs), "次回定期点検", "予定"))

    conn.executemany("INSERT INTO equipment_states VALUES(?,?,?,?,?,?,?,?)", states)
    conn.executemany("INSERT INTO equipment_daily VALUES(?,?,?,?,?,?,?,?,?,?,?)", daily)
    conn.executemany("INSERT INTO alarms VALUES(?,?,?,?,?,?,?,?,?)", alarms)
    conn.executemany("INSERT INTO maintenances VALUES(?,?,?,?,?,?,?,?,?,?)", maints)
    conn.executemany("INSERT INTO equipment_meters VALUES(?,?,?,?,?,?,?,?)", meters)
    conn.commit()
    conn.close()
    print(f"OK  fab_equipment.db  状態ログ{len(states)} 日次{len(daily)} "
          f"アラーム{len(alarms)} 保全{len(maints)} 計測{len(meters)}")
    return {"maintenances": maints}


# =============================================================================
# fab_parts
# =============================================================================

PART_NAMES = {
    "露光": ["レチクルステージベアリング", "光源ランプ", "アライメントセンサ",
             "ウェハチャック", "レンズ保護窓"],
    "エッチング": ["フォーカスリング", "電極プレート", "石英チャンバライナ",
                   "Oリングセット", "ガスノズル"],
    "成膜": ["シャワーヘッド", "サセプタ", "ヒータエレメント", "ターゲット材",
             "ベローズ"],
    "CMP": ["研磨パッド", "リテーナリング", "コンディショナディスク",
            "スラリー供給ポンプ", "ドレッサ"],
    "洗浄": ["薬液フィルタ", "スピンチャック", "ノズルヘッド", "ドレンバルブ",
             "純水配管ジョイント"],
    "熱処理": ["石英ボート", "ヒータコイル", "熱電対", "反応管", "シールキャップ"],
    "イオン注入": ["イオン源フィラメント", "アークチャンバ", "サプレッション電極",
                   "ビームライン絶縁碍子", "ファラデーカップ"],
    "検査": ["対物レンズ", "CCDカメラ", "ステージモータ", "照明ユニット",
             "除振マウント"],
}
SUPPLIERS = ["東京パーツ商会", "関西精密工業", "日本真空部品", "アドバンス電材",
             "グローバルサプライ", "九州テクノ"]


def build_parts(m, eq):
    _reset(PARTS_DB)
    conn = sqlite3.connect(str(PARTS_DB))
    conn.executescript("""
        CREATE TABLE parts (
            part_id       INTEGER PRIMARY KEY,
            part_no       TEXT NOT NULL,
            name          TEXT NOT NULL,
            category      TEXT NOT NULL,
            equipment_type TEXT NOT NULL,
            unit          TEXT NOT NULL,
            unit_price_yen INTEGER NOT NULL,
            lead_time_days INTEGER NOT NULL,
            supplier_name TEXT NOT NULL,
            life_hours    INTEGER,
            safety_stock  INTEGER NOT NULL,
            status        TEXT NOT NULL
        );
        CREATE TABLE equipment_parts (
            equipment_part_id INTEGER PRIMARY KEY,
            equipment_id      INTEGER NOT NULL,
            part_id           INTEGER NOT NULL,
            quantity_per_unit INTEGER NOT NULL,
            standard_life_hours INTEGER,
            FOREIGN KEY (part_id) REFERENCES parts(part_id)
        );
        CREATE TABLE part_stocks (
            stock_id     INTEGER PRIMARY KEY,
            part_id      INTEGER NOT NULL,
            warehouse    TEXT NOT NULL,
            location_code TEXT NOT NULL,
            quantity     INTEGER NOT NULL,
            reserved_quantity INTEGER NOT NULL,
            updated_at   TEXT NOT NULL,
            FOREIGN KEY (part_id) REFERENCES parts(part_id)
        );
        CREATE TABLE part_movements (
            movement_id  INTEGER PRIMARY KEY,
            part_id      INTEGER NOT NULL,
            warehouse    TEXT NOT NULL,
            movement_type TEXT NOT NULL,
            quantity     INTEGER NOT NULL,
            moved_at     TEXT NOT NULL,
            equipment_id INTEGER,
            reference_no TEXT NOT NULL,
            operator     TEXT NOT NULL,
            FOREIGN KEY (part_id) REFERENCES parts(part_id)
        );
        CREATE TABLE part_replacements (
            replacement_id INTEGER PRIMARY KEY,
            equipment_id   INTEGER NOT NULL,
            part_id        INTEGER NOT NULL,
            maintenance_id INTEGER,
            replaced_at    TEXT NOT NULL,
            used_hours     REAL NOT NULL,
            reason         TEXT NOT NULL,
            technician     TEXT NOT NULL,
            FOREIGN KEY (part_id) REFERENCES parts(part_id)
        );
        CREATE TABLE part_orders (
            part_order_id INTEGER PRIMARY KEY,
            part_id       INTEGER NOT NULL,
            supplier_name TEXT NOT NULL,
            ordered_at    TEXT NOT NULL,
            quantity      INTEGER NOT NULL,
            unit_price_yen INTEGER NOT NULL,
            expected_at   TEXT NOT NULL,
            arrived_at    TEXT,
            status        TEXT NOT NULL,
            FOREIGN KEY (part_id) REFERENCES parts(part_id)
        );
    """)

    parts, pid = [], 0
    parts_of_type: dict[str, list[int]] = {}
    for etype, names in PART_NAMES.items():
        parts_of_type[etype] = []
        for nm in names:
            for grade in ("標準", "高耐久", "廉価"):
                pid += 1
                cat = ("消耗品" if any(k in nm for k in ("パッド", "フィルタ",
                                                        "ランプ", "ターゲット",
                                                        "フィラメント", "リング"))
                       else rnd.choice(PART_CATEGORIES))
                life = rnd.choice([200, 400, 600, 1000, 2000, 4000, None])
                parts.append((pid, f"PN-{pid:04d}", f"{nm}（{grade}）", cat, etype,
                              rnd.choice(["個", "個", "式", "セット", "本"]),
                              rnd.choice([8000, 15000, 32000, 78000, 150000,
                                          320000, 850000]),
                              rnd.choice([3, 7, 14, 21, 30, 60, 90]),
                              rnd.choice(SUPPLIERS), life,
                              rnd.choice([1, 2, 3, 5, 10]),
                              "有効" if rnd.random() > 0.06 else "廃番"))
                parts_of_type[etype].append(pid)
    conn.executemany("INSERT INTO parts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", parts)

    eqps = [e for e in m["equipments"] if e[9] != "廃却"]
    part_by_id = {p[0]: p for p in parts}

    # 装置ごとの適合部品
    eparts, ep = [], 0
    parts_of_equipment: dict[int, list[int]] = {}
    for e in eqps:
        cands = parts_of_type[e[3]]
        picked = rnd.sample(cands, min(len(cands), rnd.randint(6, 10)))
        parts_of_equipment[e[0]] = picked
        for p in picked:
            ep += 1
            eparts.append((ep, e[0], p, rnd.choice([1, 1, 1, 2, 4]),
                           part_by_id[p][9]))
    conn.executemany("INSERT INTO equipment_parts VALUES(?,?,?,?,?)", eparts)

    # 在庫（倉庫ごと）
    stocks, sid = [], 0
    for p in parts:
        for wh in rnd.sample(WAREHOUSES, rnd.randint(1, 3)):
            sid += 1
            # 安全在庫を割っているものを2割ほど混ぜる
            base = p[10]
            qty = (rnd.randint(0, max(0, base - 1)) if rnd.random() < 0.2
                   else rnd.randint(base, base * 6))
            stocks.append((sid, p[0], wh, f"{wh[:2]}-{rnd.randint(1, 20):02d}-"
                           f"{rnd.randint(1, 9)}", qty,
                           rnd.randint(0, max(0, qty // 3)),
                           _dt(TODAY - timedelta(days=rnd.randint(0, 20)),
                               rnd.randint(8, 19))))
    conn.executemany("INSERT INTO part_stocks VALUES(?,?,?,?,?,?,?)", stocks)

    # 交換履歴 → 出庫 → 発注 を連動させる
    moves, reps, orders = [], [], []
    mv = rp = od = 0
    techs = ["保全1課 大野", "保全1課 川口", "保全2課 森", "保全2課 岩本",
             "ベンダ常駐 佐々木"]
    maint_by_eqp: dict[int, list] = {}
    for mrow in eq["maintenances"]:
        maint_by_eqp.setdefault(mrow[1], []).append(mrow)

    for e in eqps:
        for _ in range(rnd.randint(2, 8)):
            part_id = rnd.choice(parts_of_equipment[e[0]])
            when = datetime(START.year, START.month, START.day) + timedelta(
                days=rnd.randint(0, DAYS - 1), hours=rnd.randint(8, 20))
            ms = maint_by_eqp.get(e[0], [])
            mid = rnd.choice(ms)[0] if ms and rnd.random() < 0.6 else None
            life = part_by_id[part_id][9] or 1000
            rp += 1
            reps.append((rp, e[0], part_id, mid, _iso(when),
                         round(life * rnd.uniform(0.5, 1.3), 1),
                         rnd.choice(["寿命到達", "破損", "予防交換", "不具合対応"]),
                         rnd.choice(techs)))
            mv += 1
            moves.append((mv, part_id, rnd.choice(WAREHOUSES), "出庫",
                          rnd.choice([1, 1, 2]), _iso(when), e[0],
                          f"WO-{rp:05d}", rnd.choice(techs)))

    # 入庫と発注
    for _ in range(900):
        p = rnd.choice(parts)
        od += 1
        ordered = datetime(START.year, START.month, START.day) + timedelta(
            days=rnd.randint(0, DAYS - 1), hours=rnd.randint(9, 17))
        expected = ordered + timedelta(days=p[7])
        arrived = None
        stat = "手配中"
        if expected <= datetime(TODAY.year, TODAY.month, TODAY.day):
            # 遅延することもある
            delay = rnd.choices([0, 1, 3, 7], weights=[70, 15, 10, 5])[0]
            arr = expected + timedelta(days=delay)
            if arr <= datetime(TODAY.year, TODAY.month, TODAY.day):
                arrived = arr
                stat = "入庫済"
                mv += 1
                moves.append((mv, p[0], rnd.choice(WAREHOUSES), "入庫",
                              rnd.randint(1, 10), _iso(arr), None,
                              f"PO-{od:05d}", "資材課 林"))
            else:
                stat = "遅延"
        orders.append((od, p[0], p[8], _iso(ordered), rnd.randint(1, 10), p[6],
                       _iso(expected), _iso(arrived) if arrived else None, stat))

    # 返却・廃棄も少し
    for _ in range(200):
        p = rnd.choice(parts)
        mv += 1
        when = datetime(START.year, START.month, START.day) + timedelta(
            days=rnd.randint(0, DAYS - 1), hours=rnd.randint(9, 18))
        moves.append((mv, p[0], rnd.choice(WAREHOUSES),
                      rnd.choice(["返却", "廃棄"]), rnd.randint(1, 3), _iso(when),
                      None, f"RT-{mv:05d}", rnd.choice(techs)))

    moves.sort(key=lambda r: r[5])
    conn.executemany("INSERT INTO part_movements VALUES(?,?,?,?,?,?,?,?,?)", moves)
    conn.executemany("INSERT INTO part_replacements VALUES(?,?,?,?,?,?,?,?)", reps)
    conn.executemany("INSERT INTO part_orders VALUES(?,?,?,?,?,?,?,?,?)", orders)
    conn.commit()
    conn.close()
    print(f"OK  fab_parts.db  部品{len(parts)} 適合{len(eparts)} 在庫{len(stocks)} "
          f"入出庫{len(moves)} 交換{len(reps)} 発注{len(orders)}")


# =============================================================================
# fab_quality
# =============================================================================

def build_quality(m, prod):
    _reset(QUAL_DB)
    conn = sqlite3.connect(str(QUAL_DB))
    conn.executescript("""
        CREATE TABLE measurements (
            measurement_id INTEGER PRIMARY KEY,
            lot_id       INTEGER NOT NULL,
            process_id   INTEGER NOT NULL,
            equipment_id INTEGER NOT NULL,
            measured_at  TEXT NOT NULL,
            item         TEXT NOT NULL,
            unit         TEXT NOT NULL,
            value        REAL NOT NULL,
            target_value REAL NOT NULL,
            lower_limit  REAL NOT NULL,
            upper_limit  REAL NOT NULL,
            judgement    TEXT NOT NULL,
            wafer_no     INTEGER NOT NULL
        );
        CREATE TABLE defects (
            defect_id    INTEGER PRIMARY KEY,
            lot_id       INTEGER NOT NULL,
            process_id   INTEGER NOT NULL,
            equipment_id INTEGER NOT NULL,
            inspected_at TEXT NOT NULL,
            defect_type  TEXT NOT NULL,
            defect_count INTEGER NOT NULL,
            defect_density REAL NOT NULL,
            wafer_no     INTEGER NOT NULL
        );
        CREATE TABLE yields (
            yield_id    INTEGER PRIMARY KEY,
            lot_id      INTEGER NOT NULL,
            tested_at   TEXT NOT NULL,
            tested_dies INTEGER NOT NULL,
            good_dies   INTEGER NOT NULL,
            yield_rate  REAL NOT NULL,
            fail_bin_top TEXT NOT NULL,
            grade       TEXT NOT NULL
        );
    """)

    measurements, defects, yields = [], [], []
    mid = did = yid = 0
    proc_by_id = {p[0]: p for p in m["processes"]}
    defect_types = ["パーティクル", "スクラッチ", "残渣", "パターン欠け",
                    "ボイド", "異物付着"]

    # 検査工程の実績に対して測定値を作る
    for r in prod["results"]:
        _rid, lot_id, proc_id, eqp_id, _rcp, _step, _start, end, *_ = r
        ptype = proc_by_id[proc_id][3]
        if ptype in ("検査", "測定"):
            for _ in range(rnd.randint(2, 4)):
                item = rnd.choice(list(MEASURE_ITEMS))
                unit, target, sigma = MEASURE_ITEMS[item]
                # 装置ごとの癖（偏り）を入れる
                bias = ((eqp_id % 7) - 3) * sigma * 0.25
                value = rnd.gauss(target + bias, sigma)
                lo, hi = target - sigma * 3, target + sigma * 3
                mid += 1
                measurements.append((mid, lot_id, proc_id, eqp_id, end, item, unit,
                                     round(value, 3), target, round(lo, 3),
                                     round(hi, 3),
                                     "OK" if lo <= value <= hi else "NG",
                                     rnd.choice([1, 5, 13, 25])))
            if rnd.random() < 0.6:
                did += 1
                cnt = max(0, int(rnd.gauss(18, 14)))
                defects.append((did, lot_id, proc_id, eqp_id, end,
                                rnd.choice(defect_types), cnt,
                                round(cnt / 706.0, 4), rnd.choice([1, 13, 25])))

    # 完了ロットの歩留まり
    lots_done = [lot for lot in prod["lots"] if lot[7] == "完了"]
    ng_by_lot: dict[int, int] = {}
    for mrow in measurements:
        if mrow[11] == "NG":
            ng_by_lot[mrow[1]] = ng_by_lot.get(mrow[1], 0) + 1
    for lot in lots_done:
        yid += 1
        dies = lot[3] * rnd.randint(380, 720)
        # 測定NGが多いロットほど歩留まりが落ちる
        penalty = min(0.25, ng_by_lot.get(lot[0], 0) * 0.012)
        rate = max(0.35, min(0.995, rnd.gauss(0.90, 0.05) - penalty))
        good = int(dies * rate)
        yields.append((yid, lot[0],
                       _dt(date.fromisoformat(lot[4]) + timedelta(days=30),
                           rnd.randint(9, 18)),
                       dies, good, round(rate, 4),
                       rnd.choice(["BIN2 機能不良", "BIN3 リーク", "BIN4 速度不足",
                                   "BIN5 オープン", "BIN7 外観"]),
                       "A" if rate >= 0.92 else ("B" if rate >= 0.85 else "C")))

    conn.executemany("INSERT INTO measurements VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     measurements)
    conn.executemany("INSERT INTO defects VALUES(?,?,?,?,?,?,?,?,?)", defects)
    conn.executemany("INSERT INTO yields VALUES(?,?,?,?,?,?,?,?)", yields)
    conn.commit()
    conn.close()
    print(f"OK  fab_quality.db  測定{len(measurements)} 欠陥{len(defects)} "
          f"歩留まり{len(yields)}")


# =============================================================================
# メタ情報（カタログ）
# =============================================================================

META = {}


def _build_meta():
    META[MASTER_DB] = {
        "title": "工場マスタDB",
        "description": "装置・工程・製品・工程フロー・装置能力・レシピ。"
                       "他の fab_* DBはすべてこのマスタを参照する。",
        "caveats": [
            "廃却された装置（equipments.status='廃却'）は稼働分析から除くこと。",
            "工程はレイヤ（STI→Well→Gate→Contact→M1→M2→M3→Passivation）の順に進む。",
            "「どの装置でどの工程ができるか」は equipment_capabilities にしかない。"
            "装置種別だけで判断すると、認定されていない装置まで含めてしまう。",
        ],
        "tables": {
            "areas": {"description": "クリーンルームのベイ（区画）。1行 = 1ベイ。"},
            "equipments": {
                "description": "装置マスタ。1行 = 1台。号機単位で管理する。",
                "columns": {
                    "equipment_type": {
                        "description": "装置種別。処理できる工程の種類を決める",
                        "values": {"露光": "ステッパ/スキャナ",
                                   "エッチング": "ドライ・ウェットエッチング",
                                   "成膜": "CVD/PVD/ALD", "CMP": "化学機械研磨",
                                   "洗浄": "枚葉・バッチ洗浄", "熱処理": "拡散炉・RTP",
                                   "イオン注入": "インプランタ", "検査": "測長・欠陥検査"}},
                    "status": {"description": "装置の登録状態",
                               "values": {"稼働中": "使用中", "停止中": "長期停止",
                                          "廃却": "撤去済み。分析対象から外す"}},
                    "chamber_count": {"description": "チャンバ数。複数室の装置は並列処理できる"},
                    "install_date": {"description": "導入日。古い装置ほど故障が増える傾向"},
                },
                "glossary": {
                    "現役装置": {"description": "撤去されていない装置。稼働率や故障の分析は必ずこれで絞る。",
                                 "sql": "equipments.status <> '廃却'"},
                    "装置年齢": {"description": "導入からの経過年数。故障率との関係を見るときに使う。",
                                 "sql": "(julianday('now') - julianday(equipments.install_date)) / 365.0"},
                }},
            "processes": {
                "description": "工程マスタ。1行 = 1工程。製品共通の工程定義。",
                "columns": {
                    "layer": {"description": "配線層・工程層。STI/Well/Gate/Contact/M1〜M3/Passivation"},
                    "standard_minutes": {"description": "標準処理時間（分）。実績との差が段取り・待ちの目安"},
                }},
            "products": {
                "description": "製品マスタ。1行 = 1品種。",
                "columns": {
                    "technology_nm": {"description": "テクノロジノード（nm）。小さいほど微細で工程数が多い"},
                    "status": {"description": "量産状態",
                               "values": {"量産": "通常生産", "立ち上げ": "歩留まり改善中",
                                          "試作": "開発品。歩留まりは低くて当然"}},
                }},
            "routes": {
                "description": "製品別の工程フロー。1行 = 製品の何番目にどの工程を通すか。"
                               "step_no の順に流れる。",
                "glossary": {
                    "総工程数": {"description": "その製品が通る工程の数。",
                                 "sql": "COUNT(*) OVER (PARTITION BY routes.product_id)"},
                }},
            "equipment_capabilities": {
                "description": "装置と工程の対応表。1行 = 「この装置でこの工程が処理できる」。"
                               "ここに無い組み合わせは流せない。",
                "columns": {
                    "qualified_flag": {"description": "認定済みかどうか",
                                       "values": {"1": "認定済み。実際に流せる",
                                                  "0": "未認定。流してはいけない"}},
                    "wafers_per_hour": {"description": "1時間あたりの処理枚数（能力）"},
                    "setup_minutes": {"description": "段取り替えに要する時間（分）"},
                },
                "glossary": {
                    "処理可能な装置": {"description": "その工程を実際に流せる装置。認定済みのものだけ。",
                                       "sql": "equipment_capabilities.qualified_flag = '1'"},
                }},
            "recipes": {
                "description": "レシピマスタ。1行 = 製品×工程のレシピ。",
                "columns": {"status": {"description": "有効／旧版",
                                       "values": {"有効": "現行", "旧版": "使用しない"}}}},
        },
        "relationships": [
            {"from": "equipments.area_id", "to": "areas.area_id", "cardinality": "N:1"},
            {"from": "routes.product_id", "to": "products.product_id", "cardinality": "N:1"},
            {"from": "routes.process_id", "to": "processes.process_id", "cardinality": "N:1"},
            {"from": "equipment_capabilities.equipment_id", "to": "equipments.equipment_id",
             "cardinality": "N:1"},
            {"from": "equipment_capabilities.process_id", "to": "processes.process_id",
             "cardinality": "N:1"},
            {"from": "recipes.process_id", "to": "processes.process_id", "cardinality": "N:1"},
            {"from": "recipes.product_id", "to": "products.product_id", "cardinality": "N:1"},
        ],
    }

    META[PROD_DB] = {
        "title": "生産管理DB",
        "description": "ロットの投入から出荷まで。いまどの工程にどれだけ仕掛があるかは wip を見る。"
                       "直近90日分。",
        "caveats": [
            "仕掛（WIP）の現在値は wip テーブルにある。process_results を数えても仕掛にはならない。",
            "枚数は工程ごとに減りうる（scrap_wafers）。最新の枚数は wip.wafer_count を使う。",
            "装置・工程・製品の名前は fab_master 側にある。",
        ],
        "tables": {
            "lots": {
                "description": "ロット。1行 = 1ロット（ウェハの束）。生産の管理単位。",
                "columns": {
                    "status": {"description": "ロットの状態",
                               "values": {"仕掛": "ライン上を流れている", "完了": "全工程を終えた",
                                          "保留": "止まっている", "廃棄": "廃却した"}},
                    "priority": {"description": "優先度",
                                 "values": {"通常": "標準", "特急": "優先的に流す",
                                            "最優先": "他を止めてでも流す"}},
                    "current_step_no": {"description": "いま何工程目にいるか（routes.step_no）"},
                    "current_process_id": {"description": "いま待っている工程（fab_master.processes）"},
                    "wafer_count": {"description": "投入時の枚数。減耗後は wip を見る"},
                },
                "glossary": {
                    "仕掛ロット": {"description": "まだライン上にあるロット。廃棄と完了を除く。",
                                   "sql": "lots.status IN ('仕掛', '保留')"},
                    "納期遅れ": {"description": "納期を過ぎてもまだ完了していないロット。",
                                 "sql": "lots.status <> '完了' AND lots.due_date < date('now')"},
                    "TAT": {"description": "投入から完了までの日数（Turn Around Time）。短いほど良い。",
                            "sql": "julianday(lots.due_date) - julianday(lots.start_date)"},
                }},
            "wip": {
                "description": "工程別の仕掛在庫。1行 = 「このロットがこの工程の前で待っている」。"
                               "いまの姿を表すスナップショット。",
                "columns": {
                    "waiting_hours": {"description": "その工程の前で待っている時間（時間）"},
                    "hold_flag": {"description": "保留中かどうか",
                                  "values": {"1": "保留中", "0": "通常"}},
                },
                "glossary": {
                    "工程別仕掛枚数": {"description": "どの工程にどれだけウェハが溜まっているか。ボトルネックを探す基本。",
                                       "sql": "SUM(wip.wafer_count)"},
                    "滞留": {"description": "48時間以上待っている仕掛。ここが詰まりの兆候。",
                             "sql": "wip.waiting_hours >= 48"},
                }},
            "process_results": {
                "description": "工程実績。1行 = ロット1本をある装置で1工程処理した記録。",
                "columns": {
                    "process_minutes": {"description": "実処理時間（分）。standard_minutes との差が改善余地"},
                    "scrap_wafers": {"description": "その工程で失った枚数"},
                    "result_flag": {"description": "処理結果",
                                    "values": {"正常": "問題なし", "再処理": "やり直した",
                                               "一部廃棄": "枚数が減った"}},
                },
                "glossary": {
                    "処理枚数": {"description": "装置が実際に処理した枚数。稼働の実績値。",
                                 "sql": "SUM(process_results.input_wafers)"},
                    "工程通過率": {"description": "投入枚数のうち残った割合。",
                                   "sql": "SUM(process_results.output_wafers) * 1.0 / SUM(process_results.input_wafers)"},
                }},
            "lot_holds": {
                "description": "ロットの保留履歴。1行 = 1回の保留。released_at が空なら保留中。",
                "columns": {"hold_type": {"description": "保留の種類",
                                          "values": {"品質": "測定NGなど", "工程": "装置待ち・部材待ちなど"}}}},
            "shipments": {"description": "出荷。1行 = 1ロットの出荷。"},
        },
        "relationships": [
            {"from": "wip.lot_id", "to": "lots.lot_id", "cardinality": "1:1"},
            {"from": "process_results.lot_id", "to": "lots.lot_id", "cardinality": "N:1"},
            {"from": "lot_holds.lot_id", "to": "lots.lot_id", "cardinality": "N:1"},
            {"from": "shipments.lot_id", "to": "lots.lot_id", "cardinality": "1:1"},
            {"from": "lots.product_id", "to": "fab_master.products.product_id",
             "cardinality": "N:1"},
            {"from": "lots.current_process_id", "to": "fab_master.processes.process_id",
             "cardinality": "N:1"},
            {"from": "wip.process_id", "to": "fab_master.processes.process_id",
             "cardinality": "N:1"},
            {"from": "process_results.process_id", "to": "fab_master.processes.process_id",
             "cardinality": "N:1"},
            {"from": "process_results.equipment_id", "to": "fab_master.equipments.equipment_id",
             "cardinality": "N:1"},
            {"from": "process_results.recipe_id", "to": "fab_master.recipes.recipe_id",
             "cardinality": "N:1"},
        ],
    }

    META[EQP_DB] = {
        "title": "装置稼働DB",
        "description": "装置の状態ログ・日次稼働サマリ・アラーム・保全記録・計測値。直近90日分。",
        "caveats": [
            "稼働率を出すなら equipment_daily を使う（日次で集計済み）。"
            "equipment_states は状態の生ログなので、集計には向くが重い。",
            "装置名・種別は fab_master.equipments にある。",
            "down_minutes は故障、pm_minutes は計画保全。両者を混ぜないこと。",
        ],
        "tables": {
            "equipment_states": {
                "description": "装置の状態ログ（SEMI E10 に倣う）。1行 = 状態が続いた1区間。",
                "columns": {
                    "state": {"description": "装置の状態",
                              "values": {"稼働": "製品を処理している", "待機": "動けるがロットが無い",
                                         "段取": "レシピ切替・調整", "故障": "止まっている（計画外）",
                                         "計画保全": "定期点検（計画内）", "非稼働": "電源断など"}},
                }},
            "equipment_daily": {
                "description": "装置の日次稼働サマリ。1行 = 装置1台の1日。稼働分析はまずここを見る。",
                "columns": {
                    "availability": {"description": "可用率 =（1440 − 故障 − 計画保全）÷ 1440"},
                    "utilization": {"description": "稼働率 = 稼働分 ÷ 1440。実際に製品を処理していた割合"},
                    "wafer_count": {"description": "その日に処理した枚数"},
                },
                "glossary": {
                    "稼働率": {"description": "1日のうち実際に製品を処理していた時間の割合。設備の使い方の指標。",
                               "sql": "AVG(equipment_daily.utilization)"},
                    "可用率": {"description": "止まっていなかった時間の割合。故障と計画保全を除いた割合。",
                               "sql": "AVG(equipment_daily.availability)"},
                    "ダウンタイム": {"description": "計画外に止まっていた時間（分）。故障のみ。",
                                     "sql": "SUM(equipment_daily.down_minutes)"},
                }},
            "alarms": {
                "description": "アラーム・故障の履歴。1行 = 1件の発報。",
                "columns": {
                    "severity": {"description": "重大度",
                                 "values": {"重": "装置停止を伴う", "中": "処理に影響あり",
                                            "軽": "記録のみ。停止しない"}},
                    "down_minutes": {"description": "その発報で止まった時間（分）。0なら停止していない"},
                },
                "glossary": {
                    "停止アラーム": {"description": "実際に装置を止めたアラーム。件数を数えるときはこれで絞る。",
                                     "sql": "alarms.down_minutes > 0"},
                    "MTTR": {"description": "平均修復時間。1件あたり何分止まったか。",
                             "sql": "AVG(alarms.down_minutes)"},
                }},
            "maintenances": {
                "description": "保全記録。1行 = 1回の保全。予定のものは done_date が空。",
                "columns": {
                    "maintenance_type": {"description": "保全の種類",
                                         "values": {"定期保全": "計画的な点検（PM）",
                                                    "事後保全": "壊れてから直した（BM）",
                                                    "改造": "仕様変更"}},
                    "status": {"description": "完了／予定",
                               "values": {"完了": "実施済み", "予定": "これから"}},
                },
                "glossary": {
                    "保全費用": {"description": "保全にかかった金額の合計（円）。",
                                 "sql": "SUM(maintenances.cost_yen)"},
                }},
            "equipment_meters": {
                "description": "装置の計測値。1日2回。温度・圧力・積算RF時間・パーティクル数。",
                "columns": {
                    "rf_hours": {"description": "積算稼働時間。部品寿命の判断に使う"},
                    "particle_count": {"description": "パーティクル数。増えてきたら清掃・部品交換の合図"},
                }},
        },
        "relationships": [
            {"from": "equipment_states.equipment_id", "to": "fab_master.equipments.equipment_id",
             "cardinality": "N:1"},
            {"from": "equipment_daily.equipment_id", "to": "fab_master.equipments.equipment_id",
             "cardinality": "N:1"},
            {"from": "alarms.equipment_id", "to": "fab_master.equipments.equipment_id",
             "cardinality": "N:1"},
            {"from": "maintenances.equipment_id", "to": "fab_master.equipments.equipment_id",
             "cardinality": "N:1"},
            {"from": "equipment_meters.equipment_id", "to": "fab_master.equipments.equipment_id",
             "cardinality": "N:1"},
        ],
    }

    META[PARTS_DB] = {
        "title": "部品在庫DB",
        "description": "装置の部品マスタ・在庫・入出庫・交換履歴・発注。直近90日分。",
        "caveats": [
            "在庫は倉庫ごとに行が分かれている。総在庫を見るなら part_id で合計すること。",
            "引当済み(reserved_quantity)は使えない在庫。使える在庫 = quantity − reserved_quantity。",
            "廃番(parts.status='廃番')の部品は発注できない。",
        ],
        "tables": {
            "parts": {
                "description": "部品マスタ。1行 = 1品目。",
                "columns": {
                    "category": {"description": "部品の区分",
                                 "values": {"消耗品": "使うたびに減る", "交換部品": "寿命で交換",
                                            "治工具": "作業に使う道具"}},
                    "equipment_type": {"description": "どの装置種別で使うか（fab_master.equipments.equipment_type）"},
                    "life_hours": {"description": "標準寿命（時間）。空欄は寿命管理をしない部品"},
                    "safety_stock": {"description": "安全在庫。これを割ったら発注する目安"},
                    "lead_time_days": {"description": "発注してから届くまでの日数"},
                    "status": {"description": "有効／廃番",
                               "values": {"有効": "調達できる", "廃番": "もう買えない"}},
                }},
            "equipment_parts": {
                "description": "装置ごとの適合部品。1行 = 「この装置にこの部品が使える」。",
                "columns": {"quantity_per_unit": {"description": "1台あたりの必要数"}}},
            "part_stocks": {
                "description": "部品在庫。1行 = 部品×倉庫の在庫。",
                "columns": {
                    "quantity": {"description": "在庫数（引当を含む）"},
                    "reserved_quantity": {"description": "引当済み数。すでに使い道が決まっている分"},
                },
                "glossary": {
                    "有効在庫": {"description": "実際に使える在庫。引当済みを引いた数。",
                                 "sql": "SUM(part_stocks.quantity - part_stocks.reserved_quantity)"},
                    "安全在庫割れ": {"description": "在庫が安全在庫を下回っている状態。発注の判断に使う。",
                                     "sql": "SUM(part_stocks.quantity) < parts.safety_stock"},
                    "在庫金額": {"description": "在庫数×単価。棚卸資産の評価に使う。",
                                 "sql": "SUM(part_stocks.quantity * parts.unit_price_yen)"},
                }},
            "part_movements": {
                "description": "部品の入出庫。1行 = 1回の移動。",
                "columns": {
                    "movement_type": {"description": "移動の種類",
                                      "values": {"入庫": "倉庫に入った", "出庫": "装置に払い出した",
                                                 "返却": "戻ってきた", "廃棄": "捨てた"}},
                    "equipment_id": {"description": "出庫先の装置（入庫のときは空）"},
                }},
            "part_replacements": {
                "description": "部品の交換履歴。1行 = 1回の交換。",
                "columns": {
                    "used_hours": {"description": "交換時点の使用時間。標準寿命との比較に使う"},
                    "reason": {"description": "交換の理由",
                               "values": {"寿命到達": "計画どおり", "破損": "壊れた",
                                          "予防交換": "念のため", "不具合対応": "トラブル対応"}},
                },
                "glossary": {
                    "寿命到達率": {"description": "標準寿命に対して何割使えたか。1未満は早期交換。",
                                   "sql": "AVG(part_replacements.used_hours * 1.0 / parts.life_hours)"},
                }},
            "part_orders": {
                "description": "部品の発注。1行 = 1発注。",
                "columns": {
                    "status": {"description": "発注の状態",
                               "values": {"手配中": "まだ届いていない（納期前）",
                                          "入庫済": "受け入れ済み", "遅延": "納期を過ぎている"}},
                },
                "glossary": {
                    "納期遅延": {"description": "予定日を過ぎても届いていない発注。",
                                 "sql": "part_orders.status = '遅延'"},
                    "調達リードタイム実績": {"description": "発注から入庫までの実日数。",
                                             "sql": "julianday(part_orders.arrived_at) - julianday(part_orders.ordered_at)"},
                }},
        },
        "relationships": [
            {"from": "equipment_parts.part_id", "to": "parts.part_id", "cardinality": "N:1"},
            {"from": "part_stocks.part_id", "to": "parts.part_id", "cardinality": "N:1"},
            {"from": "part_movements.part_id", "to": "parts.part_id", "cardinality": "N:1"},
            {"from": "part_replacements.part_id", "to": "parts.part_id", "cardinality": "N:1"},
            {"from": "part_orders.part_id", "to": "parts.part_id", "cardinality": "N:1"},
            {"from": "equipment_parts.equipment_id", "to": "fab_master.equipments.equipment_id",
             "cardinality": "N:1"},
            {"from": "part_movements.equipment_id", "to": "fab_master.equipments.equipment_id",
             "cardinality": "N:1"},
            {"from": "part_replacements.equipment_id", "to": "fab_master.equipments.equipment_id",
             "cardinality": "N:1"},
            {"from": "part_replacements.maintenance_id",
             "to": "fab_equipment.maintenances.maintenance_id", "cardinality": "N:1"},
        ],
    }

    META[QUAL_DB] = {
        "title": "品質DB",
        "description": "工程内の測定値・欠陥検査・ロット歩留まり。直近90日分。",
        "caveats": [
            "測定値の合否は judgement 列にある。規格は lower_limit / upper_limit。",
            "歩留まりは完了ロットにしか無い（仕掛中のロットには行が無い）。",
            "測定は検査工程の実績にだけ紐づく。全工程にあるわけではない。",
        ],
        "tables": {
            "measurements": {
                "description": "工程内測定。1行 = 1回の測定値。",
                "columns": {
                    "item": {"description": "測定項目",
                             "values": {"膜厚": "成膜の厚み(nm)", "線幅": "パターンの幅(nm)",
                                        "オーバーレイ": "重ね合わせずれ(nm)",
                                        "シート抵抗": "薄膜の抵抗(Ω/sq)"}},
                    "judgement": {"description": "規格内かどうか",
                                  "values": {"OK": "規格内", "NG": "規格外"}},
                },
                "glossary": {
                    "規格外率": {"description": "測定のうち規格を外れた割合。装置の調子を見る基本指標。",
                                 "sql": "SUM(CASE WHEN measurements.judgement = 'NG' THEN 1 ELSE 0 END) * 1.0 / COUNT(*)"},
                    "目標からのずれ": {"description": "測定値と目標値の差。装置ごとの癖を見るのに使う。",
                                       "sql": "AVG(measurements.value - measurements.target_value)"},
                }},
            "defects": {
                "description": "欠陥検査。1行 = ウェハ1枚の検査結果。",
                "columns": {"defect_density": {"description": "欠陥密度（個/cm²）。歩留まりと強く相関する"}}},
            "yields": {
                "description": "ロット歩留まり。1行 = 1ロットの最終テスト結果。完了ロットのみ。",
                "columns": {
                    "yield_rate": {"description": "良品率 = good_dies ÷ tested_dies"},
                    "fail_bin_top": {"description": "最も多かった不良のBIN"},
                    "grade": {"description": "歩留まりの格付け",
                              "values": {"A": "92%以上", "B": "85〜92%", "C": "85%未満。要因分析の対象"}},
                },
                "glossary": {
                    "歩留まり": {"description": "良品ダイ数 ÷ 検査ダイ数。工場の最重要指標。",
                                 "sql": "SUM(yields.good_dies) * 1.0 / SUM(yields.tested_dies)"},
                    "低歩留まりロット": {"description": "格付けCのロット。原因調査の対象。",
                                         "sql": "yields.grade = 'C'"},
                }},
        },
        "relationships": [
            {"from": "measurements.lot_id", "to": "fab_production.lots.lot_id",
             "cardinality": "N:1"},
            {"from": "measurements.process_id", "to": "fab_master.processes.process_id",
             "cardinality": "N:1"},
            {"from": "measurements.equipment_id", "to": "fab_master.equipments.equipment_id",
             "cardinality": "N:1"},
            {"from": "defects.lot_id", "to": "fab_production.lots.lot_id", "cardinality": "N:1"},
            {"from": "defects.equipment_id", "to": "fab_master.equipments.equipment_id",
             "cardinality": "N:1"},
            {"from": "yields.lot_id", "to": "fab_production.lots.lot_id", "cardinality": "1:1"},
        ],
    }


def write_meta():
    _build_meta()
    for path, meta in META.items():
        mp = catalog.meta_path(path)
        if mp.exists():
            print(f"SKIP {mp.name} は既存のため上書きしません（作り直すなら削除してから実行）")
            continue
        mp.write_text(
            __import__("yaml").safe_dump(meta, allow_unicode=True, sort_keys=False),
            encoding="utf-8")
        print(f"OK  {mp.name} を作成")


if __name__ == "__main__":
    m = build_master()
    prod = build_production(m)
    eq = build_equipment(m, prod)
    build_parts(m, eq)
    build_quality(m, prod)
    print()
    write_meta()
    print("""
半導体工場のデモDB（5DB × 全26テーブル）を作成しました。

横断クエリの例:
  「いまどの工程に仕掛が溜まっている？上位10工程を棒グラフで」   … production × master
  「装置種別ごとの稼働率を比べて」                              … equipment × master
  「故障が多い装置トップ10と、その装置の平均稼働率」            … equipment × master
  「安全在庫を割っている部品と、その部品を使う装置」            … parts × master
  「歩留まりが低いロットは、どの装置を通っている？」            … quality × production × master
  「装置の年齢と故障回数に相関はある？」                        … equipment × master
""")
