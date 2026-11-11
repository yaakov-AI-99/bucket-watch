#!/usr/bin/env python3
"""Bucket Watch resale tracker.

Pulls live eBay listings (asks, "X sold" counts, watchers, bids) and Google Trends
interest through SerpApi's MCP endpoint, then estimates resale profit per bucket.
The SerpApi key is injected by the session's network proxy as a Bearer header;
this script never handles it.

Two ways to reach SerpApi:
  * GitHub Actions / your own computer: set SERPAPI_KEY and it calls serpapi.com directly.
  * Claude cloud session: leave SERPAPI_KEY unset; it uses mcp.serpapi.com, where the
    session's network proxy injects the key.

Usage: python3 tracker.py out.json [--reuse]
"""
import json, os, ssl, statistics, sys, time, urllib.request, urllib.parse, math
from datetime import datetime, timezone

MCP = "https://mcp.serpapi.com/mcp"
EBAY_FEE = 0.1325      # eBay final value fee on item + shipping (most categories)
PER_ORDER_FEE = 0.40

# retail = what you pay at the theater (estimate unless noted); store = closest location to work
ITEMS = [
  {"id":"dune-sandworm","name":"Dune: Part Three Double Sandworm","chain":"AMC","store":"AMC Fantasy 5","sale":"2026-12-17","retail":30,
   "q":"dune sandworm popcorn bucket","must":["sandworm"],"also":["three","part 3","dune 3","double","2026","part three"]},
  {"id":"dune-navigator","name":"Dune: Part Three Navigator","chain":"Regal","store":"Regal Lynbrook","sale":"2026-12-17","retail":35,
   "q":"dune navigator popcorn bucket","must":["navigator"]},
  {"id":"doom-throne","name":"Doomsday: Doom's Throne","chain":"Regal","store":"Regal Lynbrook","sale":"2026-12-17","retail":40,
   "q":"doom throne popcorn bucket","must":["throne","doom"],"exclude":["led","totem","cinemark","skeletor","masters"]},
  {"id":"doom-sentinel","name":"Doomsday: Sentinel Head","chain":"AMC","store":"AMC Fantasy 5","sale":"2026-12-17","retail":40,
   "q":"sentinel popcorn bucket doomsday","must":["sentinel"],"exclude":["cup","drink","vessel"]},
  {"id":"doom-statue","name":"Doomsday: Doctor Doom statue","chain":"All chains","store":"Regal Lynbrook","sale":"2026-12-17","retail":60,
   "q":"doctor doom statue popcorn bucket","must":["doom"],"also":["statue","bust"],"exclude":["throne","led","sentinel","combo"]},
  {"id":"hg-tin","name":"Hunger Games: book tin","chain":"Regal","store":"Regal Lynbrook","sale":"2026-11-18","retail":30,
   "q":"hunger games sunrise reaping popcorn","must":["hunger"],"also":["tin","bucket","book"]},
  {"id":"godzilla","name":"Godzilla Minus Zero giant figure bucket","chain":"AMC / Regal","store":"Regal Lynbrook","sale":"2026-11-06","retail":99,
   "q":"godzilla minus zero popcorn bucket","must":["godzilla"],"also":["giant","3d","19"],"exclude":["ghidorah","tin"]},
  {"id":"cat-fishbowl","name":"Cat in the Hat Fishbowl","chain":"Regal","store":"Regal Lynbrook","sale":"2026-11-05","retail":25,
   "q":"cat in the hat fishbowl popcorn bucket","must":["fish"],"also":["cat","hat","seuss"]},
]
GLOBAL_EXCLUDE = ["poster","sticker","pin ","keychain","magnet","lot of","full set","print","shirt","card","replacement lid","lid only","3d print","stl","decal","photo"]
TRENDS = ["dune popcorn bucket","doomsday popcorn bucket","hunger games popcorn bucket","godzilla popcorn bucket","cat in the hat popcorn bucket"]
DISCOVER_Q = "popcorn bucket 2026"

searches_used = 0

def _ctx():
    ca = "/root/.ccr/ca-bundle.crt"
    return ssl.create_default_context(cafile=ca) if os.path.exists(ca) else ssl.create_default_context()
CTX = _ctx()

KEY = os.environ.get("SERPAPI_KEY", "").strip()

