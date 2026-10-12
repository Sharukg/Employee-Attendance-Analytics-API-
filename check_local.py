"""
Local verification script (NOT part of the submission - do not commit it).

1. Start the API against a THROW-AWAY database, e.g.
     MONGO_DB=attendance_check uvicorn app.main:app --port 8000
2. In another terminal (same MONGO_URI / MONGO_DB env vars):
     pip install requests pymongo
     python check_local.py            # small awkward dataset: correctness + concurrency + explain
     python check_local.py --big      # additionally loads ~100k logs and checks the explain plans / timing

It EMPTIES the employees and attendance_logs collections of $MONGO_DB (it deletes documents, it keeps the indexes).
"""
import os, sys, time, random, json, threading
from datetime import date, timedelta, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import requests
from pymongo import MongoClient

BASE = os.getenv("API", "http://localhost:8000")
mdb = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)[os.getenv("MONGO_DB", "attendance_db")]
if mdb.name in ("admin", "local", "config"): sys.exit("refusing to touch a system database")
mdb.employees.delete_many({}); mdb.attendance_logs.delete_many({})

class _C:   # tiny requests wrapper with the same .get(...).json() shape the checks use
    def get(self, path, params=None): return requests.get(BASE + path, params=params)
c = _C()

rnd = random.Random(7)
UTC = timezone.utc
def dt(d, h, mi): return datetime(d.year, d.month, d.day, h, mi, tzinfo=UTC)

emps = []
def add(code, dept, joined): emps.append({"emp_code": code, "name": "N"+code, "department": dept, "joined_on": joined, "shift_start": "09:30", "shift_end": "18:30"})
add("EMP0001","Eng","2026-01-05"); add("EMP0002","Eng","2026-07-10"); add("EMP0003","Eng","2026-07-31"); add("EMP0004","Eng","2026-08-01")
add("EMP0005","Ops","2026-03-01"); add("EMP0006","Ops","2026-07-01"); add("EMP0007","Ops","2026-07-15"); add("EMP0008","HR","2026-01-01"); add("EMP0009","HR","2026-05-05")
logs = []
days = [date(2026,6,28)+timedelta(days=i) for i in range(0, 40)]
for e in emps:
    if e["emp_code"] == "EMP0009": continue         # employee with no logs
    for d in days:
        if rnd.random() < 0.25: continue            # missing days
        st = rnd.choice(["PRESENT"]*6+["WFH","ON_DUTY","LEAVE","ABSENT"])
        L = {"emp_code": e["emp_code"], "date": d.isoformat(), "status": st}
        if st in ("LEAVE","ABSENT"):
            L.update(punch_in=None, punch_out=None, work_hours=None, late_minutes=0, overtime_minutes=0, half_day=False)
        else:
            wh = rnd.choice([None, 3.5, 4.49, 4.5, 8.125, 9.12, 7.0, 8.75]) 
            L.update(punch_in=dt(d,4,0), punch_out=None if wh is None else dt(d,13,0), work_hours=wh,
                     late_minutes=rnd.choice([0,0,0,10,10,35,35,45,12]), overtime_minutes=rnd.choice([0,0,30,40]),
                     half_day=(wh is not None and wh < 4.5))
            if rnd.random() < .2: del L["half_day"]
            if rnd.random() < .1: del L["late_minutes"]
        logs.append(L)
logs.append({"emp_code":"GHOST","date":"2026-07-08","status":"PRESENT","late_minutes":999,"work_hours":8.0})

PRES = {"PRESENT","WFH","ON_DUTY"}
def r2(x, p): return float(Decimal(x).quantize(Decimal(1).scaleb(-p), rounding=ROUND_HALF_UP))
def pdays(L): return 0 if L["status"] not in PRES else 0.5 if L.get("half_day") else 1
def wk(ds): return date.fromisoformat(ds).weekday() < 5
def inmonth(L, mo): return L["date"].startswith(mo)

