"""
FlashEats late delivery pipeline
FDE Assignment 2 (Classes 4-8), Track A

Run:
    python pipeline.py        (starts the mock dispatch API itself if it isn't running)

Steps:
    1. INGEST     - SQL database, dispatch API (raw pages saved), driver app JSON, CSV files
    2. VALIDATE   - data checks; stop before writing any results if a critical one fails
    3. TRANSFORM  - one table with a row per order
    4. METRICS    - 5 metrics + report.md
Everything goes to output/ (a rerun overwrites it). Log: output/pipeline.log
"""
import json
import logging
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

HERE = Path(__file__).parent
DB = HERE / "database" / "flasheats.db"
DATA = HERE / "data"
OUT = HERE / "output"

API_URL = "http://127.0.0.1:8000"
PAGE_SIZE = 100
MAX_RETRIES = 3

LATE_OVER = 10    # Support Lead counts an order as "really late" after 10 minutes
RISK_SLIP = 10    # picked up more than 10 min after the planned pickup = at risk

log = logging.getLogger("pipeline")


def to_time(col):
    return pd.to_datetime(col, format="mixed", errors="coerce")


def clean_text(col):
    # only fixes how a value is written (case, spaces), not what it means
    return col.astype("string").str.strip().str.lower().str.replace(r"\s+", "_", regex=True)


def minutes(a, b):
    return (a - b).dt.total_seconds() / 60


# ---------------------------------------------------------------------------
# 1. INGEST
# ---------------------------------------------------------------------------

def load_database():
    con = sqlite3.connect(DB)
    tables = {}
    for t in ["orders", "customers", "drivers", "restaurants"]:
        tables[t] = pd.read_sql(f"SELECT * FROM {t}", con)
        log.info(f"SQL  {t}: {len(tables[t])} rows")
    con.close()
    return tables


def api_is_up():
    try:
        return requests.get(f"{API_URL}/health", timeout=1).ok
    except requests.RequestException:
        return False


def start_mock_api():
    """Start the class mock dispatch API if it isn't running, so the pipeline is one command."""
    if api_is_up():
        return None
    log.info("Starting the mock dispatch API")
    proc = subprocess.Popen([sys.executable, str(HERE / "api" / "mock_dispatch_api.py")],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(24):
        time.sleep(0.5)
        if api_is_up():
            return proc
    proc.terminate()
    raise RuntimeError("Mock dispatch API didn't start - is port 8000 already in use?")


def get_page(page):
    """Get one page from the dispatch API. Retries on 429 / 5xx because the API fails on purpose."""
    for attempt in range(1, MAX_RETRIES + 1):
        r = requests.get(f"{API_URL}/dispatch/orders", params={"page": page, "page_size": PAGE_SIZE}, timeout=10)
        if r.status_code == 200:
            return r, attempt - 1
        if r.status_code not in (429, 500, 502, 503, 504):
            raise RuntimeError(f"API page {page} failed with HTTP {r.status_code}")
        wait = r.json().get("retry_after_seconds", attempt) if r.status_code == 429 else attempt
        log.warning(f"API  page {page}: HTTP {r.status_code}, retry {attempt}/{MAX_RETRIES} in {wait}s")
        time.sleep(wait)
    raise RuntimeError(f"API page {page} still failing after {MAX_RETRIES} tries")


def fetch_dispatch_api():
    records, raw_pages, page, retries = [], [], 1, 0
    while True:
        r, retried = get_page(page)
        retries += retried
        raw_pages.append(r.text)
        body = r.json()
        records += body["data"]
        if not body["has_more"]:
            break
        page += 1

    # save the raw responses exactly as received - only once every page came back
    raw_dir = OUT / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    for old in raw_dir.glob("dispatch_page_*.json"):
        old.unlink()
    for i, text in enumerate(raw_pages, 1):
        (raw_dir / f"dispatch_page_{i:03d}.json").write_text(text, encoding="utf-8")

    ids = [x["order_id"] for x in records]
    api = {"pages": page, "records": len(records), "reported_total": body["total_records"],
           "unique_ids": len(set(ids)), "retries": retries}
    log.info(f"API  {page} pages, {len(records)} records (API says {body['total_records']}), {retries} retries")
    return pd.DataFrame(records), api


def load_files():
    files = {}
    drivers = json.loads((DATA / "driver_events.json").read_text(encoding="utf-8"))
    # nested JSON: one object per driver with a list of events -> one row per event
    files["driver_events"] = pd.DataFrame(
        [{"driver_id": d["driver_id"], **e} for d in drivers for e in d["events"]])
    log.info(f"JSON driver_events: {len(files['driver_events'])} events")

    for name in ["support_tickets", "customer_app_actions", "order_interventions",
                 "restaurant_status", "order_outcomes"]:
        files[name] = pd.read_csv(DATA / f"{name}.csv", dtype=str)
        log.info(f"CSV  {name}: {len(files[name])} rows")

    files["manifest"] = json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))
    return files


