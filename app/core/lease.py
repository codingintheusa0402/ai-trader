"""Single-engine guard.

Run ONE engine per brokerage account. Two engines trading the same account would double every order.
This build has no multi-machine failover, so the lease is always held by this engine. The interface is kept
so the risk gate and the executor can keep asking "may I trade?" in one place.
"""


class Lease:
    configured = False

    def start(self):
        pass

    def status(self) -> str:
        return "mine"

    def allow(self, side=None) -> bool:
        return True

    def view(self) -> dict:
        return {"me": "local", "status": "mine", "holder": "local", "error": None}


LEASE = Lease()