def ref_monthly(code, mo):
    e = next(x for x in emps if x["emp_code"] == code)
    y, mn = map(int, mo.split("-")); first = date(y, mn, 1); last = (date(y + (mn==12), mn % 12 + 1, 1) - timedelta(days=1))
    start = max(first, date.fromisoformat(e["joined_on"]))
    wd = sum(1 for i in range((last-start).days+1) if (start+timedelta(days=i)).weekday() < 5) if start <= last else 0
    ls = [l for l in logs if l["emp_code"] == code and inmonth(l, mo)]
    pd = sum(pdays(l) for l in ls if wk(l["date"]))
    return dict(emp_code=code, month=mo, working_days=wd, present_days=pd, leave_days=sum(l["status"]=="LEAVE" for l in ls),
                late_count=sum((l.get("late_minutes") or 0) > 0 for l in ls), total_late_minutes=sum(l.get("late_minutes") or 0 for l in ls),
                total_overtime_minutes=sum(l.get("overtime_minutes") or 0 for l in ls),
                attendance_pct=r2(Decimal(str(pd))/wd*100, 2) if wd else None)

def ref_summary(mo, dept=None):
    y, mn = map(int, mo.split("-")); last = (date(y + (mn==12), mn % 12 + 1, 1) - timedelta(days=1)).isoformat()
    out = []
    for d in sorted({e["department"] for e in emps}):
        if dept and d != dept: continue
        es = [e for e in emps if e["department"] == d and e["joined_on"] <= last]
        if not es: continue
        codes = {e["emp_code"] for e in es}
        ls = [l for l in logs if l["emp_code"] in codes and inmonth(l, mo)]
        whs = [l["work_hours"] for l in ls if l["status"] in PRES and l.get("work_hours") is not None]
        out.append(dict(department=d, headcount=len(es), present_days=sum(pdays(l) for l in ls if wk(l["date"])),
            avg_work_hours=r2(Decimal(str(sum(Decimal(str(w)) for w in whs)))/len(whs), 2) if whs else None,
            late_count=sum((l.get("late_minutes") or 0) > 0 for l in ls), total_late_minutes=sum(l.get("late_minutes") or 0 for l in ls),
            leave_count=sum(l["status"]=="LEAVE" for l in ls), on_duty_count=sum(l["status"]=="ON_DUTY" for l in ls)))
    return out

def ref_board(mo, limit, dept=None):
    tot = {}
    for l in logs:
        e = next((x for x in emps if x["emp_code"] == l["emp_code"]), None)
        if e is None or not inmonth(l, mo) or (l.get("late_minutes") or 0) <= 0: continue
        if dept and e["department"] != dept: continue
        t = tot.setdefault(e["emp_code"], [0, 0, e]); t[0] += l["late_minutes"]; t[1] += 1
    rows = sorted(tot.items(), key=lambda kv: (-kv[1][0], kv[0]))
    res = []
    for code, (t, n, e) in rows:
        rank = 1 + sum(1 for _, (t2, _, _) in rows if t2 > t)
        if rank <= limit: res.append(dict(rank=rank, emp_code=code, name=e["name"], department=e["department"], total_late_minutes=t, late_count=n))
    return res

def ref_trend(dept, frm, to):
    f, t = date.fromisoformat(frm), date.fromisoformat(to); rows = []
    es = [e for e in emps if e["department"] == dept]; codes = {e["emp_code"] for e in es}
    for i in range((t-f).days+1):
        d = f + timedelta(days=i); ds = d.isoformat()
        hc = sum(1 for e in es if e["joined_on"] <= ds)
        ls = [l for l in logs if l["emp_code"] in codes and l["date"] == ds]
        pc = sum(pdays(l) for l in ls); work = d.weekday() < 5
        rate = r2(Decimal(str(pc))/hc, 4) if work and hc > 0 else None
        rows.append(dict(date=ds, is_working_day=work, headcount=hc, present_count=pc, late_count=sum((l.get("late_minutes") or 0) > 0 for l in ls), attendance_rate=rate))
    for i, r in enumerate(rows):
        w = [x["attendance_rate"] for x in rows[max(0, i-6):i+1] if x["attendance_rate"] is not None]
        r["moving_avg_7d"] = r2(sum(Decimal(str(x)) for x in w)/len(w), 4) if w else None
    return rows