# ---------------------------------------------------------------------------
# 2. VALIDATE
# ---------------------------------------------------------------------------

def spelling_variants(col):
    raw = col.dropna().astype("string")
    changed = raw[raw != clean_text(raw)]
    return ", ".join(f"'{v}' x{n}" for v, n in changed.value_counts().items()) or "none"


def run_checks(d, api):
    checks = []
    found = {}   # numbers reused later in the table, the metrics and the report

    def add(name, result, details, action, critical=False):
        checks.append({"check": name, "result": result, "details": details,
                       "action": action, "critical": critical})
        (log.info if result == "PASS" else log.warning)(f"{result:7s} {name}")

    orders, dispatch = d["orders"], d["dispatch"]
    first = orders.drop_duplicates("order_id").copy()
    for c in ["created_at", "promised_eta", "pickup_at", "actual_delivery_at"]:
        first[c] = to_time(first[c])
    status = clean_text(first.final_status)

    # --- is the data complete?
    ok = api["records"] == api["reported_total"] == api["unique_ids"]
    add("API returned every record exactly once", "PASS" if ok else "FAIL",
        f"{api['pages']} pages, {api['records']} records, API says {api['reported_total']}, "
        f"{api['unique_ids']} unique order ids, {api['retries']} failed requests retried",
        "Saved every raw page in output/raw", critical=True)

    expected = d["manifest"]["record_counts"]
    got = {"orders_rows": len(orders), "unique_orders": orders.order_id.nunique(),
           "support_tickets_rows": len(d["support_tickets"]), "dispatch_records": len(dispatch),
           "drivers": len(d["drivers"]), "restaurants": len(d["restaurants"])}
    wrong = {k: (got.get(k), v) for k, v in expected.items() if got.get(k) != v}
    add("Row counts match the client's manifest.json", "PASS" if not wrong else "FAIL",
        "all 6 counts match" if not wrong else f"got vs expected: {wrong}",
        "Nothing, it confirms no table was cut short", critical=True)

    mismatch = set(first.order_id) ^ set(dispatch.order_id)
    add("Orders table and dispatch API have the same orders", "PASS" if not mismatch else "FAIL",
        f"{len(mismatch)} orders in one but not the other", "Joined them one to one", critical=True)

    # --- grain
    dups = orders[orders.order_id.duplicated(keep=False)]
    differ = sorted({c for _, g in dups.groupby("order_id") for c in g.columns if g[c].nunique(dropna=False) > 1})
    found["duplicate_orders"] = dups.order_id.nunique()
    add("One row per order", "WARN" if len(dups) else "PASS",
        f"{found['duplicate_orders']} orders appear twice; the copies only differ on {', '.join(differ) or 'nothing'}",
        "Kept the first copy (the difference doesn't affect the KPI)")

    # --- timestamps
    de = d["driver_events"].assign(ts=lambda x: to_time(x.timestamp))
    app_delivered = de[de.type == "delivered"].groupby("order_id").ts.max()
    delivered = first[status == "delivered"]
    no_time = delivered[delivered.actual_delivery_at.isna()]
    both = delivered.dropna(subset=["actual_delivery_at"]).set_index("order_id").join(
        app_delivered.rename("app"), how="inner")
    agree = ((both.actual_delivery_at - both.app).abs() < pd.Timedelta(seconds=1)).mean()
    found["untimed_delivered"] = len(no_time)
    found["both_timed"] = len(both)
    found["use_driver_app"] = bool(agree >= 0.99)
    add("Delivered orders have a delivery time", "WARN" if len(no_time) else "PASS",
        f"{len(no_time)} of {len(delivered)} delivered orders have no delivery time. The driver app has one for "
        f"{no_time.order_id.isin(app_delivered.index).sum()} of them, and it matches the orders table to the "
        f"second on {agree:.1%} of the {len(both)} orders both systems timed",
        "Used the driver app time for those orders (only because the two systems agree)"
        if found["use_driver_app"] else "Left them out")

    bad_promise = first[first.promised_eta < first.created_at]
    inverted = delivered[delivered.actual_delivery_at < delivered.pickup_at]
    found["bad_promise"] = len(bad_promise)
    found["inverted"] = len(inverted)
    add("Timestamps are in the right order", "WARN" if len(bad_promise) or len(inverted) else "PASS",
        f"{len(bad_promise)} orders have a promised ETA before the order was placed; "
        f"{len(inverted)} orders were delivered before they were picked up",
        "Left the first group out of the KPI and the second out of the pickup/transit split")

    # --- categories
    t, rs = d["support_tickets"], d["restaurant_status"]
    add("Status and category values are written one way", "WARN",
        f"final_status: {spelling_variants(first.final_status)}; traffic: {spelling_variants(first.traffic_bucket)}; "
        f"ticket category: {spelling_variants(t.category)}; restaurant status: {spelling_variants(rs.status)}",
        "Fixed case and spaces only. Did not merge values that might mean something different, "
        "like 'ETA issue' or 'handoff'")

    # --- meaning
    live = first.merge(dispatch[["order_id", "current_delivery_eta"]], on="order_id")
    live["live_eta"] = to_time(live.current_delivery_eta)
    drift = minutes(live.live_eta, live.promised_eta)
    timed = live[(clean_text(live.final_status) == "delivered") & live.actual_delivery_at.notna()]
    found["eta_revised_share"] = float((drift > 0).mean())
    add("Which ETA counts as the promise?", "WARN",
        f"Dispatch moved the ETA later on {(drift > 0).mean():.1%} of orders (median +{drift.median():.1f} min). "
        f"The late rate is {(timed.actual_delivery_at > timed.promised_eta).mean():.1%} against the original "
        f"promise but only {(timed.actual_delivery_at > timed.live_eta).mean():.1%} against the updated ETA",
        "Used the original promise, because that is what the customer saw at checkout")

    found["reassigned"] = int((dispatch.driver_id != dispatch.original_driver_id).sum())
    add("orders.driver_id is the driver who delivered", "WARN",
        f"orders.driver_id is always the first driver assigned; {found['reassigned']} orders were reassigned",
        "Took the driver from the dispatch API instead")

    geo = (first.merge(d["restaurants"][["restaurant_id", "lat", "lon"]], on="restaurant_id")
           .merge(d["customers"][["customer_id", "lat", "lon"]], on="customer_id", suffixes=("_r", "_c")))
    lat1, lon1, lat2, lon2 = (np.radians(geo[c]) for c in ["lat_r", "lon_r", "lat_c", "lon_c"])
    geo["km"] = 2 * 6371 * np.arcsin(np.sqrt(np.sin((lat2 - lat1) / 2) ** 2
                                             + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2))
    found["distance_capped_share"] = float((first.distance_km_estimate == 18.0).mean())
    add("Distance column is usable", "FAIL",
        f"{found['distance_capped_share']:.1%} of orders are exactly 18.0 km. Working out the straight-line "
        f"distance from the coordinates, those orders are always more than "
        f"{geo[geo.distance_km_estimate == 18.0].km.min():.1f} km away, so 18.0 is a cap, not a real distance",
        "Didn't use distance")

    coverage = rs.order_id.nunique() / len(first)
    add("Can we tell restaurant delay from driver delay?", "UNKNOWN",
        f"The driver app has no 'arrived at restaurant' event (only {', '.join(sorted(de.type.unique()))}), "
        f"and restaurant status only covers {coverage:.0%} of orders with just the latest status",
        "Reported the delay before pickup as one number")

    iv = d["order_interventions"].merge(first[["order_id", "created_at", "actual_delivery_at"]], on="order_id")
    iv_at = to_time(iv.intervention_at)
    found["interventions_after_delivery"] = int((iv_at > iv.actual_delivery_at).sum())
    add("Interventions happen while the order is in progress", "WARN",
        f"{(iv_at < iv.created_at).sum()} logged before the order was placed, "
        f"{found['interventions_after_delivery']} after delivery (all customer credits)",
        "Only counted interventions made before delivery when checking if they help")

    oc = d["order_outcomes"].merge(first[["order_id", "promised_eta", "actual_delivery_at"]], on="order_id")
    theirs = oc.delay_min.astype(float)
    match = ((minutes(oc.actual_delivery_at, oc.promised_eta) - theirs).abs() < 0.05)[theirs.notna()].mean()
    add("Delay numbers match the client's order_outcomes.csv", "PASS" if match == 1 else "FAIL",
        f"match on {match:.1%} of the {theirs.notna().sum()} orders it has a delay for",
        "Only used order_outcomes.csv for this cross-check, not as an input")

    return pd.DataFrame(checks), found


