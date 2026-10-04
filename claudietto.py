"""Claudietto, avvisi automatici."""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
WATCHLIST = HERE / "watchlist.json"
STATE = HERE / "state.json"
PAUSE_SECONDS = 15
LABEL = {"buy": "Compra", "sell": "Vendi", "wait": "Attendi"}


def fetch_csv(symbol, key):
    q = urllib.parse.urlencode({
        "function": "TIME_SERIES_DAILY", "symbol": symbol,
        "outputsize": "compact", "datatype": "csv", "apikey": key,
    })
    with urllib.request.urlopen("https://www.alphavantage.co/query?" + q, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def parse_csv(text):
    lines = text.strip().splitlines()
    if len(lines) < 2 or not lines[0].lower().startswith("timestamp,open,high,low,close"):
        return None
    rows = []
    for line in lines[1:]:
        p = line.split(",")
        try:
            rows.append({"d": p[0], "h": float(p[2]), "l": float(p[3]), "c": float(p[4])})
        except (IndexError, ValueError):
            continue
    rows.sort(key=lambda r: r["d"])
    return rows


def sma(a, n):
    return sum(a[-n:]) / n


def ema_series(a, n):
    k, out, e = 2 / (n + 1), [], a[0]
    for i, v in enumerate(a):
        e = v * k + e * (1 - k) if i else v
        out.append(e)
    return out


def rsi(c, n=14):
    g = l = 0.0
    for i in range(1, n + 1):
        d = c[i] - c[i - 1]
        g, l = (g + d, l) if d > 0 else (g, l - d)
    g, l = g / n, l / n
    for i in range(n + 1, len(c)):
        d = c[i] - c[i - 1]
        g = (g * (n - 1) + max(d, 0)) / n
        l = (l * (n - 1) + max(-d, 0)) / n
    return 100.0 if l == 0 else 100 - 100 / (1 + g / l)


def atr(rows, n=14):
    tr = [max(r["h"] - r["l"], abs(r["h"] - p["c"]), abs(r["l"] - p["c"]))
          for p, r in zip(rows, rows[1:])]
    a = sum(tr[:n]) / n
    for v in tr[n:]:
        a = (a * (n - 1) + v) / n
    return a


def analyze(rows):
    if not rows or len(rows) < 60:
        return None
    c = [r["c"] for r in rows]
    last, prev = rows[-1], rows[-2]
    s20, s50, r, a = sma(c, 20), sma(c, 50), rsi(c), atr(rows)
    e12, e26 = ema_series(c, 12), ema_series(c, 26)
    macd = [x - y for x, y in zip(e12, e26)]
    hist = macd[-1] - ema_series(macd, 9)[-1]
    window = rows[-21:-1]
    hi, lo = max(x["h"] for x in window), min(x["l"] for x in window)
    score = (1 if last["c"] > s50 else -1) + (1 if s20 > s50 else -1) + (1 if hist > 0 else -1)
    score += 1 if r < 30 else (-1 if r > 70 else 0)
    sig = "buy" if score >= 2 else ("sell" if score <= -2 else "wait")
    return {
        "date": last["d"], "close": last["c"], "chg": (last["c"] / prev["c"] - 1) * 100,
        "rsi": r, "hi": hi, "lo": lo, "sig": sig,
        "stop": last["c"] - 2 * a if sig == "buy" else None,
        "target": last["c"] + 3 * a if sig == "buy" else None,
    }


def num(v):
    return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def plan(a):
    if a["sig"] == "buy":
        return f"Ingresso {num(a['close'])}, stop {num(a['stop'])}, target {num(a['target'])}."
    return f"Supporto {num(a['lo'])}, resistenza {num(a['hi'])}."


def send_telegram(text):
    if os.environ.get("DRY_RUN") == "1":
        print(text)
        return
    data = urllib.parse.urlencode({
        "chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text,
    }).encode()
    url = f"https://api.telegram.org/bot{os.environ['TELEGRAM_TOKEN']}/sendMessage"
    with urllib.request.urlopen(url, data=data, timeout=30) as r:
        r.read()


def main():
    key = os.environ.get("ALPHAVANTAGE_KEY")
    if not key:
        sys.exit("Manca ALPHAVANTAGE_KEY")
    full = os.environ.get("FULL_REPORT") == "1"
    watch = json.loads(WATCHLIST.read_text(encoding="utf-8"))
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}

    news, summary, problems, last_date = [], [], [], ""
    for i, item in enumerate(watch):
        sym = item["symbol"].upper()
        if i:
            time.sleep(PAUSE_SECONDS)
        try:
            text = fetch_csv(sym, key)
        except Exception as e:
            problems.append(f"{sym}: dati non raggiungibili ({type(e).__name__})")
            continue
        a = analyze(parse_csv(text))
        if not a:
            problems.append(f"{sym}: nessun dato utilizzabile. {' '.join(text.split())[:120]}")
            continue

        last_date = max(last_date, a["date"])
        old = state.get(sym, {})
        up, down = item.get("alert_up"), item.get("alert_down")
        hit_up = up is not None and a["close"] >= up
        hit_down = down is not None and a["close"] <= down
        summary.append(f"{sym}: {LABEL[a['sig']]}, {num(a['close'])} ({a['chg']:+.2f}%). {plan(a)}")

        if old.get("date") != a["date"]:
            if hit_up and not old.get("hit_up"):
                news.append(f"🔔 {sym}: alert scattato, {num(a['close'])} sopra {num(up)}.")
            if hit_down and not old.get("hit_down"):
                news.append(f"🔔 {sym}: alert scattato, {num(a['close'])} sotto {num(down)}.")
            if old.get("sig") and old["sig"] != a["sig"]:
                news.append(f"🔄 {sym}: da {LABEL[old['sig']]} a {LABEL[a['sig']]}. {plan(a)}")
            elif not old.get("sig"):
                news.append(f"🆕 {sym}: {LABEL[a['sig']]}. {plan(a)}")
        state[sym] = {"date": a["date"], "sig": a["sig"], "hit_up": hit_up, "hit_down": hit_down}

    lines = summary if full else news
    if full and problems or (news and problems):
        lines = lines + ["", "Problemi:"] + problems
    if lines:
        day = "/".join(reversed(last_date.split("-"))) if last_date else ""
        title = "📈 Claudietto" + (f", chiusura del {day}" if day else "")
        send_telegram(title + "\n\n" + "\n".join(lines) + "\n\nSegnali da regole tecniche, non consulenza finanziaria.")
    elif problems:
        print("\n".join(problems))

    STATE.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")


if __name__ == "__main__":
    main()