def direct_search(params, timeout=120):
    q = urllib.parse.urlencode({k: v for k, v in params.items() if k != "async"} | {"api_key": KEY})
    with urllib.request.urlopen("https://serpapi.com/search.json?" + q, timeout=timeout, context=CTX) as r:
        d = json.loads(r.read().decode())
    if d.get("error"):
        raise RuntimeError(d["error"])
    return d

def mcp_search(params, timeout=60):
    if KEY:
        return direct_search(params)
    body = json.dumps({"jsonrpc":"2.0","id":1,"method":"tools/call",
        "params":{"name":"search","arguments":{"params":params,"mode":"complete"}}}).encode()
    req = urllib.request.Request(MCP, data=body, method="POST",
        headers={"Content-Type":"application/json","Accept":"application/json, text/event-stream"})
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        raw = r.read().decode()
    if raw.startswith("event:") or "\ndata:" in raw:  # SSE framing
        raw = [l[5:].strip() for l in raw.splitlines() if l.startswith("data:")][-1]
    d = json.loads(raw)
    res = d.get("result", {})
    txt = res.get("content", [{}])[0].get("text", "")
    if res.get("isError"):
        raise RuntimeError(txt[:300])
    return json.loads(txt) if txt.strip().startswith("{") else {}

def fetch_slow(params, label):
    """eBay searches exceed the 30s relay timeout, so submit async, wait, then read the cached result."""
    global searches_used
    for attempt in range(10):
        try:
            out = mcp_search(params)
            if out.get("search_metadata", {}).get("status") == "Success":
                return out
        except Exception as e:
            print(f"  {label}: waiting ({type(e).__name__})", file=sys.stderr)
        time.sleep(30)
    print(f"  {label}: gave up", file=sys.stderr)
    return None

def submit_async(params):
    global searches_used
    if KEY:  # direct calls have no 30s limit; fetch_slow does the real search
        searches_used += 1
        return
    try:
        mcp_search(dict(params, **{"async":"true"}), timeout=30)
        searches_used += 1
    except Exception as e:
        print("  async submit failed:", e, file=sys.stderr)

def price_of(x):
    p = x.get("price") or {}
    if "extracted" in p: return p["extracted"]
    if "from" in p: return p["from"].get("extracted")
    return None

def relevant(item, title):
    t = title.lower()
    if any(w in t for w in GLOBAL_EXCLUDE + item.get("exclude", [])): return False
    if "popcorn" not in t and "bucket" not in t and "tin" not in t: return False
    if not all(w in t for w in item["must"]): return False
    if item.get("also") and not any(w in t for w in item["also"]): return False
    return True

def analyze(item, data):
    rows = []
    for x in (data or {}).get("organic_results", []):
        title = x.get("title") or ""
        p = price_of(x)
        if not p or not relevant(item, title): continue
        sh = x.get("shipping")
        ship = (sh.get("extracted") if isinstance(sh, dict) else 0) or 0
        rows.append({"title":title[:110],"price":round(p,2),"ship":round(ship,2),
            "sold":x.get("extracted_quantity_sold") or 0,"watchers":x.get("extracted_watchers") or 0,
            "bids":x.get("bids") or 0,"format":x.get("buying_format") or "","link":x.get("link")})
    # drop obvious outliers (bundles / joke prices)
    if len(rows) >= 5:
        med = statistics.median(r["price"] for r in rows)
        rows = [r for r in rows if med/3 <= r["price"] <= med*3]
    asks = sorted(r["price"] for r in rows)
    units = sum(r["sold"] for r in rows)
    watchers = sum(r["watchers"] for r in rows)
    bids = sum(r["bids"] if isinstance(r["bids"], int) else 0 for r in rows)
    sold_avg = round(sum(r["price"]*r["sold"] for r in rows)/units, 2) if units else None
    med_ask = round(statistics.median(asks), 2) if asks else None
    med_ship = statistics.median([r["ship"] for r in rows]) if rows else 10
    # what you can realistically get: proven sold price if enough sales, else a discount off asking
    if units >= 3:
        est, basis, conf = sold_avg, f"{units} sold at an average of ${sold_avg:.0f}", "high" if units >= 10 else "medium"
    elif med_ask:
        est, basis, conf = round(med_ask*0.8, 2), f"80% of median ask (${med_ask:.0f}), few confirmed sales", "low"
    else:
        est, basis, conf = None, "no listings yet", "none"
    profit = round(est - EBAY_FEE*(est+med_ship) - PER_ORDER_FEE - item["retail"], 2) if est else None
    gap = round((med_ask - sold_avg)/sold_avg*100) if (med_ask and sold_avg) else None
    demand = math.log1p(units + watchers/5 + bids/2)
    weight = {"high":1.0,"medium":0.8,"low":0.5,"none":0}[conf]
    score = round((profit or 0) * weight * (0.5 + demand), 1)
    verdict = ("buy" if profit and profit >= 30 and conf != "low" else
               "maybe" if profit and profit >= 15 else "skip")
    top = sorted(rows, key=lambda r:(r["sold"], r["watchers"]), reverse=True)[:5]
    return {**{k:item[k] for k in ("id","name","chain","store","sale","retail")},
        "listings":len(rows),"low_ask":asks[0] if asks else None,"high_ask":asks[-1] if asks else None,
        "median_ask":med_ask,"units_sold":units,"sold_avg":sold_avg,"watchers":watchers,"bids":bids,
        "ask_vs_sold_pct":gap,"est_sell":est,"basis":basis,"confidence":conf,"profit":profit,
        "score":score,"verdict":verdict,"top":top,
        "ebay":"https://www.ebay.com/sch/i.html?_nkw="+urllib.parse.quote_plus(item["q"])+"&LH_Sold=1&LH_Complete=1"}

