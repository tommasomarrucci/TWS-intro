#!/usr/bin/env python3
"""
Scarica catene di opzioni da TWS / IB Gateway e le salva nel database options.db
(SQLite, accanto a questo script). Options Lab lo apre con "Apri database".

Esempi:
  python3 ibkr_chain_api.py NFLX
  python3 ibkr_chain_api.py NFLX NVDA SZG          # più titoli nello stesso giro
  python3 ibkr_chain_api.py NVDA --until 2027-06-30
  python3 ibkr_chain_api.py NVDA --all-strikes     # anche gli strike lontanissimi (più lento)
  python3 ibkr_chain_api.py SZG --csv              # salva anche il CSV
  python3 ibkr_chain_api.py NDA.DE AG:CAD          # titolo europeo / valuta precisa

Borsa, valuta ed exchange delle opzioni vengono trovati da soli. Se il ticker è ambiguo
lo script ti mostra l'elenco e scegli il numero; oppure scrivi TICKER.DE (Xetra),
TICKER.IT, .FR, .NL, .UK, .CH o TICKER:EUR per indicare valuta/borsa.

Requisiti: TWS aperto e loggato, API attiva (Global Configuration > API > Settings:
"Enable ActiveX and Socket Clients", porta 7496 live / 7497 paper), pip install ib_async
"""
import argparse
import asyncio
import csv
import datetime as dt
import math
import os
import sqlite3
import sys
import time
from copy import copy

from ib_async import IB, Option, Stock

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
HERE = os.path.dirname(os.path.abspath(__file__))

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticker TEXT NOT NULL,
  taken_at TEXT NOT NULL,          -- data e ora locali dello scarico
  snap_date TEXT NOT NULL,         -- AAAA-MM-GG
  spot REAL, currency TEXT, multiplier TEXT,
  stock_exchange TEXT, opt_exchange TEXT, trading_class TEXT,
  n_contracts INTEGER, seconds REAL
);
CREATE TABLE IF NOT EXISTS quotes(
  snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
  expiry TEXT NOT NULL,            -- AAAA-MM-GG
  strike REAL NOT NULL,
  right TEXT NOT NULL,             -- C o P
  bid REAL, bid_size REAL, ask REAL, ask_size REAL, last REAL, close REAL,
  PRIMARY KEY(snapshot_id, expiry, strike, right)
);
CREATE INDEX IF NOT EXISTS ix_snap_ticker ON snapshots(ticker, taken_at);
"""


def num(x):
    """Prezzo/quantità pulito: None se mancante. IB usa NaN o -1 per 'nessun dato'."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or x < 0:
        return None
    return x


def fmt(x):
    return "" if x is None else f"{x:g}"


SUFFIX = {  # ticker.XX -> (valuta, borsa del titolo)
    "US": ("USD", None), "DE": ("EUR", "IBIS"), "XETRA": ("EUR", "IBIS"), "IT": ("EUR", "BVME"),
    "MI": ("EUR", "BVME"), "FR": ("EUR", "SBF"), "PA": ("EUR", "SBF"), "NL": ("EUR", "AEB"),
    "AS": ("EUR", "AEB"), "ES": ("EUR", "BM"), "UK": ("GBP", "LSE"), "L": ("GBP", "LSE"),
    "CH": ("CHF", "EBS"), "SW": ("CHF", "EBS"), "CA": ("CAD", None), "TO": ("CAD", None),
}


def parse_symbol(raw, a):
    """NDA, NDA.DE, NDA:EUR -> (ticker, opzioni con valuta/borsa aggiornate)."""
    a = copy(a)
    s = raw.strip().upper()
    if ":" in s:
        s, a.currency = s.split(":", 1)
    elif "." in s and s.rsplit(".", 1)[1] in SUFFIX:
        s, suf = s.rsplit(".", 1)
        cur, ex = SUFFIX[suf]
        a.currency = a.currency or cur
        a.stock_exchange = a.stock_exchange or ex
    return s, a


