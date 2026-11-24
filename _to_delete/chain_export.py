"""
Scarica l'intera option chain di un titolo da IBKR (TWS/Gateway) e la salva in CSV.

Uso:
    python chain_export.py            # NKE
    python chain_export.py ONON       # altro titolo

Output (nella cartella corrente):
    <SYMBOL>_chain_<data_ora>.csv       formato lungo: una riga per contratto
    <SYMBOL>_chain_<data_ora>_wide.csv  formato come la UI: call a sinistra, put a destra per ogni strike
"""
import sys
import math
import logging
from datetime import datetime, date

import pandas as pd
from ib_async import IB, Stock, Option

# ---------------- PARAMETRI ----------------
SYMBOL = sys.argv[1].upper() if len(sys.argv) > 1 else 'NKE'
PORT = 7496            # 7496 TWS live, 7497 TWS paper, 4001 Gateway live
CLIENT_ID = 3
BATCH = 80             # contratti per giro (limite: 100 linee di market data)
WAIT = 4               # secondi di attesa dati per ogni giro
MAX_DAYS = None        # es. 180 = solo scadenze entro 180 giorni; None = tutte
STRIKE_RANGE = None    # es. 0.30 = solo strike entro ±30% dallo spot; None = tutti
MKT_DATA_TYPE = 3      # 1 = solo real-time; 3 = real-time dove abbonato, altrimenti delayed
GENERIC_TICKS = '101'  # 101 = open interest
# -------------------------------------------

# silenzia i warning per-contratto su dati ritardati/abbonamenti parziali
logging.getLogger('ib_async.wrapper').setLevel(logging.CRITICAL)


def num(x):
    """None per NaN/None/valori sentinella IBKR, altrimenti float."""
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or x == -1 or abs(x) > 1e300:
        return None
    return x


ib = IB()
ib.connect('127.0.0.1', PORT, clientId=CLIENT_ID, readonly=True)
ib.reqMarketDataType(MKT_DATA_TYPE)

try:
    # 1. sottostante e spot
    stock = Stock(SYMBOL, 'SMART', 'USD')
    ib.qualifyContracts(stock)
    st = ib.reqMktData(stock)
    ib.sleep(2)
    spot = num(st.marketPrice()) or num(st.last) or num(st.close)
    ib.cancelMktData(stock)
    print(f'{SYMBOL} spot: {spot}')

    # 2. scadenze disponibili
    chains = ib.reqSecDefOptParams(stock.symbol, '', stock.secType, stock.conId)
    smart = [c for c in chains if c.exchange == 'SMART']
    chain = next((c for c in smart if c.tradingClass == SYMBOL), smart[0])
    today = date.today()
    expiries = sorted(chain.expirations)
    if MAX_DAYS is not None:
        expiries = [e for e in expiries
                    if (datetime.strptime(e, '%Y%m%d').date() - today).days <= MAX_DAYS]
    print(f'Scadenze: {len(expiries)} ({expiries[0]} -> {expiries[-1]})')

    # 3. tutti i contratti realmente esistenti, scadenza per scadenza
    contracts = []
    for exp in expiries:
        cds = ib.reqContractDetails(Option(
            symbol=SYMBOL, lastTradeDateOrContractMonth=exp,
            exchange='SMART', currency='USD', tradingClass=chain.tradingClass))
        contracts += [cd.contract for cd in cds]

    if STRIKE_RANGE is not None and spot:
        contracts = [c for c in contracts if abs(c.strike / spot - 1) <= STRIKE_RANGE]

    contracts.sort(key=lambda c: (c.lastTradeDateOrContractMonth, c.strike, c.right))
    print(f'Contratti: {len(contracts)}  (~{len(contracts) / BATCH * WAIT:.0f}s)')

    # 4. market data a blocchi
    ts = datetime.now()
    rows = []
    for i in range(0, len(contracts), BATCH):
        batch = contracts[i:i + BATCH]
        tickers = [ib.reqMktData(c, GENERIC_TICKS) for c in batch]
        ib.sleep(WAIT)

        for c, t in zip(batch, tickers):
            g = t.modelGreeks
            bid, ask = num(t.bid), num(t.ask)
            exp_date = datetime.strptime(c.lastTradeDateOrContractMonth, '%Y%m%d').date()
            oi = t.callOpenInterest if c.right == 'C' else t.putOpenInterest
            rows.append({
                'timestamp': ts.isoformat(timespec='seconds'),
                'symbol': SYMBOL,
                'expiry': c.lastTradeDateOrContractMonth,
                'dte': (exp_date - today).days,
                'strike': c.strike,
                'right': c.right,
                'bid': bid,
                'ask': ask,
                'mid': round((bid + ask) / 2, 4) if bid and ask else None,
                'bid_size': num(t.bidSize),
                'ask_size': num(t.askSize),
                'last': num(t.last),
                'close': num(t.close),
                'volume': num(t.volume),
                'open_interest': num(oi),
                'iv': num(g.impliedVol) if g else None,
                'delta': num(g.delta) if g else None,
                'gamma': num(g.gamma) if g else None,
                'vega': num(g.vega) if g else None,
                'theta': num(g.theta) if g else None,
                'und_price': num(g.undPrice) if g else spot,
                'md_type': t.marketDataType,   # 1 real-time, 2 frozen, 3 delayed, 4 delayed frozen
                'conId': c.conId,
                'local_symbol': c.localSymbol,
            })

        for c in batch:
            ib.cancelMktData(c)
        print(f'  {min(i + BATCH, len(contracts))}/{len(contracts)}')

    # 5. salvataggio
    df = pd.DataFrame(rows)
    stamp = ts.strftime('%Y%m%d_%H%M')
    long_path = f'{SYMBOL}_chain_{stamp}.csv'
    df.to_csv(long_path, index=False)

    cols = ['bid', 'ask', 'mid', 'last', 'volume', 'open_interest', 'iv', 'delta', 'gamma', 'vega', 'theta']
    calls = df[df.right == 'C'][['expiry', 'dte', 'strike'] + cols]
    puts = df[df.right == 'P'][['expiry', 'strike'] + cols]
    wide = calls.merge(puts, on=['expiry', 'strike'], how='outer', suffixes=('_C', '_P'))
    wide = wide.sort_values(['expiry', 'strike'])
    wide_path = f'{SYMBOL}_chain_{stamp}_wide.csv'
    wide.to_csv(wide_path, index=False)

    print(f'Salvati: {long_path}, {wide_path}')
    print(f'Righe con bid/ask: {df.bid.notna().sum()}/{len(df)}  |  con greche: {df.delta.notna().sum()}/{len(df)}')

finally:
    ib.disconnect()