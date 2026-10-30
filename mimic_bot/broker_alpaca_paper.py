"""Alpaca PAPER trading client (stdlib only).

This only ever talks to Alpaca's paper environment. The trading base URL is hard-wired to
https://paper-api.alpaca.markets and the constructor refuses anything else, including an
APCA_API_BASE_URL env var that points somewhere else. There is no live-money code path in this repo.

Market data (bars and option quotes) comes from https://data.alpaca.markets with GET requests only.
That host is read-only market data; it can't place orders.

Keys come from the environment: APCA_API_KEY_ID and APCA_API_SECRET_KEY (paper keys). They are only
ever put in request headers; error messages and logs never include headers.

Reliability:
  * network errors, timeouts, 429 and 5xx are retried with exponential backoff. Order POSTs
    require a client_order_id, so a retry can't double-submit: if Alpaca already has that id
    ("client_order_id must be unique"), we fetch the existing order instead of placing a second one.
  * other 4xx errors (bad request, insufficient qty, rejected stop, ...) are raised immediately
    as AlpacaAPIError, even when the payload mentions client_order_id.
"""
from __future__ import annotations

import json, os, socket, time as _time, urllib.request, urllib.parse, urllib.error
from typing import Any, Callable, Mapping

PAPER_BASE_URL = "https://paper-api.alpaca.markets"
DATA_BASE_URL = "https://data.alpaca.markets"
RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})

Transport = Callable[[str, str, Mapping[str, str], Any], "tuple[int, Any]"]


class LiveTradingRefused(RuntimeError):
    pass


class AlpacaAPIError(RuntimeError):
    def __init__(self, method: str, path: str, status: int | None, payload: Any):
        self.status = status; self.payload = payload; self.method = method; self.path = path
        super().__init__(f"Alpaca {method} {path} -> {status if status is not None else 'network error'}: {payload}")