def same(a, b, what):
    if a != b:
        print("MISMATCH", what)
        for x, y in zip(a if isinstance(a, list) else [a], b if isinstance(b, list) else [b]):
            if x != y: print("  got", x, "\n  exp", y); break
        if isinstance(a, list): print("  lens", len(a), len(b))
        sys.exit(1)

assert requests.get(BASE + "/health").status_code == 200, "API not running?"
mdb.employees.insert_many([dict(e) for e in emps]); mdb.attendance_logs.insert_many([dict(l) for l in logs])

for mo in ["2026-07", "2026-06", "2026-08"]:
    for e in emps:
        same(c.get(f"/analytics/employees/{e['emp_code']}/monthly?month={mo}").json(), ref_monthly(e["emp_code"], mo), f"monthly {e['emp_code']} {mo}")
    same(c.get(f"/analytics/departments/summary?month={mo}").json()["items"], ref_summary(mo), f"summary {mo}")
    same(c.get(f"/analytics/departments/summary?month={mo}&department=Ops").json()["items"], ref_summary(mo, "Ops"), f"summary Ops {mo}")
    for lim in (1, 2, 3, 10):
        same(c.get(f"/analytics/leaderboard/late?month={mo}&limit={lim}").json()["items"], ref_board(mo, lim), f"board {mo} {lim}")
        same(c.get(f"/analytics/leaderboard/late?month={mo}&limit={lim}&department=Eng").json()["items"], ref_board(mo, lim, "Eng"), f"board Eng {mo} {lim}")
print("OK  monthly / summary / leaderboard match the Python reference")
for dept in ["Eng", "Ops", "HR"]:
    for frm, to in [("2026-07-01","2026-07-31"), ("2026-06-28","2026-08-05"), ("2026-07-04","2026-07-04"), ("2026-07-29","2026-07-31"), ("2026-08-01","2026-08-02")]:
        got = c.get(f"/analytics/departments/{dept}/trend", params={"from": frm, "to": to}).json()["items"]
        same(got, ref_trend(dept, frm, to), f"trend {dept} {frm} {to}")
print("OK  trend matches the Python reference")

# ---- ties 1,2,2,4,4,6
mdb.attendance_logs.delete_many({})
logs[:] = []
for code, t in zip(["EMP0001","EMP0005","EMP0006","EMP0008","EMP0009","EMP0002"], [50, 40, 40, 30, 30, 10]):
    logs.append({"emp_code": code, "date": "2026-07-08", "status": "PRESENT", "late_minutes": t})
mdb.attendance_logs.insert_many([dict(l) for l in logs])
for lim in range(1, 7):
    same(c.get(f"/analytics/leaderboard/late?month=2026-07&limit={lim}").json()["items"], ref_board("2026-07", lim), f"ties {lim}")
print("OK  leaderboard ties", [r["rank"] for r in c.get("/analytics/leaderboard/late?month=2026-07&limit=6").json()["items"]])

# ---- concurrency
mdb.attendance_logs.delete_many({})
def burst(n, fn):
    out = []; ts = [threading.Thread(target=lambda: out.append(fn())) for _ in range(n)]
    [t.start() for t in ts]; [t.join() for t in ts]; return sorted(out)
