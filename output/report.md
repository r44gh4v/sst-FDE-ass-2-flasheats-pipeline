# FlashEats late delivery report

Made by `pipeline.py`. All numbers come from the data, not typed in by hand.

## Metrics

| ID | Metric | Type | Value | Count | What it means for the KPI |
|---|---|---|---|---|---|
| M1 | Late Delivery Rate | Outcome (the KPI) | **56.5%** | 863/1528 | Is the KPI |
| M2 | Pickup Slip Rate | Workflow (where delay starts) | **42.1%** | 641/1523 | 98.9% of late minutes happen before pickup |
| M3 | ETA Drift | Promise reliability | **+8.1 min** |  | Against the updated ETA the late rate would be 26.2% instead of M1 |
| M4 | Late-Order Support Contact Rate | Customer reaction | **29.9%** | 258/863 | On-time orders: 5.1% |
| M5 | At-Risk Intervention Coverage | Intervention | **20.8%** | 133/641 | At-risk orders end up late 96.9% of the time |

- **M1** late deliveries / delivered orders with a valid promise and a delivery time
- **M2** orders picked up more than 10 min after the planned pickup / orders where the delay could be split
- **M3** median of (updated ETA from dispatch - original promised ETA)
- **M4** late orders where the customer opened support in the app or raised a ticket / late orders
- **M5** at-risk orders (from M2) that got an intervention before pickup / at-risk orders

1600 orders in total, 1528 counted in the KPI (left out: 68 not delivered, 4 promise before order). 37 delivery times came from the driver app.

Late rate under different definitions of "late":

| Definition | Late rate |
|---|---|
| Any minute after the original promise (VP Operations, = M1) | 56.5% (863/1528) |
| More than 10 minutes late (Support Lead) | 23.2% (354/1528) |
| Against the updated ETA (not the KPI) | 26.2% (401/1528) |

## Main findings

- **98.9% of late minutes happen before pickup.** Late orders get picked up a median 13.9 min after the planned pickup, then the drive is 5.9 min *faster* than planned.
- Orders picked up more than 10 min late end up late 96.9% (621/641). Everything else: 27.2% (240/882).
- Late orders contact support 29.9% (258/863), on-time orders 5.1% (34/665).
- At-risk orders are late 97.0% (129/133) with an intervention before pickup and 96.9% (492/508) without one.

## Data checks

13 checks - PASS 4, WARN 7, FAIL 1, UNKNOWN 1. Critical checks stop the pipeline if they fail.

| # | Check | Result | Details | Action |
|---|---|---|---|---|
| 1 | API returned every record exactly once | **PASS** | 16 pages, 1600 records, API says 1600, 1600 unique order ids, 2 failed requests retried | Saved every raw page in output/raw |
| 2 | Row counts match the client's manifest.json | **PASS** | all 6 counts match | Nothing, it confirms no table was cut short |
| 3 | Orders table and dispatch API have the same orders | **PASS** | 0 orders in one but not the other | Joined them one to one |
| 4 | One row per order | **WARN** | 3 orders appear twice; the copies only differ on traffic_bucket | Kept the first copy (the difference doesn't affect the KPI) |
| 5 | Delivered orders have a delivery time | **WARN** | 37 of 1532 delivered orders have no delivery time. The driver app has one for 37 of them, and it matches the orders table to the second on 100.0% of the 1495 orders both systems timed | Used the driver app time for those orders (only because the two systems agree) |
| 6 | Timestamps are in the right order | **WARN** | 4 orders have a promised ETA before the order was placed; 5 orders were delivered before they were picked up | Left the first group out of the KPI and the second out of the pickup/transit split |
| 7 | Status and category values are written one way | **WARN** | final_status: 'Delivered' x5; traffic: 'HIGH' x3; ticket category: 'Late Delivery' x1, 'late_delivery ' x1, 'ETA issue' x1; restaurant status: 'ready ' x2, 'READY' x1, 'Ready' x1 | Fixed case and spaces only. Did not merge values that might mean something different, like 'ETA issue' or 'handoff' |
| 8 | Which ETA counts as the promise? | **WARN** | Dispatch moved the ETA later on 91.4% of orders (median +8.3 min). The late rate is 56.4% against the original promise but only 25.9% against the updated ETA | Used the original promise, because that is what the customer saw at checkout |
| 9 | orders.driver_id is the driver who delivered | **WARN** | orders.driver_id is always the first driver assigned; 95 orders were reassigned | Took the driver from the dispatch API instead |
| 10 | Distance column is usable | **FAIL** | 50.4% of orders are exactly 18.0 km. Working out the straight-line distance from the coordinates, those orders are always more than 18.1 km away, so 18.0 is a cap, not a real distance | Didn't use distance |
| 11 | Can we tell restaurant delay from driver delay? | **UNKNOWN** | The driver app has no 'arrived at restaurant' event (only assigned, delivered, gps_ping, picked_up), and restaurant status only covers 31% of orders with just the latest status | Reported the delay before pickup as one number |
| 12 | Interventions happen while the order is in progress | **WARN** | 1 logged before the order was placed, 45 after delivery (all customer credits) | Only counted interventions made before delivery when checking if they help |
| 13 | Delay numbers match the client's order_outcomes.csv | **PASS** | match on 100.0% of the 1495 orders it has a delay for | Only used order_outcomes.csv for this cross-check, not as an input |

## Known / Unknown / Assumptions / Limitations

| | |
|---|---|
| **Known** | 56.5% of orders are late (863 of 1528). 98.9% of late minutes happen before pickup. Late orders contact support 29.9% of the time vs 5.1%. |
| **Unknown** | Whether the pickup delay is the restaurant (food not ready) or the driver (arrived late). Whether interventions actually help - there's no control group. Who owns the KPI. |
| **Assumptions** | The promise is `orders.promised_eta`. All timestamps are in the same timezone. The driver app time can be used when the orders table has none. The first copy of a duplicated order is the right one. |
| **Limitations** | One month (August 2026), one city. Distance can't be used (50.4% of orders capped at 18.0 km). Everything is association, not cause and effect. |

## Recommendation

**Don't build the AI delay predictor yet.** The delay happens before pickup, and one simple rule (picked up more than 10 min late) already finds orders that end up late 97% of the time. Instead: record when the driver arrives at the restaurant and when the food is ready, act on at-risk orders earlier (only 20.8% get an intervention before pickup today), and agree who owns the KPI.