def _urllib_transport(method: str, url: str, headers: Mapping[str, str], body: Any) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={**headers, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try: payload = json.loads(raw)
        except Exception: payload = {"message": raw.decode(errors="replace")}
        return e.code, payload


class AlpacaPaperBroker:
    def __init__(self, key_id: str | None = None, secret: str | None = None, base_url: str = PAPER_BASE_URL,
                 transport: Transport | None = None, env: Mapping[str, str] = os.environ,
                 max_retries: int = 4, backoff_s: float = 1.0, sleep: Callable[[float], None] = _time.sleep):
        env_base = env.get("APCA_API_BASE_URL")
        for u in (base_url, env_base):
            if u is not None and u.rstrip("/").removesuffix("/v2") != PAPER_BASE_URL:
                raise LiveTradingRefused(f"refusing base URL {u!r}: this bot only trades on {PAPER_BASE_URL}")
        self.base = PAPER_BASE_URL
        self.key_id = key_id or env.get("APCA_API_KEY_ID")
        self.secret = secret or env.get("APCA_API_SECRET_KEY")
        if not self.key_id or not self.secret:
            raise RuntimeError("set APCA_API_KEY_ID and APCA_API_SECRET_KEY (paper keys) in your environment or .env")
        self.transport = transport or _urllib_transport
        self.max_retries = max_retries; self.backoff_s = backoff_s; self.sleep = sleep

    def __repr__(self) -> str:   # never show keys
        return f"AlpacaPaperBroker(base={self.base!r})"

    # --- plumbing ---
    def _req(self, method: str, host: str, path: str, params: Mapping[str, Any] | None = None, body: Any = None,
             retry: bool = True) -> Any:
        if host not in (PAPER_BASE_URL, DATA_BASE_URL):
            raise LiveTradingRefused(f"unexpected host {host}")
        if host == DATA_BASE_URL and method != "GET":
            raise LiveTradingRefused("the data host is read-only")
        url = host + path + ("?" + urllib.parse.urlencode(params) if params else "")
        headers = {"APCA-API-KEY-ID": self.key_id, "APCA-API-SECRET-KEY": self.secret}
        attempts = (self.max_retries + 1) if retry else 1
        last: AlpacaAPIError | None = None
        for n in range(attempts):
            try:
                status, payload = self.transport(method, url, headers, body)
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
                last = AlpacaAPIError(method, path, None, type(e).__name__ + ": " + str(getattr(e, "reason", e)))
            else:
                if status < 400:
                    return payload
                last = AlpacaAPIError(method, path, status, payload)
                if status not in RETRY_STATUSES:
                    raise last
            if n + 1 < attempts:
                self.sleep(min(30.0, self.backoff_s * 2 ** n))
        assert last is not None
        raise last

    def _trade(self, method: str, path: str, params: Mapping[str, Any] | None = None, body: Any = None) -> Any:
        return self._req(method, PAPER_BASE_URL, path, params, body)

    def _data(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        return self._req("GET", DATA_BASE_URL, path, params)

    # --- read-only ---
    def account(self) -> dict: return self._trade("GET", "/v2/account")
    def clock(self) -> dict: return self._trade("GET", "/v2/clock")
    def positions(self) -> list[dict]: return self._trade("GET", "/v2/positions") or []
    def get_order(self, order_id: str) -> dict: return self._trade("GET", f"/v2/orders/{order_id}")

    def get_order_by_client_id(self, client_order_id: str) -> dict:
        return self._trade("GET", "/v2/orders:by_client_order_id", dict(client_order_id=client_order_id))

    def open_orders(self) -> list[dict]:
        return self._trade("GET", "/v2/orders", dict(status="open", limit=500, nested="false")) or []

    def closed_orders(self, symbol: str, limit: int = 50) -> list[dict]:
        return self._trade("GET", "/v2/orders", dict(status="closed", symbols=symbol, limit=limit, direction="desc")) or []

    def option_positions(self) -> list[dict]:
        return [p for p in self.positions() if p.get("asset_class") == "us_option" or _looks_like_occ(p.get("symbol", ""))]

    def options_level(self) -> int:
        a = self.account()
        return int(a.get("options_trading_level") or a.get("options_approved_level") or 0)

    def check(self) -> dict:
        """Read-only sanity check: account, clock, options level. Places nothing."""
        a = self.account(); c = self.clock()
        out = dict(base_url=self.base, status=a.get("status"), equity=a.get("equity"), buying_power=a.get("buying_power"),
                   options_trading_level=int(a.get("options_trading_level") or a.get("options_approved_level") or 0),
                   market_open=c.get("is_open"), next_open=c.get("next_open"))
        try:
            out["open_option_positions"] = [p.get("symbol") for p in self.option_positions()]
            out["open_orders"] = len(self.open_orders())
        except AlpacaAPIError as e:
            out["positions_check_error"] = str(e)
        return out

    def stock_bars(self, symbol: str, start_iso: str, timeframe: str = "5Min", feed: str = "iex") -> list[dict]:
        out: list[dict] = []; token = None
        while True:
            p: dict[str, Any] = dict(timeframe=timeframe, start=start_iso, limit=10000, feed=feed, adjustment="raw")
            if token: p["page_token"] = token
            r = self._data(f"/v2/stocks/{symbol}/bars", p) or {}
            out += r.get("bars") or []
            token = r.get("next_page_token")
            if not token: return out

    def option_contracts(self, underlying: str, expiration: str, kind: str) -> list[dict]:
        r = self._trade("GET", "/v2/options/contracts", dict(underlying_symbols=underlying, expiration_date=expiration,
                                                             type=kind, status="active", limit=1000))
        return (r or {}).get("option_contracts") or []

    def option_quotes(self, symbols: list[str]) -> dict[str, dict]:
        if not symbols: return {}
        r = self._data("/v1beta1/options/quotes/latest", dict(symbols=",".join(symbols), feed="indicative"))
        return (r or {}).get("quotes") or {}

    # --- paper orders (buy-to-open / sell-to-close long options only) ---
    def _submit(self, body: dict) -> dict:
        """POST an order. client_order_id is required so a retry cannot open a second order.
        Only the duplicate-id 422 is turned into a fetch of the existing order. Other 422s
        (rejected stop, insufficient qty, ...) are raised even if the body echoed the id.
        Alpaca uses code 40010001 for several different 422s, so the message has to say the id is unique."""
        cid = body.get("client_order_id")
        if not cid:
            raise ValueError("client_order_id is required so a retried POST cannot double-submit")
        try:
            return self._req("POST", PAPER_BASE_URL, "/v2/orders", body=body, retry=True)
        except AlpacaAPIError as e:
            if _is_duplicate_client_order(e):
                return self.get_order_by_client_id(cid)
            raise

    def buy_to_open(self, occ: str, qty: int, limit_price: float, client_order_id: str | None = None) -> dict:
        body = dict(symbol=occ, qty=str(int(qty)), side="buy", type="limit", limit_price=f"{limit_price:.2f}",
                    time_in_force="day", client_order_id=client_order_id)
        return self._submit(body)

    def place_stop(self, occ: str, qty: int, stop_price: float, client_order_id: str | None = None) -> dict:
        body = dict(symbol=occ, qty=str(int(qty)), side="sell", type="stop", stop_price=f"{stop_price:.2f}",
                    time_in_force="day", client_order_id=client_order_id)
        return self._submit(body)

    def sell_to_close(self, occ: str, qty: int, client_order_id: str | None = None) -> dict:
        body = dict(symbol=occ, qty=str(int(qty)), side="sell", type="market", time_in_force="day",
                    client_order_id=client_order_id)
        return self._submit(body)

    def cancel(self, order_id: str) -> Any:
        try:
            return self._trade("DELETE", f"/v2/orders/{order_id}")
        except AlpacaAPIError as e:
            if e.status in (404, 422):   # already filled / cancelled / gone: nothing to cancel
                return None
            raise


def _is_duplicate_client_order(err: AlpacaAPIError) -> bool:
    """True only for Alpaca's 'client_order_id must be unique' 422, not every 422 that mentions the field."""
    if err.status != 422:
        return False
    try:
        text = json.dumps(err.payload, default=str).lower()
    except Exception:
        text = str(err.payload).lower()
    return "client_order_id" in text and ("unique" in text or "duplicate" in text)


def _looks_like_occ(sym: str) -> bool:
    """OCC option symbol: ROOT + YYMMDD + C/P + 8-digit strike, e.g. SPY251219C00600000."""
    return len(sym) > 15 and sym[-9] in "CP" and sym[-8:].isdigit() and sym[-15:-9].isdigit()


def occ_root(sym: str) -> str:
    return sym[:-15]


def occ_kind(sym: str) -> str:
    return "call" if sym[-9] == "C" else "put"