IST = timezone(timedelta(hours=5, minutes=30))
ms = lambda *a: int(datetime(*a, tzinfo=IST).timestamp() * 1000)
r = burst(20, lambda: requests.post(BASE + "/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026,7,6,9,40)}).status_code)
assert r.count(201) == 1 and r.count(409) == 19, r
r = burst(20, lambda: requests.post(BASE + "/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026,7,6,18,40)}).status_code)
assert r.count(200) == 1 and r.count(409) == 19, r
r = burst(10, lambda: requests.post(BASE + "/employees", json=dict(emps[0], emp_code="EMP7777", email="race@example.com")).status_code)
assert r.count(201) == 1 and r.count(409) == 9, r
mdb.employees.delete_one({"emp_code": "EMP7777"})
def patch(i): return requests.patch(BASE + "/attendance/EMP0001/2026-07-06", json={"punch_in": ms(2026,7,6,9,0+i), "reason": "concurrent edit", "regularized_by": f"hr{i}"}).status_code
r = burst(8, lambda: patch(random.randint(1, 50)))
hist = requests.get(BASE + "/attendance?emp_code=EMP0001").json()["items"][0]["history"]
assert len(hist) == r.count(200) and set(r) <= {200, 409, 422}, (r, len(hist))
print("OK  concurrency: punch-in / punch-out / create / patch (history entries == successful patches)")

# ---- explain: IXSCAN and no COLLSCAN
q = {"attendance_list": "", "employee_monthly": "emp_code=EMP0001&month=2026-07", "department_summary": "month=2026-07",
     "late_leaderboard": "month=2026-07", "department_trend": "department=Eng&from=2026-07-01&to=2026-07-31"}
for name, qs in q.items():
    for extra in (["", "&department=Eng"] if name in ("department_summary", "late_leaderboard") else [""]):
        txt = json.dumps(requests.get(f"{BASE}/admin/explain/{name}?{qs}{extra}").json())
        assert "IXSCAN" in txt and "COLLSCAN" not in txt, (name, extra)
print("OK  explain: IXSCAN present, no COLLSCAN for all 5 endpoints")

if "--big" in sys.argv:
    print("loading ~100k logs ...")
    docs = []; codes = [f"EMP{i:04d}" for i in range(100, 400)]
    mdb.employees.insert_many([{"emp_code": k, "name": k, "department": ["Eng","Ops","HR","QA"][i % 4], "shift_start": "09:30", "shift_end": "18:30",
                                "joined_on": "2026-01-01", "created_at": datetime.now(timezone.utc), "email": "a@b.co"} for i, k in enumerate(codes)])
    d0 = date(2026, 1, 1)
    for k in codes:
        for i in range(340):
            d = d0 + timedelta(days=i)
            docs.append({"emp_code": k, "date": d.isoformat(), "status": "PRESENT", "punch_in": datetime(d.year, d.month, d.day, 4, tzinfo=timezone.utc),
                         "punch_out": datetime(d.year, d.month, d.day, 13, tzinfo=timezone.utc), "work_hours": 9.0,
                         "late_minutes": random.choice([0, 0, 15, 40]), "overtime_minutes": 0, "half_day": False, "history": []})
    mdb.attendance_logs.insert_many(docs); print(len(docs), "logs loaded")
    for name, qs in [("attendance_list", "?date_from=2026-03-01&date_to=2026-03-31&page=3"), ("attendance_list", "?status=PRESENT"), ("attendance_list", ""),
                     ("analytics/departments/summary", "?month=2026-05"), ("analytics/leaderboard/late", "?month=2026-05"),
                     ("analytics/departments/Eng/trend", "?from=2026-04-01&to=2026-06-30"), ("analytics/employees/EMP0100/monthly", "?month=2026-05")]:
        url = f"{BASE}/{name}{qs}" if name.startswith("analytics") else f"{BASE}/attendance{qs}"
        t = time.time(); r = requests.get(url, params={"from": "2026-04-01", "to": "2026-06-30"} if "trend" in name else None); dt = time.time() - t
        print(f"  {r.status_code}  {dt*1000:7.0f} ms  {url}")
    for name, qs in q.items():
        txt = json.dumps(requests.get(f"{BASE}/admin/explain/{name}?{qs}").json())
        assert "IXSCAN" in txt and "COLLSCAN" not in txt, name
    print("OK  explain still index-only on the big dataset")
print("ALL CHECKS PASSED")
