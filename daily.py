"""World Tension Reader - daily refresh, self-contained (runs in a cloud routine or on the VPS).
usage: python daily.py STATE_IN.json OUT_DIR
Reads the compact history (STATE_IN), adds any missing GDELT 1.0 days up to yesterday (UTC),
downloads the Caldara-Iacoviello GPR daily index, and writes OUT_DIR/{scores,movers,countries,
gpr_monthly,gpr_recent}.json as {rows, meta} plus OUT_DIR/state.json (the new history).

Scores
  unrest share = articles about fighting/violence/protest events / all articles that day
  crisis share = articles about state-vs-state military threats, force posture, fighting or
                 mass violence between two different countries / all articles that day
  score (0-100) = share of the previous 365 days whose 3-day average was LOWER than today's.
Movers = countries whose unrest or crisis share jumped most vs their own 30-day average."""
import csv, io, json, re, sys, zipfile, pathlib, urllib.parse, urllib.request, datetime as dt
from collections import defaultdict

csv.field_size_limit(10_000_000)
UA = {"User-Agent": "tension-reader/0.2"}
SRC = [{"label": "daily.py", "value": pathlib.Path(__file__).read_text(encoding="utf-8"), "type": "code", "lang": "python"}]
KEEP_COUNTRY_DAYS = 95      # per-country counts kept for the movers / country table
KEEP_DAYS = 560             # daily totals kept for the 365-day ranking


def classify(code, root, quad, a1, a2):
    unrest = quad == "4" or root == "14"
    interstate = a1 and a2 and a1 != a2
    crisis = bool(interstate and (code.startswith("138") or root in ("15", "19", "20")))
    return unrest, crisis


def get(url, timeout=300):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


def process(day):
    blob = get(f"https://data.gdeltproject.org/events/{day}.export.CSV.zip")
    tot = unrest = crisis = 0
    c_un = defaultdict(int); c_cr = defaultdict(int)
    names = defaultdict(lambda: defaultdict(int))
    links = defaultdict(list); urls = defaultdict(set)
    with zipfile.ZipFile(io.BytesIO(blob)) as z, z.open(z.namelist()[0]) as f:
        for row in csv.reader(io.TextIOWrapper(f, encoding="utf-8", errors="replace"), delimiter="\t"):
            if len(row) < 58 or row[25] != "1":          # root events only
                continue
            try:
                n = int(row[33] or 0)
            except ValueError:
                continue
            code, root, quad, cc = row[26], row[28], row[29], row[51]
            if row[49] == "1" and cc:
                names[cc][row[50]] += 1
            u, c = classify(code, root, quad, row[7], row[17])
            tot += n
            if u:
                unrest += n
                if cc:
                    c_un[cc] += n; links[(cc, "u")].append((n, row[57], code)); urls[(cc, "u")].add(row[57])
            if c:
                crisis += n
                if cc:
                    c_cr[cc] += n; links[(cc, "c")].append((n, row[57], code)); urls[(cc, "c")].add(row[57])
    top = {}
    for (cc, kind), lst in links.items():
        seen, keep = set(), []
        for n, u, code in sorted(lst, reverse=True):
            if u not in seen:
                seen.add(u); keep.append([n, u, code])
            if len(keep) == 5:
                break
        top[f"{cc}|{kind}"] = keep
    return {"day": day, "total": tot, "unrest": unrest, "crisis": crisis,
            "c_unrest": dict(c_un), "c_crisis": dict(c_cr), "top": top,
            "n_links": {f"{cc}|{k}": len(v) for (cc, k), v in urls.items()},
            "names": {cc: max(v, key=v.get) for cc, v in names.items()}}


CAMEO = {"138": "threat of military force", "15": "military posturing", "190": "fighting", "193": "small-arms fighting",
         "194": "artillery or tank fire", "195": "air strikes", "196": "ceasefire breach", "18": "assault", "180": "assault",
         "181": "abduction", "182": "physical assault", "183": "bombing", "1831": "suicide bombing", "1832": "car bombing",
         "1833": "roadside bombing", "186": "assassination", "14": "protest", "141": "demonstration", "145": "violent protest",
         "1451": "violent protest", "20": "mass violence", "202": "mass killing", "17": "coercion", "173": "arrests", "175": "repression"}