def find_stock(ib, sym, a):
    """Usa le indicazioni date; altrimenti chiede a IB tutte le quotazioni del ticker e, se ambiguo, chiede a te."""
    if a.stock_exchange and a.currency:
        s = Stock(sym, a.stock_exchange, a.currency, primaryExchange=a.primary)
        if ib.qualifyContracts(s):
            return s
        raise RuntimeError(f"{sym} non trovato su {a.stock_exchange}/{a.currency}.")
    det = ib.reqContractDetails(Stock(sym, "", a.currency or "", primaryExchange=a.primary))
    if not det:  # alcuni TWS vogliono un exchange: prova SMART
        det = ib.reqContractDetails(Stock(sym, "SMART", a.currency or ""))
    if not det:
        raise RuntimeError(f"Ticker {sym} non trovato. Prova {sym}.DE, {sym}:EUR o --currency/--stock-exchange.")
    opts = {}
    for d in det:
        c = d.contract
        opts.setdefault(c.conId, (d.longName, c.primaryExchange or c.exchange, c.currency))
    items = list(opts.items())
    # scelta predefinita: USD se c'è, altrimenti la prima
    default = next((k for k, (_, _, cur) in enumerate(v for _, v in items) if cur == "USD"), 0)
    if len(items) > 1:
        print(f"  Il ticker {sym} corrisponde a più titoli:")
        for k, (cid, (name, px, cur)) in enumerate(items, 1):
            print(f"    {k}) {name} · {px} · {cur}" + ("   <- predefinito" if k - 1 == default else ""))
        if sys.stdin.isatty() and not a.auto:
            ans = input(f"  Quale? [1-{len(items)}, Invio = {default + 1}]: ").strip()
            if ans.isdigit() and 1 <= int(ans) <= len(items):
                default = int(ans) - 1
    cid, (name, px, cur) = items[default]
    s = Stock(conId=cid, exchange=a.stock_exchange or "SMART")
    if not ib.qualifyContracts(s):
        s = Stock(conId=cid, exchange=px)
        if not ib.qualifyContracts(s):
            raise RuntimeError(f"Non riesco a qualificare {sym} ({name}).")
    print(f"  Scelto: {name} · {px} · {cur}")
    return s


def get_spot(ib, stk, a):
    """Prezzo del titolo: dati in tempo reale, poi ritardati/congelati se non sei abbonato a quella borsa."""
    for mdt in ([3] if a.delayed else [1, 4]):
        ib.reqMarketDataType(mdt)
        t = ib.reqMktData(stk, "", False, False)
        wait_quotes(ib, [t], a.wait)
        spot = num(t.marketPrice()) or num(t.last) or num(t.close)
        ib.cancelMktData(stk)
        if spot:
            if mdt == 4:
                print("  (spot da dati ritardati: per il titolo non hai l'abbonamento in tempo reale)")
            break
    ib.reqMarketDataType(3 if a.delayed else 1)
    return spot


def parity_spot(quotes, r):
    """Spot implicito: C - P + K*e^(-rT) sullo strike con call e put più vicine, prima scadenza >= 7 giorni."""
    today = dt.date.today()
    by_exp = {}
    for (e, k, rt), q in quotes.items():
        by_exp.setdefault(e, {}).setdefault(k, {})[rt] = q
    for e in sorted(by_exp):
        d = dt.date(int(e[:4]), int(e[4:6]), int(e[6:8]))
        T = (d - today).days / 365
        if T < 7 / 365:
            continue
        best = None
        for k, s in by_exp[e].items():
            c, p = s.get("C"), s.get("P")
            if not c or not p or None in (c[0], c[2], p[0], p[2]):
                continue
            cm, pm = (c[0] + c[2]) / 2, (p[0] + p[2]) / 2
            if best is None or abs(cm - pm) < best[0]:
                best = (abs(cm - pm), cm - pm + k * math.exp(-r * T))
        if best:
            return best[1]
    return None