def trends():
    global searches_used
    try:
        d = mcp_search({"engine":"google_trends","q":",".join(TRENDS),"date":"today 3-m","data_type":"TIMESERIES"})
        searches_used += 1
    except Exception as e:
        print("  trends failed:", e, file=sys.stderr); return []
    tl = d.get("interest_over_time", {}).get("timeline_data", [])
    out = []
    for i, q in enumerate(TRENDS):
        s = [ (pt["values"][i].get("extracted_value") or 0) for pt in tl if len(pt.get("values",[]))>i ]
        last7, prev7 = s[-7:], s[-14:-7]
        cur = round(sum(last7)/max(len(last7),1),1)
        prev = sum(prev7)/max(len(prev7),1)
        out.append({"q":q,"current":cur,"change_pct":round((cur-prev)/prev*100) if prev else None,"series":s[-45:]})
    return out

def discover(data):
    rows = []
    for x in (data or {}).get("organic_results", []):
        t = (x.get("title") or "")
        p = price_of(x)
        if not p or any(w in t.lower() for w in GLOBAL_EXCLUDE) or "popcorn" not in t.lower(): continue
        rows.append({"title":t[:110],"price":p,"sold":x.get("extracted_quantity_sold") or 0,
                     "watchers":x.get("extracted_watchers") or 0,"link":x.get("link")})
    rows.sort(key=lambda r:(r["sold"]*3 + r["watchers"]), reverse=True)
    return [r for r in rows if r["sold"] or r["watchers"]][:10]

def main(out_path):
    import urllib.parse  # noqa
    raw_path = os.path.join(os.path.dirname(os.path.abspath(out_path)), "raw_results.json")
    if "--reuse" in sys.argv and os.path.exists(raw_path):
        results = json.load(open(raw_path))
        return finish(out_path, results)
    jobs = [(it["id"], {"engine":"ebay","_nkw":it["q"],"_ipg":"120"}) for it in ITEMS]
    jobs.append(("discover", {"engine":"ebay","_nkw":DISCOVER_Q,"_ipg":"120"}))
    print("Submitting", len(jobs), "eBay searches", file=sys.stderr)
    for _, p in jobs: submit_async(p)
    if not KEY: time.sleep(90)
    results = {}
    for jid, p in jobs:
        results[jid] = fetch_slow(p, jid)
    for jid, p in jobs:  # second pass for any that timed out; they have usually finished server-side by now
        if results[jid] is None:
            results[jid] = fetch_slow(p, jid)
    json.dump(results, open(raw_path, "w"))
    finish(out_path, results)

def finish(out_path, results):
    items = [analyze(it, results.get(it["id"])) for it in ITEMS]
    items.sort(key=lambda r: r["score"], reverse=True)
    data = {"updated":datetime.now(timezone.utc).isoformat(timespec="minutes"),
            "searches_used":searches_used+1,"fee_pct":EBAY_FEE,
            "items":items,"hot":discover(results.get("discover")),"trends":trends()}
    json.dump(data, open(out_path,"w"), indent=1)
    print("Wrote", out_path, "| searches:", data["searches_used"], file=sys.stderr)

if __name__ == "__main__":
    import urllib.parse
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    main(args[0] if args else "data.json")
