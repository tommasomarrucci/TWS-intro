#!/usr/bin/env python3
"""
Scarica catene di opzioni da TWS / IB Gateway e le accumula nel database options.db
(SQLite, accanto a questo script). Ogni scarico è uno snapshot con data e ora: i dati
vecchi restano, quindi il database diventa uno storico. Options Lab lo apre con lab.py.

Esempi:
  python3 ibkr_chain_api.py NFLX
  python3 ibkr_chain_api.py NFLX NVDA SZG.DE       # più titoli nello stesso giro (stesso run)
  python3 ibkr_chain_api.py NVDA --until 2027-06-30
  python3 ibkr_chain_api.py NVDA --all-strikes     # anche gli strike lontanissimi
  python3 ibkr_chain_api.py NDA.DE --greeks        # aspetta anche IV e greche calcolate da IB

Ticker: NFLX, NDA.DE (Xetra), .IT .FR .NL .UK .CH, oppure NDA:EUR. Se è ambiguo
lo script mostra l'elenco e scegli il numero (--auto per non chiedere).

Requisiti: TWS aperto e loggato, API attiva (Global Configuration > API > Settings:
"Enable ActiveX and Socket Clients", porta 7496 live / 7497 paper), pip install ib_async

Velocità: TWS accetta circa 50 messaggi al secondo per connessione e ogni opzione ne
richiede 2 (apri e chiudi il prezzo), quindi il massimo teorico è ~22 opzioni al secondo.
Lo script ci si avvicina tenendo sempre piene le linee dati (finestra scorrevole) e
riusando l'elenco dei contratti già scaricato nello stesso giorno.
"""
import argparse
import asyncio
import datetime as dt
import math
import os
import sqlite3
import sys
import time
from copy import copy

from ib_async import IB, Option, Stock