def wait_quotes(ib, tickers, max_wait):
    """Aspetta finché quasi tutti i contratti hanno ricevuto almeno un prezzo (o scade il tempo)."""
    t0 = time.time()
    ib.sleep(0.5)
    while time.time() - t0 < max_wait:
        got = sum(1 for t in tickers if any(num(v) is not None for v in (t.bid, t.ask, t.last, t.close)))
        if got >= 0.97 * len(tickers):
            break
        ib.sleep(0.25)


def download(ib, sym, a):
    t_start = time.time()
    stk = find_stock(ib, sym, a)
    print(f"  Titolo: {sym} · {stk.primaryExchange or stk.exchange} · {stk.currency}")

    spot = get_spot(ib, stk, a)
    spot_src = "TWS"
    if spot:
        print(f"  Spot {spot}")
    else:
        print("  Spot non disponibile da TWS (manca l'abbonamento alla borsa del titolo): "
              "lo stimo dalle opzioni con la parità put-call.")

    allch = ib.reqSecDefOptParams(stk.symbol, "", stk.secType, stk.conId)
    if not allch:
        raise RuntimeError(f"Nessuna opzione quotata su {sym}.")
    opt_ex = a.opt_exchange
    if not opt_ex:
        exs = {c.exchange for c in allch}
        opt_ex = "SMART" if "SMART" in exs else max(allch, key=lambda c: len(c.expirations)).exchange
    chains = [c for c in allch if c.exchange == opt_ex]
    if not chains:
        raise RuntimeError(f"Nessuna catena su {opt_ex}. Disponibili: {sorted({c.exchange for c in allch})}")
    chain = next((c for c in chains if c.tradingClass == sym), max(chains, key=lambda c: len(c.expirations)))
    exps = sorted(chain.expirations)
    if a.until:
        lim = a.until.replace("-", "")
        exps = [e for e in exps if e <= lim]
    center = spot
    if not center:  # centro provvisorio: strike mediano della prima scadenza, intervallo più largo
        first = ib.reqContractDetails(Option(sym, exps[0], exchange=opt_ex, currency=stk.currency,
                                             tradingClass=chain.tradingClass))
        ks = sorted(d.contract.strike for d in first) or sorted(chain.strikes)
        center = ks[len(ks) // 2]
    rng = a.range if spot else max(a.range, 0.7)
    lo, hi = (0, float("inf")) if a.all_strikes else (center * (1 - rng), center * (1 + rng))
    print(f"  Catena {chain.tradingClass} su {chain.exchange}: {len(exps)} scadenze"
          + ("" if a.all_strikes else f", strike tra {lo:.2f} e {hi:.2f}"))

    # contratti realmente quotati: tutte le scadenze in parallelo
    async def details_all():
        reqs = [ib.reqContractDetailsAsync(Option(sym, e, exchange=opt_ex, currency=stk.currency,
                                                  tradingClass=chain.tradingClass)) for e in exps]
        return await asyncio.gather(*reqs)
    det_lists = ib.run(details_all())
    contracts = [d.contract for det in det_lists for d in det
                 if d.contract.right in ("C", "P") and lo <= d.contract.strike <= hi]
    if not contracts:
        raise RuntimeError("Nessun contratto trovato.")
    n = len(contracts)
    print(f"  {n} contratti · tempo stimato ~{max(1, round(n * 2 / 45 / 60 + n / a.batch * 1.0 / 60))} min "
          "(TWS accetta circa 45 richieste al secondo)")

    quotes = {}
    for i in range(0, n, a.batch):
        block = contracts[i:i + a.batch]
        tks = [ib.reqMktData(c, "", False, False) for c in block]
        wait_quotes(ib, tks, a.wait)
        for c, tk in zip(block, tks):
            b, ak = num(tk.bid), num(tk.ask)
            if b is not None and ak is not None and b > ak:  # quote incrociata: scarta
                b = ak = None
            quotes[(c.lastTradeDateOrContractMonth, c.strike, c.right)] = (
                b, num(tk.bidSize), ak, num(tk.askSize), num(tk.last), num(tk.close))
            ib.cancelMktData(c)
        done = min(i + a.batch, n)
        print(f"\r  prezzi {done}/{n}", end="", flush=True)
    print()
    if not spot:
        spot = parity_spot(quotes, 0.02 if stk.currency == "EUR" else 0.04)
        spot_src = "parità put-call"
        if not spot:
            spot = float(input(f"  Non riesco a stimare lo spot di {sym}: inseriscilo a mano: ").replace(",", "."))
            spot_src = "a mano"
        print(f"  Spot {spot:.4f} ({spot_src})")
    return dict(sym=sym, stk=stk, spot=spot, chain=chain, opt_ex=opt_ex, quotes=quotes, spot_src=spot_src,
                seconds=round(time.time() - t_start, 1))


def save_db(path, r):
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    now = dt.datetime.now()
    cur = con.execute(
        "INSERT INTO snapshots(ticker,taken_at,snap_date,spot,currency,multiplier,stock_exchange,opt_exchange,"
        "trading_class,n_contracts,seconds) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (r["sym"], now.isoformat(timespec="seconds"), now.date().isoformat(), r["spot"], r["stk"].currency,
         str(r["chain"].multiplier), r["stk"].primaryExchange or r["stk"].exchange, r["opt_ex"],
         r["chain"].tradingClass, len(r["quotes"]), r["seconds"]))
    sid = cur.lastrowid
    con.executemany(
        "INSERT INTO quotes VALUES(?,?,?,?,?,?,?,?,?,?)",
        [(sid, f"{e[:4]}-{e[4:6]}-{e[6:8]}", k, rt, *q) for (e, k, rt), q in r["quotes"].items()])
    con.commit()
    con.close()
    return sid


