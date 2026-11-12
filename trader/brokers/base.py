"""What the engine needs from a broker. Toss (trader/toss.py) is the reference: its JSON shapes are the contract.

Read-only calls
    token() -> str                                 log in / refresh; raise on bad keys
    prices(symbols) -> {"result": [{"symbol", "lastPrice", "timestamp" (ISO-8601, None if no trade yet)}]}
    holdings() -> {"result": {"items": [{"symbol", "quantity", ...}]}}
    buying_power(ccy) -> {"result": {"cashBuyingPower": "123.45"}}
    candles(symbol, interval="1d"|"1m", count=200, before=None) -> {"result": {"candles": [...]}}
        optional: raise NotImplementedError and the engine uses Yahoo bars
    market_calendar(market) -> {"previousBusinessDay", "today", "nextBusinessDay"}   optional (NotImplementedError)
    usd_krw() -> float                             optional (NotImplementedError)

Orders (REAL MONEY)
    submit_order(body) -> orderId
        body = {"clientOrderId", "symbol", "side": "BUY"|"SELL", "orderType": "LIMIT", "timeInForce": "DAY",
                "quantity": "8", "price": "123.45"}
    list_orders(status "OPEN"|"CLOSED") -> [{"orderId", "symbol", "side", "quantity", "price", "orderedAt", "status"}]
    get_order(orderId) -> {"orderId", "status": "OPEN"|"FILLED"|"CANCELED"|"REJECTED"|...,
                           "execution": {"filledQuantity", "averageFilledPrice", "commission", "tax"}}
    cancel_order(orderId)

The engine decides; the adapter only translates. Safety checks (arming, caps, kill switch, risk gate) live in
app/core/accounts.py and apply to every broker the same way.
"""


class BrokerUnavailable(RuntimeError):
    """The broker could not be reached after retries (network blip, DNS, outage): nothing was processed."""


class Broker:
    name = "base"
    label = "broker"
    tested = False

    def token(self) -> str: raise NotImplementedError
    def prices(self, symbols): raise NotImplementedError
    def holdings(self): raise NotImplementedError
    def buying_power(self, currency="USD"): raise NotImplementedError
    def candles(self, symbol, **p): raise NotImplementedError
    def market_calendar(self, market): raise NotImplementedError
    def usd_krw(self) -> float: raise NotImplementedError
    def submit_order(self, body) -> str: raise NotImplementedError
    def list_orders(self, status) -> list: raise NotImplementedError
    def get_order(self, oid) -> dict: raise NotImplementedError
    def cancel_order(self, oid): raise NotImplementedError