# ---------------------------------------------------------------------------
# 3. TRANSFORM - one row per order
# ---------------------------------------------------------------------------

def build_order_table(d, found):
    o = d["orders"].drop_duplicates("order_id", keep="first").copy()   # one row per order
    for c in ["created_at", "promised_eta", "pickup_at", "actual_delivery_at"]:
        o[c] = to_time(o[c])
    o["final_status"] = clean_text(o.final_status)

    disp = d["dispatch"].copy()
    for c in ["reassigned_at", "estimated_pickup_at", "current_delivery_eta"]:
        disp[c] = to_time(disp[c])
    o = o.rename(columns={"driver_id": "first_driver_id", "final_status": "status"}).merge(
        disp[["order_id", "driver_id", "reassigned_at", "estimated_pickup_at", "current_delivery_eta"]]
        .rename(columns={"estimated_pickup_at": "planned_pickup_at", "current_delivery_eta": "live_eta"}),
        on="order_id", how="left")

    # delivery time: orders table first, driver app only if the check showed they agree
    de = d["driver_events"].assign(ts=lambda x: to_time(x.timestamp))
    app_delivered = de[de.type == "delivered"].groupby("order_id").ts.max()
    o["delivered_at"] = o.actual_delivery_at
    o["delivered_at_from"] = np.where(o.actual_delivery_at.notna(), "orders_table", None)
    if found["use_driver_app"]:
        fill = (o.status == "delivered") & o.delivered_at.isna() & o.order_id.isin(app_delivered.index)
        o.loc[fill, "delivered_at"] = o.loc[fill, "order_id"].map(app_delivered)
        o.loc[fill, "delivered_at_from"] = "driver_app"

    # which orders count for the KPI, and why not
    o["excluded_because"] = np.select(
        [o.status != "delivered", o.delivered_at.isna(), o.promised_eta < o.created_at],
        ["not_delivered", "no_delivery_time", "promise_before_order"], default="")
    o["in_kpi"] = o.excluded_because == ""

    o["delay_min"] = minutes(o.delivered_at, o.promised_eta).where(o.in_kpi)
    o["late"] = (o.delay_min > 0).where(o.in_kpi)
    o["late_over_10"] = (o.delay_min > LATE_OVER).where(o.in_kpi)
    o["eta_drift_min"] = minutes(o.live_eta, o.promised_eta)
    o["delay_vs_live_eta_min"] = minutes(o.delivered_at, o.live_eta).where(o.in_kpi)

    # split the delay: late pickup vs slow drive (dispatch gives a planned pickup time)
    o["can_split"] = o.in_kpi & o.pickup_at.notna() & o.planned_pickup_at.notna() & (o.pickup_at <= o.delivered_at)
    o["pickup_slip_min"] = minutes(o.pickup_at, o.planned_pickup_at).where(o.can_split)
    o["transit_overrun_min"] = (o.delay_min - o.pickup_slip_min).where(o.can_split)
    o["at_risk"] = (o.pickup_slip_min > RISK_SLIP).where(o.can_split)

    # customer side: support opened in the app or a support ticket, counted per order
    acts = d["customer_app_actions"]
    opens = acts[acts.action_type == "SUPPORT_OPENED"].order_id.value_counts()
    tickets = d["support_tickets"].drop_duplicates()          # one ticket was in the file twice
    o["support_opens"] = o.order_id.map(opens).fillna(0).astype(int)
    o["support_tickets"] = o.order_id.map(tickets.order_id.value_counts()).fillna(0).astype(int)
    o["contacted_support"] = (o.support_opens > 0) | (o.support_tickets > 0)

    # interventions: only the ones made before pickup can stop the pickup being late
    iv = d["order_interventions"].copy()
    iv["intervention_at"] = to_time(iv.intervention_at)
    iv = iv.merge(o[["order_id", "created_at", "pickup_at", "status"]], on="order_id")
    before_pickup = iv[(iv.status == "delivered") & (iv.intervention_at >= iv.created_at)
                       & (iv.intervention_at <= iv.pickup_at)]
    o["interventions"] = o.order_id.map(iv.order_id.value_counts()).fillna(0).astype(int)
    o["intervention_before_pickup"] = o.order_id.isin(before_pickup.order_id)

    cols = ["order_id", "customer_id", "restaurant_id", "first_driver_id", "driver_id", "status",
            "created_at", "planned_pickup_at", "pickup_at", "promised_eta", "live_eta",
            "delivered_at", "delivered_at_from", "in_kpi", "excluded_because",
            "delay_min", "late", "late_over_10", "eta_drift_min", "delay_vs_live_eta_min",
            "can_split", "pickup_slip_min", "transit_overrun_min", "at_risk",
            "support_opens", "support_tickets", "contacted_support",
            "interventions", "intervention_before_pickup"]
    return o[cols].sort_values("order_id").reset_index(drop=True)