HERE = os.path.dirname(os.path.abspath(__file__))
MSG_PER_SEC = 44  # sotto il limite di 50 di TWS, con margine

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT,                     -- stesso valore per i titoli scaricati con un solo comando
  ticker TEXT NOT NULL,
  taken_at TEXT NOT NULL,          -- data e ora locali di fine scarico
  snap_date TEXT NOT NULL,         -- AAAA-MM-GG
  spot REAL, spot_source TEXT, currency TEXT, multiplier TEXT,
  stock_exchange TEXT, opt_exchange TEXT, trading_class TEXT, con_id INTEGER,
  n_contracts INTEGER, n_two_sided INTEGER, seconds REAL
);
CREATE TABLE IF NOT EXISTS quotes(
  snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
  expiry TEXT NOT NULL,            -- AAAA-MM-GG
  strike REAL NOT NULL,
  right TEXT NOT NULL,             -- C o P
  bid REAL, bid_size REAL, ask REAL, ask_size REAL, last REAL, close REAL, volume REAL,
  iv REAL, delta REAL, gamma REAL, vega REAL, theta REAL, und_price REAL,  -- modello IB, se arrivato
  PRIMARY KEY(snapshot_id, expiry, strike, right)
);
CREATE TABLE IF NOT EXISTS contracts(          -- cache dell'elenco contratti
  ticker TEXT, opt_exchange TEXT, trading_class TEXT, con_id INTEGER PRIMARY KEY,
  expiry TEXT, strike REAL, right TEXT, exchange TEXT, currency TEXT, multiplier TEXT,
  fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_snap_ticker ON snapshots(ticker, taken_at);
CREATE INDEX IF NOT EXISTS ix_contracts ON contracts(ticker, opt_exchange, trading_class);
"""

SUFFIX = {  # ticker.XX -> (valuta, borsa del titolo)
    "US": ("USD", None), "DE": ("EUR", "IBIS"), "XETRA": ("EUR", "IBIS"), "IT": ("EUR", "BVME"),
    "MI": ("EUR", "BVME"), "FR": ("EUR", "SBF"), "PA": ("EUR", "SBF"), "NL": ("EUR", "AEB"),
    "AS": ("EUR", "AEB"), "ES": ("EUR", "BM"), "UK": ("GBP", "LSE"), "L": ("GBP", "LSE"),
    "CH": ("CHF", "EBS"), "SW": ("CHF", "EBS"), "CA": ("CAD", None), "TO": ("CAD", None),
}


# ---------------------------------------------------------------- utilità
def num(x):
    """Valore pulito: None se mancante (IB usa NaN o -1 per 'nessun dato')."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or x < 0:
        return None
    return x


def numg(x):
    """Come num ma ammette valori negativi (delta, theta)."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) or abs(x) > 1e9 else x


class Pacer:
    """Token bucket: non superare MSG_PER_SEC messaggi al secondo verso TWS."""

    def __init__(self, rate, burst=4):
        self.rate, self.burst, self.tokens, self.t = rate, burst, burst, time.time()

    def take(self, n=1):
        now = time.time()
        self.tokens = min(self.burst, self.tokens + (now - self.t) * self.rate)
        self.t = now
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False


def parse_symbol(raw, a):
    """NDA, NDA.DE, NDA:EUR -> (ticker, argomenti con valuta/borsa aggiornate)."""
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


# ---------------------------------------------------------------- titolo e spot
def find_stock(ib, sym, a):
    if a.stock_exchange and a.currency:
        s = Stock(sym, a.stock_exchange, a.currency, primaryExchange=a.primary)
        if ib.qualifyContracts(s):
            return s
        raise RuntimeError(f"{sym} non trovato su {a.stock_exchange}/{a.currency}.")
    det = ib.reqContractDetails(Stock(sym, "", a.currency or "", primaryExchange=a.primary))
    if not det:
        det = ib.reqContractDetails(Stock(sym, "SMART", a.currency or ""))
    if not det:
        raise RuntimeError(f"Ticker {sym} non trovato. Prova {sym}.DE, {sym}:EUR o --currency/--stock-exchange.")
    opts = {}
    for d in det:
        c = d.contract
        opts.setdefault(c.conId, (d.longName, c.primaryExchange or c.exchange, c.currency))
    items = list(opts.items())
    default = next((k for k, (_, v) in enumerate(items) if v[2] == "USD"), 0)
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
    print(f"  Titolo: {name} · {px} · {cur}")
    return s


def get_spot(ib, stk, a):
    """Tempo reale; se non abbonato a quella borsa prova i dati ritardati/congelati."""
    spot = None
    for mdt in ([3] if a.delayed else [1, 4]):
        ib.reqMarketDataType(mdt)
        t = ib.reqMktData(stk, "", False, False)
        t0 = time.time()
        while time.time() - t0 < 2.0:
            ib.sleep(0.1)
            if num(t.bid) and num(t.ask):
                break
        spot = num(t.marketPrice()) or num(t.last) or num(t.close)
        ib.cancelMktData(stk)
        if spot:
            if mdt == 4:
                print("  (spot da dati ritardati: per il titolo non hai l'abbonamento in tempo reale)")
            break
    ib.reqMarketDataType(3 if a.delayed else 1)
    return spot


def parity_spot(quotes, r):
    """C - P + K*e^(-rT) sullo strike con call e put più vicine, prima scadenza >= 7 giorni."""
    today = dt.date.today()
    by_exp = {}
    for (e, k, rt), q in quotes.items():
        by_exp.setdefault(e, {}).setdefault(k, {})[rt] = q
    for e in sorted(by_exp):
        T = (dt.date.fromisoformat(e) - today).days / 365
        if T < 7 / 365:
            continue
        best = None
        for k, s in by_exp[e].items():
            c, p = s.get("C"), s.get("P")
            if not c or not p or None in (c["bid"], c["ask"], p["bid"], p["ask"]):
                continue
            cm, pm = (c["bid"] + c["ask"]) / 2, (p["bid"] + p["ask"]) / 2
            if best is None or abs(cm - pm) < best[0]:
                best = (abs(cm - pm), cm - pm + k * math.exp(-r * T))
        if best:
            return best[1]
    return None


# ---------------------------------------------------------------- contratti (con cache)
def load_contracts(ib, con, sym, stk, chain, opt_ex, exps, a):
    key = (sym, opt_ex, chain.tradingClass)
    today = dt.date.today().isoformat()
    if not a.refresh:
        rows = con.execute(
            "SELECT con_id,expiry,strike,right,exchange,currency,multiplier FROM contracts "
            "WHERE ticker=? AND opt_exchange=? AND trading_class=? AND substr(fetched_at,1,10)=?",
            (*key, today)).fetchall()
        cached_exps = {r[1].replace("-", "") for r in rows}
        if rows and set(exps) <= cached_exps:
            out = []
            for cid, e, k, rt, ex, cur, mult in rows:
                if e.replace("-", "") in exps:
                    o = Option(sym, e.replace("-", ""), k, rt, ex, multiplier=mult, currency=cur,
                               tradingClass=chain.tradingClass)
                    o.conId = cid
                    out.append(o)
            print(f"  Elenco contratti dalla cache di oggi ({len(out)})")
            return out

    async def details_all():
        reqs = [ib.reqContractDetailsAsync(Option(sym, e, exchange=opt_ex, currency=stk.currency,
                                                  tradingClass=chain.tradingClass)) for e in exps]
        return await asyncio.gather(*reqs)

    t0 = time.time()
    det_lists = ib.run(details_all())
    out = [d.contract for det in det_lists for d in det if d.contract.right in ("C", "P")]
    con.execute("DELETE FROM contracts WHERE ticker=? AND opt_exchange=? AND trading_class=?", key)
    now = dt.datetime.now().isoformat(timespec="seconds")
    con.executemany(
        "INSERT OR REPLACE INTO contracts VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        [(*key, c.conId,
          f"{c.lastTradeDateOrContractMonth[:4]}-{c.lastTradeDateOrContractMonth[4:6]}-{c.lastTradeDateOrContractMonth[6:8]}",
          c.strike, c.right, c.exchange, c.currency, str(c.multiplier), now) for c in out])
    con.commit()
    print(f"  Elenco contratti da IB: {len(out)} in {time.time() - t0:.1f} s (salvato in cache per oggi)")
    return out


# ---------------------------------------------------------------- prezzi (finestra scorrevole)
def stream_quotes(ib, contracts, a):
    """Tiene sempre ~a.lines richieste aperte; chiude ognuna appena ha bid e ask (o dopo a.wait s)."""
    pacer = Pacer(MSG_PER_SEC)
    pending = list(reversed(contracts))
    active, out, to_cancel = {}, {}, []
    n, done, t_start, last_print = len(contracts), 0, time.time(), 0.0
    while pending or active or to_cancel:
        # prima chiudi (libera linee), poi apri nuove richieste rispettando il ritmo
        while to_cancel and pacer.take():
            ib.cancelMktData(to_cancel.pop())
        while pending and len(active) + len(to_cancel) < a.lines and pacer.take():
            c = pending.pop()
            active[id(c)] = (c, ib.reqMktData(c, "", False, False), time.time())
        ib.sleep(0.05)
        now = time.time()
        for key in list(active):
            c, tk, t0 = active[key]
            b, k, g = num(tk.bid), num(tk.ask), tk.modelGreeks
            complete = b is not None and k is not None and (not a.greeks or g is not None)
            if (complete and now - t0 >= 0.25) or now - t0 >= a.wait:
                if b is not None and k is not None and b > k:  # quote incrociata
                    b = k = None
                e = c.lastTradeDateOrContractMonth
                out[(f"{e[:4]}-{e[4:6]}-{e[6:8]}", c.strike, c.right)] = dict(
                    bid=b, bid_size=num(tk.bidSize), ask=k, ask_size=num(tk.askSize), last=num(tk.last),
                    close=num(tk.close), volume=num(tk.volume),
                    iv=numg(g.impliedVol) if g else None, delta=numg(g.delta) if g else None,
                    gamma=numg(g.gamma) if g else None, vega=numg(g.vega) if g else None,
                    theta=numg(g.theta) if g else None, und_price=num(g.undPrice) if g else None)
                del active[key]
                to_cancel.append(c)
                done += 1
        if now - last_print > 0.5 or done == n:
            el = now - t_start
            rate = done / el if el > 0 else 0
            eta = (n - done) / rate if rate > 0 else 0
            print(f"\r  prezzi {done}/{n} · {rate:.1f}/s · mancano ~{eta:.0f} s   ", end="", flush=True)
            last_print = now
    print()
    return out


# ---------------------------------------------------------------- scarico di un titolo
def download(ib, con, sym, a):
    t_start = time.time()
    stk = find_stock(ib, sym, a)
    spot = get_spot(ib, stk, a)
    spot_src = "TWS"
    print(f"  Spot {spot}" if spot else "  Spot non disponibile da TWS: lo ricavo dalle opzioni")

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
    today = dt.date.today().strftime("%Y%m%d")
    exps = sorted(e for e in chain.expirations if e >= today)
    if a.until:
        exps = [e for e in exps if e <= a.until.replace("-", "")]
    print(f"  Catena {chain.tradingClass} su {chain.exchange}: {len(exps)} scadenze, moltiplicatore {chain.multiplier}")

    contracts = load_contracts(ib, con, sym, stk, chain, opt_ex, exps, a)
    if not contracts:
        raise RuntimeError("Nessun contratto trovato.")
    center = spot or sorted(c.strike for c in contracts)[len(contracts) // 2]
    rng = a.range if spot else max(a.range, 0.7)
    if not a.all_strikes:
        lo, hi = center * (1 - rng), center * (1 + rng)
        contracts = [c for c in contracts if lo <= c.strike <= hi]
        print(f"  Strike tra {lo:.2f} e {hi:.2f}: {len(contracts)} contratti "
              f"(minimo teorico ~{len(contracts) / (MSG_PER_SEC / 2):.0f} s)")
    if not contracts:
        raise RuntimeError("Nessun contratto da scaricare.")

    quotes = stream_quotes(ib, contracts, a)

    if not spot:
        unds = sorted(q["und_price"] for q in quotes.values() if q["und_price"])
        if unds:
            spot, spot_src = unds[len(unds) // 2], "modello IB (opzioni)"
        else:
            spot, spot_src = parity_spot(quotes, 0.02 if stk.currency == "EUR" else 0.04), "parità put-call"
        if not spot:
            spot = float(input(f"  Non riesco a stimare lo spot di {sym}: inseriscilo a mano: ").replace(",", "."))
            spot_src = "a mano"
        print(f"  Spot {spot:.4f} ({spot_src})")
    return dict(sym=sym, stk=stk, spot=spot, spot_src=spot_src, chain=chain, opt_ex=opt_ex, quotes=quotes,
                seconds=round(time.time() - t_start, 1))


def save_db(con, r, run_id):
    now = dt.datetime.now()
    two = sum(1 for q in r["quotes"].values() if q["bid"] is not None and q["ask"] is not None)
    cur = con.execute(
        "INSERT INTO snapshots(run_id,ticker,taken_at,snap_date,spot,spot_source,currency,multiplier,stock_exchange,"
        "opt_exchange,trading_class,con_id,n_contracts,n_two_sided,seconds) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, r["sym"], now.isoformat(timespec="seconds"), now.date().isoformat(), r["spot"], r["spot_src"],
         r["stk"].currency, str(r["chain"].multiplier), r["stk"].primaryExchange or r["stk"].exchange, r["opt_ex"],
         r["chain"].tradingClass, r["stk"].conId, len(r["quotes"]), two, r["seconds"]))
    sid = cur.lastrowid
    cols = ["bid", "bid_size", "ask", "ask_size", "last", "close", "volume", "iv", "delta", "gamma", "vega", "theta",
            "und_price"]
    con.executemany(f"INSERT INTO quotes VALUES(?,?,?,?,{','.join('?' * len(cols))})",
                    [(sid, e, k, rt, *[q[c] for c in cols]) for (e, k, rt), q in r["quotes"].items()])
    con.commit()
    return sid, two


def summary(r):
    per = {}
    for (e, k, rt), q in r["quotes"].items():
        s = per.setdefault(e, [set(), 0])
        s[0].add(k)
        s[1] += q["bid"] is not None and q["ask"] is not None
    print("  Scadenza: strike / opzioni con bid e ask")
    for e in sorted(per):
        ks, ok = per[e]
        flag = "  <-- pochi prezzi" if ok < 0.3 * 2 * len(ks) else ""
        print(f"    {e}: {len(ks)} / {ok}{flag}")


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Catene opzioni IBKR -> options.db (storico) per Options Lab")
    ap.add_argument("symbols", nargs="+", help="uno o più ticker, es. NFLX NVDA NDA.DE")
    ap.add_argument("--stock-exchange", default=None, help="facoltativo: exchange del titolo (es. SMART, IBIS)")
    ap.add_argument("--primary", default="", help="facoltativo: primary exchange, es. NASDAQ")
    ap.add_argument("--currency", default=None, help="facoltativo: valuta, es. USD o EUR")
    ap.add_argument("--opt-exchange", default=None, help="facoltativo: exchange delle opzioni (es. SMART, EUREX)")
    ap.add_argument("--port", type=int, default=7496, help="7496 TWS live, 7497 TWS paper, 4001/4002 IB Gateway")
    ap.add_argument("--client-id", type=int, default=27)
    ap.add_argument("--until", default=None, help="ultima scadenza da scaricare, AAAA-MM-GG")
    ap.add_argument("--range", type=float, default=0.5, help="strike entro ±questa frazione dello spot (default 0.5)")
    ap.add_argument("--all-strikes", action="store_true", help="scarica tutti gli strike")
    ap.add_argument("--lines", type=int, default=95, help="linee dati usate in parallelo (tue linee meno 5)")
    ap.add_argument("--wait", type=float, default=2.0, help="attesa massima per un'opzione senza prezzo, secondi")
    ap.add_argument("--greeks", action="store_true", help="aspetta anche IV e greche di IB (più lento)")
    ap.add_argument("--refresh", action="store_true", help="riscarica l'elenco contratti anche se in cache")
    ap.add_argument("--auto", action="store_true", help="se il ticker è ambiguo usa la scelta predefinita")
    ap.add_argument("--delayed", action="store_true", help="usa dati ritardati se non hai l'abbonamento")
    ap.add_argument("--db", default=os.path.join(HERE, "options.db"), help="database SQLite")
    a = ap.parse_args()

    con = sqlite3.connect(a.db)
    con.executescript(SCHEMA)

    ib = IB()
    errors = {}

    def on_error(reqId, code, msg, contract=None, *rest):
        if code in (2104, 2106, 2107, 2108, 2158, 2119, 200, 300, 10090, 10167):
            errors[code] = errors.get(code, 0) + 1
            return
        if code not in errors:
            print(f"\n  [IB {code}] {msg}")
        errors[code] = errors.get(code, 0) + 1

    ib.errorEvent += on_error
    try:
        ib.connect("127.0.0.1", a.port, clientId=a.client_id, timeout=15, readonly=True)
    except Exception as e:
        sys.exit(f"Connessione a TWS fallita sulla porta {a.port}: {e}\n"
                 "Controlla che TWS sia aperto, che l'API sia attiva e la porta giusta (7496 live, 7497 paper).")
    ib.reqMarketDataType(3 if a.delayed else 1)

    run_id = dt.datetime.now().isoformat(timespec="seconds")
    t_all = time.time()
    for raw in a.symbols:
        sym, aa = parse_symbol(raw, a)
        print(f"\n== {sym} ==")
        try:
            r = download(ib, con, sym, aa)
        except RuntimeError as e:
            print(f"  ERRORE: {e}")
            continue
        sid, two = save_db(con, r, run_id)
        summary(r)
        print(f"  Salvato: snapshot {sid} · {len(r['quotes'])} opzioni, {two} con bid e ask · {r['seconds']} s")
    ib.disconnect()
    n_snap = con.execute("SELECT COUNT(*), COUNT(DISTINCT ticker) FROM snapshots").fetchone()
    con.close()
    print(f"\nDatabase {a.db}: {n_snap[0]} snapshot di {n_snap[1]} titoli in totale · questo giro {time.time() - t_all:.0f} s")
    if errors.get(354) or errors.get(10089):
        print("ATTENZIONE: TWS segnala dati non abbonati per alcuni contratti.")


if __name__ == "__main__":
    main()
