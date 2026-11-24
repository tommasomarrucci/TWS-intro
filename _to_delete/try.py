from ib_async import IB, Option
ib = IB()
ib.connect('127.0.0.1', 7496, clientId=2, readonly=True)
ib.reqMarketDataType(1)  # solo real-time
c = Option('NKE', '20270319', 45, 'C', 'SMART')
ib.qualifyContracts(c)
t = ib.reqMktData(c)
ib.sleep(3)
print(t.bid, t.ask, t.last, t.marketDataType)
ib.disconnect()