# ---------------------------------------------------------------------------
# 4. METRICS + REPORT
# ---------------------------------------------------------------------------

def rate(mask):
    mask = mask.astype(bool)
    return {"value": round(float(mask.mean()), 4), "numerator": int(mask.sum()), "denominator": int(len(mask))}


def calculate_metrics(o, d, found, api, checks):
    kpi = o[o.in_kpi]
    split = o[o.can_split]
    late = kpi[kpi.late.astype(bool)]
    on_time = kpi[~kpi.late.astype(bool)]
    risk = split[split.at_risk.astype(bool)]

    slip = late.pickup_slip_min.dropna().clip(lower=0)
    over = late.transit_overrun_min.dropna().clip(lower=0)
    before_pickup = float(slip.sum() / (slip.sum() + over.sum()))

    metrics = [
        {"id": "M1", "metric": "Late Delivery Rate", "type": "Outcome (the KPI)", **rate(kpi.late),
         "unit": "share", "meaning": "Is the KPI"},
        {"id": "M2", "metric": "Pickup Slip Rate", "type": "Workflow (where delay starts)", **rate(split.at_risk),
         "unit": "share", "meaning": f"{before_pickup:.1%} of late minutes happen before pickup"},
        {"id": "M3", "metric": "ETA Drift", "type": "Promise reliability",
         "value": round(float(kpi.eta_drift_min.median()), 2), "numerator": None, "denominator": int(len(kpi)),
         "unit": "minutes", "meaning": "Against the updated ETA the late rate would be "
                                       f"{kpi.delay_vs_live_eta_min.gt(0).mean():.1%} instead of M1"},
        {"id": "M4", "metric": "Late-Order Support Contact Rate", "type": "Customer reaction",
         **rate(late.contacted_support), "unit": "share",
         "meaning": f"On-time orders: {on_time.contacted_support.mean():.1%}"},
        {"id": "M5", "metric": "At-Risk Intervention Coverage", "type": "Intervention",
         **rate(risk.intervention_before_pickup), "unit": "share",
         "meaning": f"At-risk orders end up late {risk.late.astype(bool).mean():.1%} of the time"},
    ]

    summary = checks.result.value_counts()
    return {
        "api": api,
        "row_counts": {"orders_rows": len(d["orders"]), "driver_events": len(d["driver_events"]),
                       **{k: len(d[k]) for k in ["support_tickets", "customer_app_actions",
                                                 "order_interventions", "restaurant_status"]}},
        "checks": {s: int(summary.get(s, 0)) for s in ["PASS", "WARN", "FAIL", "UNKNOWN"]},
        "found": found,
        "metrics": metrics,
        "late_rate_by_definition": [
            {"definition": "Any minute after the original promise (VP Operations, = M1)", **rate(kpi.late)},
            {"definition": "More than 10 minutes late (Support Lead)", **rate(kpi.late_over_10)},
            {"definition": "Against the updated ETA (not the KPI)", **rate(kpi.delay_vs_live_eta_min.gt(0))},
        ],
        "delay_split": {
            "late_minutes_before_pickup": round(before_pickup, 4),
            "late_orders_pickup_slip_median": round(float(late.pickup_slip_min.median()), 1),
            "late_orders_transit_overrun_median": round(float(late.transit_overrun_min.median()), 1),
            "late_rate_if_at_risk": rate(risk.late),
            "late_rate_if_not_at_risk": rate(split[~split.at_risk.astype(bool)].late),
        },
        "interventions": {
            "at_risk_with": rate(risk[risk.intervention_before_pickup].late),
            "at_risk_without": rate(risk[~risk.intervention_before_pickup].late),
        },
        "customers": {
            "support_on_time": rate(on_time.contacted_support),
            "support_late": rate(late.contacted_support),
        },
        "population": {
            "orders": int(len(o)), "in_kpi": int(len(kpi)),
            "excluded": o[~o.in_kpi].excluded_because.value_counts().to_dict(),
            "recovered_from_driver_app": int((kpi.delivered_at_from == "driver_app").sum()),
        },
    }


