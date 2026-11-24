from ib_async import IB, util

ib = IB()
ib.connect('127.0.0.1', 7496, clientId=1)

pos = util.df(ib.portfolio())
pos.to_csv('portfolio.csv', index=False)
print(pos[['contract', 'position', 'marketPrice', 'marketValue', 'averageCost', 'unrealizedPNL']])

util.df(ib.accountSummary()).to_csv('account.csv', index=False)
ib.disconnect()