def save_csv(r, out=None):
    today = dt.date.today()
    rows = []
    for (e, k) in sorted({(e, k) for (e, k, _) in r["quotes"]}):
        d = dt.date(int(e[:4]), int(e[4:6]), int(e[6:8]))
        row = [f"{MONTHS[d.month - 1]}{d.day:02d}-{d.year}", (d - today).days, fmt(k)]
        for right in ("C", "P"):
            b, bs, ak, asz, last, close = r["quotes"].get((e, k, right), (None,) * 6)
            is_close = "N"
            if last is None and close is not None:
                last, is_close = close, "Y"
            chg = "" if (last is None or close is None or is_close == "Y") else fmt(round(last - close, 4))
            row += [fmt(b), fmt(bs), fmt(ak), fmt(asz), fmt(last), is_close, chg]
        rows.append(row)
    path = out or os.path.join(HERE, f"{r['sym']}_option_chain_{today.isoformat()}.csv")
    with open(path, "w", newline="") as f:
        f.write(f"# {r['sym']} option chain, IBKR API, extracted {dt.datetime.now().isoformat(timespec='seconds')}, "
                f"underlying ~{r['spot']}, currency {r['stk'].currency}, multiplier {r['chain'].multiplier}\n")
        w = csv.writer(f)
        w.writerow(["expiry", "days_to_exp", "strike",
                    "call_bid", "call_bid_size", "call_ask", "call_ask_size", "call_last", "call_last_is_close", "call_change",
                    "put_bid", "put_bid_size", "put_ask", "put_ask_size", "put_last", "put_last_is_close", "put_change"])
        w.writerows(rows)
    return path