def label(code):
    for k in (code, code[:3], code[:2]):
        if k in CAMEO:
            return CAMEO[k]
    return "conflict event"


def headline(url):
    seg = [s for s in urllib.parse.urlparse(url).path.rstrip("/").split("/") if s]
    best = max(seg, key=lambda s: s.count("-") + s.count("_"), default="")
    best = re.sub(r"\.(html?|php|aspx?|cms)$", "", best)
    best = re.sub(r"[-_]+", " ", best)
    best = re.sub(r"\b\d{5,}\b|\b[a-f0-9]{8,}\b", "", best).strip()
    if len(best) < 12:
        return urllib.parse.urlparse(url).netloc.replace("www.", "")
    return best[:1].upper() + best[1:]


def pct_rank(hist, x):
    return round(100 * sum(1 for h in hist if h < x) / len(hist)) if hist else None


def write(out, name, rows, meta):
    (out / name).write_text(json.dumps({"rows": rows, "meta": meta + SRC}), encoding="utf-8")


def main():
    state = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
    out = pathlib.Path(sys.argv[2]); out.mkdir(parents=True, exist_ok=True)
    days = state["days"]                      # {YYYYMMDD: {total, unrest, crisis, c_unrest?, c_crisis?}}
    last_day = max(days)
    yesterday = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)
    d = dt.datetime.strptime(last_day, "%Y%m%d").date() + dt.timedelta(days=1)
    added = []
    while d <= yesterday and len(added) < 7:
        key = d.strftime("%Y%m%d")
        try:
            rec = process(key)
        except Exception as e:
            print("skip", key, e)
            d += dt.timedelta(days=1); continue
        if rec["total"] > 50000:
            days[key] = {k: rec[k] for k in ("total", "unrest", "crisis", "c_unrest", "c_crisis")}
            state["last"] = {k: rec[k] for k in ("day", "top", "n_links")}
            state["names"].update(rec["names"])
            added.append(key)
        d += dt.timedelta(days=1)
    keys = sorted(days)
    for k in keys[:-KEEP_DAYS]:
        days.pop(k)
    keys = sorted(days)
    for k in keys[:-KEEP_COUNTRY_DAYS]:
        days[k].pop("c_unrest", None); days[k].pop("c_crisis", None)
    print("added", added, "latest", keys[-1])

    recs = [days[k] for k in keys]
    names = state["names"]
    un = [100 * r["unrest"] / r["total"] for r in recs]
    cr = [100 * r["crisis"] / r["total"] for r in recs]

    def ma(xs, i, n=3):
        w = xs[max(0, i - n + 1): i + 1]
        return sum(w) / len(w)

    rows = []
    for i, k in enumerate(keys):
        u3, c3 = ma(un, i), ma(cr, i)
        hu = [ma(un, j) for j in range(max(0, i - 365), i)]
        hc = [ma(cr, j) for j in range(max(0, i - 365), i)]
        ok = len(hu) >= 180
        rows.append({"date": f"{k[:4]}-{k[4:6]}-{k[6:]}", "unrest_share": round(un[i], 3), "crisis_share": round(cr[i], 4),
                     "unrest_3d": round(u3, 3), "crisis_3d": round(c3, 4),
                     "unrest_score": pct_rank(hu, u3) if ok else None, "crisis_score": pct_rank(hc, c3) if ok else None,
                     "articles": recs[i]["total"]})
    rows = [r for r in rows if r["unrest_score"] is not None]
    write(out, "scores.json", rows, [{"label": "Source", "value": "GDELT 1.0 daily event files, data.gdeltproject.org/events (root events only, weighted by article count)"}])

    last, prev = recs[-1], recs[-31:-1]
    lastmeta = state["last"]

    def sh(r, key, cc):
        return 100 * r.get(key, {}).get(cc, 0) / r["total"]
    movers = []
    for kind, key, short in (("Unrest", "c_unrest", "u"), ("Crisis", "c_crisis", "c")):
        for cc in last[key]:
            nl = lastmeta["n_links"].get(f"{cc}|{short}", 0)
            if nl < 10:
                continue
            today = sh(last, key, cc)
            avg = sum(sh(r, key, cc) for r in prev) / max(1, len(prev))
            if today < 1.5 * avg:
                continue
            links = lastmeta["top"].get(f"{cc}|{short}", [])
            top = links[0] if links else None
            movers.append({"kind": kind, "country": names.get(cc, cc), "code": cc,
                           "share_today": round(today, 3), "share_avg30": round(avg, 3),
                           "change_pts": round(today - avg, 3), "ratio": round(today / avg, 1) if avg else None,
                           "articles": last[key].get(cc, 0), "distinct_links": nl,
                           "what": label(top[2]) if top else "", "headline": headline(top[1]) if top else "",
                           "link": top[1] if top else ""})
    movers.sort(key=lambda m: -m["change_pts"])
    out_m = []
    for kind in ("Crisis", "Unrest"):
        out_m += [m for m in movers if m["kind"] == kind][:5]
    write(out, "movers.json", out_m, [{"label": "Day", "value": keys[-1]},
          {"label": "Rule", "value": "countries (where the event happened) whose share of that day's news rose to at least 1.5x their own average for the previous 30 days, backed by at least 10 different articles; ranked by the rise in percentage points"}])

    ctab = []
    for cc, _ in sorted(last["c_unrest"].items(), key=lambda x: -x[1])[:15]:
        hist = [sh(r, "c_unrest", cc) for r in recs[-91:-1]]
        today = sh(last, "c_unrest", cc)
        ctab.append({"country": names.get(cc, cc), "code": cc, "unrest_share": round(today, 2),
                     "unrest_avg30": round(sum(hist[-30:]) / 30, 2), "level_90d": pct_rank(hist, today),
                     "crisis_share": round(sh(last, "c_crisis", cc), 2)})
    write(out, "countries.json", ctab, [{"label": "Day", "value": keys[-1]},
          {"label": "level_90d", "value": "share of that country's previous 90 days with a lower share of unrest news"}])

    import pandas as pd
    xls = out / "gpr_daily.xls"
    xls.write_bytes(get("https://www.matteoiacoviello.com/gpr_files/data_gpr_daily_recent.xls"))
    g = pd.read_excel(xls)
    g = g[["date", "GPRD", "GPRD_ACT", "GPRD_THREAT", "GPRD_MA7"]].dropna(subset=["GPRD"])
    g["date"] = pd.to_datetime(g["date"])
    m = g.set_index("date")["GPRD"].resample("MS").mean().dropna()
    write(out, "gpr_monthly.json", [{"month": x.strftime("%Y-%m"), "gpr": round(float(v), 1)} for x, v in m.items()],
          [{"label": "Source", "value": "Caldara and Iacoviello Geopolitical Risk Index (daily), matteoiacoviello.com/gpr.htm; monthly mean of the daily index; 100 = the 1985-2019 average"},
           {"label": "Long-run daily mean", "value": f"{float(g['GPRD'].mean()):.1f}"}])
    rec = g[g["date"] >= g["date"].max() - pd.Timedelta(days=180)]
    write(out, "gpr_recent.json", [{"date": x.strftime("%Y-%m-%d"), "gpr": round(float(a), 1), "acts": round(float(b), 1),
                                    "threats": round(float(c), 1), "gpr_ma7": round(float(e), 1)}
                                   for x, a, b, c, e in rec[["date", "GPRD", "GPRD_ACT", "GPRD_THREAT", "GPRD_MA7"]].itertuples(index=False)],
          [{"label": "Source", "value": "Caldara and Iacoviello GPR daily index; acts = actual events, threats = threats and build-ups"}])
    xls.unlink()
    (out / "state.json").write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")
    print("latest", rows[-1], "| gpr", g["date"].max().date())


if __name__ == "__main__":
    main()
