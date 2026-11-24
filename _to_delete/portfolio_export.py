from ib_async import IB, util
import pandas as pd

ib = IB()
ib.connect('127.0.0.1', 7496, clientId=1, readonly=True)

rows = []
for p in ib.portfolio():
    c = p.contract
    rows.append({
        'symbol': c.symbol,
        'type': c.secType,
        'expiry': c.lastTradeDateOrContractMonth,
        'strike': c.strike or None,
        'right': c.right,
        'currency': c.currency,
        'multiplier': c.multiplier,
        'position': p.position,
        'price': p.marketPrice,
        'value': p.marketValue,
        'avg_cost': p.averageCost,
        'unrealized': p.unrealizedPNL,
        'realized': p.realizedPNL,
    })

pos = pd.DataFrame(rows)
pos.to_csv('portfolio.csv', index=False)
print(pos.to_string(index=False))

util.df(ib.accountSummary()).to_csv('account.csv', index=False)
ib.disconnect()