def summary(r):
    per = {}
    for (e, k, rt), q in r["quotes"].items():
        s = per.setdefault(e, [set(), 0])
        s[0].add(k)
        s[1] += q[0] is not None and q[2] is not None
    print("  Scadenza: strike / opzioni con bid e ask")
    for e in sorted(per):
        ks, ok = per[e]
        flag = "  <-- pochi prezzi" if ok < 0.3 * 2 * len(ks) else ""
        print(f"    {e[:4]}-{e[4:6]}-{e[6:8]}: {len(ks)} / {ok}{flag}")


def main():
    ap = argparse.ArgumentParser(description="Catene opzioni IBKR -> options.db per Options Lab")
    ap.add_argument("symbols", nargs="+", help="uno o più ticker, es. NFLX NVDA SZG")
    ap.add_argument("--stock-exchange", default=None, help="facoltativo: exchange del titolo (es. SMART, IBIS)")
    ap.add_argument("--primary", default="", help="facoltativo: primary exchange, es. NASDAQ")
    ap.add_argument("--currency", default=None, help="facoltativo: valuta, es. USD o EUR")
    ap.add_argument("--opt-exchange", default=None, help="facoltativo: exchange delle opzioni (es. SMART, EUREX)")
    ap.add_argument("--port", type=int, default=7496, help="7496 TWS live, 7497 TWS paper, 4001/4002 IB Gateway")
    ap.add_argument("--client-id", type=int, default=27)
    ap.add_argument("--until", default=None, help="ultima scadenza da scaricare, AAAA-MM-GG")
    ap.add_argument("--range", type=float, default=0.5, help="strike entro ±questa frazione dello spot (default 0.5 = ±50%%)")
    ap.add_argument("--all-strikes", action="store_true", help="scarica tutti gli strike (più lento)")
    ap.add_argument("--batch", type=int, default=90, help="contratti per blocco (< tue linee dati, di solito 100)")
    ap.add_argument("--wait", type=float, default=2.5, help="attesa massima dei prezzi per blocco, in secondi")
    ap.add_argument("--auto", action="store_true", help="se il ticker è ambiguo non chiedere: usa la scelta predefinita")
    ap.add_argument("--delayed", action="store_true", help="usa dati ritardati se non hai l'abbonamento")
    ap.add_argument("--db", default=os.path.join(HERE, "options.db"), help="database SQLite di uscita")
    ap.add_argument("--csv", action="store_true", help="salva anche un CSV per titolo")
    a = ap.parse_args()

    ib = IB()
    errors = {}

    def on_error(reqId, code, msg, contract=None, *rest):
        if code in (2104, 2106, 2107, 2108, 2158, 2119, 200, 10090, 10167):  # informativi / contratto inesistente
            return
        if code not in errors:
            print(f"  [IB {code}] {msg}")
        errors[code] = errors.get(code, 0) + 1

    ib.errorEvent += on_error
    try:
        ib.connect("127.0.0.1", a.port, clientId=a.client_id, timeout=15, readonly=True)
    except Exception as e:
        sys.exit(f"Connessione a TWS fallita sulla porta {a.port}: {e}\n"
                 "Controlla che TWS sia aperto, che l'API sia attiva e la porta giusta (7496 live, 7497 paper).")
    ib.reqMarketDataType(3 if a.delayed else 1)

    for raw in a.symbols:
        sym, aa = parse_symbol(raw, a)
        print(f"\n== {sym} ==")
        try:
            r = download(ib, sym, aa)
        except RuntimeError as e:
            print(f"  ERRORE: {e}")
            continue
        sid = save_db(a.db, r)
        summary(r)
        print(f"  Salvato in {a.db} (snapshot {sid}, {r['seconds']} s)")
        if a.csv:
            print(f"  CSV: {save_csv(r)}")
    ib.disconnect()
    if errors.get(10167) or errors.get(354) or errors.get(10089):
        print("\nATTENZIONE: TWS segnala dati non abbonati o ritardati per alcuni contratti.")


if __name__ == "__main__":
    main()