def pct(r):
    return f"{r['value']:.1%} ({r['numerator']}/{r['denominator']})"


def write_report(res, checks):
    m = {x["id"]: x for x in res["metrics"]}
    s, iv, cu, pop, f = res["delay_split"], res["interventions"], res["customers"], res["population"], res["found"]
    show = lambda x: f"{x['value']:.1%}" if x["unit"] == "share" else f"+{x['value']:.1f} min"  # noqa: E731
    excluded = ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in pop["excluded"].items())

    lines = [
        "# FlashEats late delivery report",
        "",
        "Made by `pipeline.py`. All numbers come from the data, not typed in by hand.",
        "",
        "## Metrics",
        "",
        "| ID | Metric | Type | Value | Count | What it means for the KPI |",
        "|---|---|---|---|---|---|",
        *[f"| {x['id']} | {x['metric']} | {x['type']} | **{show(x)}** | "
          f"{str(x['numerator']) + '/' + str(x['denominator']) if x['numerator'] is not None else ''} | {x['meaning']} |"
          for x in res["metrics"]],
        "",
        "- **M1** late deliveries / delivered orders with a valid promise and a delivery time",
        f"- **M2** orders picked up more than {RISK_SLIP} min after the planned pickup / orders where the delay could be split",
        "- **M3** median of (updated ETA from dispatch - original promised ETA)",
        "- **M4** late orders where the customer opened support in the app or raised a ticket / late orders",
        "- **M5** at-risk orders (from M2) that got an intervention before pickup / at-risk orders",
        "",
        f"{pop['orders']} orders in total, {pop['in_kpi']} counted in the KPI (left out: {excluded}). "
        f"{pop['recovered_from_driver_app']} delivery times came from the driver app.",
        "",
        "Late rate under different definitions of \"late\":",
        "",
        "| Definition | Late rate |",
        "|---|---|",
        *[f"| {x['definition']} | {pct(x)} |" for x in res["late_rate_by_definition"]],
        "",
        "## Main findings",
        "",
        f"- **{s['late_minutes_before_pickup']:.1%} of late minutes happen before pickup.** Late orders get "
        f"picked up a median {s['late_orders_pickup_slip_median']} min after the planned pickup, then the "
        f"drive is {abs(s['late_orders_transit_overrun_median'])} min *faster* than planned.",
        f"- Orders picked up more than {RISK_SLIP} min late end up late {pct(s['late_rate_if_at_risk'])}. "
        f"Everything else: {pct(s['late_rate_if_not_at_risk'])}.",
        f"- Late orders contact support {pct(cu['support_late'])}, on-time orders {pct(cu['support_on_time'])}.",
        f"- At-risk orders are late {pct(iv['at_risk_with'])} with an intervention before pickup and "
        f"{pct(iv['at_risk_without'])} without one.",
        "",
        "## Data checks",
        "",
        f"{len(checks)} checks - PASS {res['checks']['PASS']}, WARN {res['checks']['WARN']}, "
        f"FAIL {res['checks']['FAIL']}, UNKNOWN {res['checks']['UNKNOWN']}. Critical checks stop the pipeline "
        "if they fail.",
        "",
        "| # | Check | Result | Details | Action |",
        "|---|---|---|---|---|",
        *[f"| {i} | {c.check} | **{c.result}** | {c.details} | {c.action} |"
          for i, c in enumerate(checks.itertuples(), 1)],
        "",
        "## Known / Unknown / Assumptions / Limitations",
        "",
        "| | |",
        "|---|---|",
        f"| **Known** | {show(m['M1'])} of orders are late ({m['M1']['numerator']} of {m['M1']['denominator']}). "
        f"{s['late_minutes_before_pickup']:.1%} of late minutes happen before pickup. Late orders contact "
        f"support {cu['support_late']['value']:.1%} of the time vs {cu['support_on_time']['value']:.1%}. |",
        "| **Unknown** | Whether the pickup delay is the restaurant (food not ready) or the driver (arrived "
        "late). Whether interventions actually help - there's no control group. Who owns the KPI. |",
        "| **Assumptions** | The promise is `orders.promised_eta`. All timestamps are in the same timezone. "
        "The driver app time can be used when the orders table has none. The first copy of a duplicated "
        "order is the right one. |",
        f"| **Limitations** | One month (August 2026), one city. Distance can't be used "
        f"({f['distance_capped_share']:.1%} of orders capped at 18.0 km). Everything is association, not "
        "cause and effect. |",
        "",
        "## Recommendation",
        "",
        f"**Don't build the AI delay predictor yet.** The delay happens before pickup, and one simple rule "
        f"(picked up more than {RISK_SLIP} min late) already finds orders that end up late "
        f"{s['late_rate_if_at_risk']['value']:.0%} of the time. Instead: record when the driver arrives at "
        f"the restaurant and when the food is ready, act on at-risk orders earlier (only {show(m['M5'])} get "
        "an intervention before pickup today), and agree who owns the KPI.",
    ]
    (OUT / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------

def main():
    OUT.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(OUT / "pipeline.log", mode="w", encoding="utf-8")])
    api_process = None
    try:
        api_process = start_mock_api()

        log.info("1. INGEST")
        d = load_database()
        d["dispatch"], api = fetch_dispatch_api()
        d.update(load_files())

        log.info("2. VALIDATE")
        checks, found = run_checks(d, api)
        failed = checks[checks.critical & (checks.result == "FAIL")]
        if len(failed):
            for c in failed.itertuples():
                log.error(f"Critical check failed: {c.check} - {c.details}")
            log.error("Stopped. No results were written.")
            return 1

        log.info("3. TRANSFORM")
        orders = build_order_table(d, found)
        log.info(f"Order table: {len(orders)} rows, {orders.in_kpi.sum()} in the KPI")

        log.info("4. METRICS")
        res = calculate_metrics(orders, d, found, api, checks)
        orders.to_csv(OUT / "order_table.csv", index=False)
        checks.to_csv(OUT / "checks.csv", index=False)
        pd.DataFrame(res["metrics"]).astype({"numerator": "Int64"}).to_csv(OUT / "metrics.csv", index=False)
        (OUT / "results.json").write_text(json.dumps(res, indent=2), encoding="utf-8")
        write_report(res, checks)
        for x in res["metrics"]:
            log.info(f"{x['id']} {x['metric']}: {x['value']}")
        log.info("Done - see output/report.md")
        return 0

    except (RuntimeError, requests.RequestException) as e:
        log.error(f"Pipeline failed: {e}. No results were written.")
        return 1
    finally:
        if api_process:
            api_process.terminate()


if __name__ == "__main__":
    sys.exit(main())
