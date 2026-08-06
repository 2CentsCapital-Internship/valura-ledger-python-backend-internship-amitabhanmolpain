"""Your ledger. This is the whole assignment.

`client.py` handles the network and hands you one event at a time. You return
the journal legs it produced. Some events correctly produce none: return an
empty list, not None-as-an-accident.

One event type is implemented as a worked example. The rest raise, with the rule
from PROTOCOL.md quoted in the message, so a practice run tells you exactly what
is left rather than silently scoring zero.

Two things to get right before anything else:

  * Use `Decimal`, never `float`. Money here does not always divide evenly, and
    a float implementation will disagree with us by a cent in places you will
    struggle to find.
  * Key balances by (customer, account), not by account. At least one event
    moves money between two customers on the same account, and an
    account-level book shows nothing wrong at all.
"""
from __future__ import annotations

from collections import defaultdict
import copy
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

D = Decimal
ZERO = D("0.00")


def money(x: Decimal | int | float | str) -> Decimal:
    """2 decimal places, half away from zero. Not round(), which is half-even."""
    if not isinstance(x, Decimal):
        x = D(str(x))
    return x.quantize(D("0.01"), rounding=ROUND_HALF_UP)


def leg(account: str, customer_id: str, debit=ZERO, credit=ZERO) -> dict:
    return {"account": account, "customer_id": customer_id,
            "debit": str(money(D(debit))), "credit": str(money(D(credit)))}


class Book:
    def __init__(self) -> None:
        # balances[(customer_id, account)] = debit-positive balance
        self.balances: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
        self.seen: set[str] = set()
        self.accounts_seen: set[str] = set()
        # What you have not written yet. An unimplemented handler must not stop
        # the run: the client keeps consuming and tells you the list at the end.
        self.todo: dict[str, int] = defaultdict(int)

        # Store withdrawal requests
        self.withdrawals = {}

        # Store fee events
        self.fees = {}

        # Store orders
        self.orders = {}

        # Store trades
        self.trades = {}

        # FIFO lots
        self.lots = defaultdict(lambda: defaultdict(list))

        # Customer positions
        self.positions = defaultdict(lambda: defaultdict(Decimal))

        # Posted legs, keyed by event_id, for reversal lookup
        self.posted_legs = {}

        # Reversed events set for double-reversal protection
        self.reversed_events = set()

        # Reversal undo actions
        self.undo_actions = defaultdict(list)

    # -----------------------------------------------------------------------
    def apply(self, ev: dict) -> list[dict]:
        """Post one event and return its legs.

        The same event_id can arrive more than once, and the server will
        deliberately re-send several hundred events partway through the run.
        Posting twice is the single most expensive mistake available here.
        """
        eid = ev["event_id"]

        if eid in self.seen:
            return []                      # already posted; nothing new happens

        self.seen.add(eid)

        handler = getattr(self, "on_" + ev["type"], None)
        if handler is None:
            self.todo[ev["type"]] += 1
            return []
        try:
            legs = handler(ev["payload"], ev) or []
        except NotImplementedError:
            # Not written yet. Submit nothing for it and carry on, so one
            # missing handler costs you that event rather than the whole run.
            self.todo[ev["type"]] += 1
            return []
        except Rejected:
            # An event you refuse still gets a submission, with no legs, and it
            # must leave your book exactly as it was.
            return []
        self._post(legs)
        # Store the posted legs so reversals can invert them later
        self.posted_legs[eid] = legs
        return legs

    def _post(self, legs: list[dict]) -> None:
        dr = sum((D(l["debit"]) for l in legs), ZERO)
        cr = sum((D(l["credit"]) for l in legs), ZERO)
        if money(dr) != money(cr):
            raise AssertionError(f"unbalanced: dr {dr} cr {cr}")
        for l in legs:
            self.accounts_seen.add(l["account"])
            self.balances[(l["customer_id"], l["account"])] += (
                D(l["debit"]) - D(l["credit"]))

    # -- worked example -----------------------------------------------------
    def on_deposit(self, p: dict, ev: dict) -> list[dict]:
        """Cash arrives, and the firm owes the customer more.

            Dr 1100 amount        Cr 2010 amount
        """
        amount = money(D(p["amount"]))
        cid = p["customer_id"]
        return [leg("1100", cid, debit=amount),
                leg("2010", cid, credit=amount)]

    # -- yours --------------------------------------------------------------
    def on_fee_charged(self, p, ev):
        try:
            amount = money(D(str(p["amount"])))
            cid = p["customer_id"]
        except (InvalidOperation, TypeError, ValueError, KeyError) as e:
            raise Rejected(f"Invalid fee_charged payload: {p}") from e

        self.fees[ev["event_id"]] = {"amount": amount, "customer_id": cid}

        def undo_fee():
            self.fees.pop(ev["event_id"], None)
        self.undo_actions[ev["event_id"]].append(undo_fee)

        return [
            leg("2010", cid, debit=amount),
            leg("1100", cid, credit=amount)
        ]

    def on_fee_refund(self, p, ev):
        try:
            refund_id = p["refunds_source_id"]

            if refund_id not in self.fees:
                raise Rejected("Original fee not found or already refunded")

            fee = self.fees.pop(refund_id)  # consume: a fee can only be refunded once
            amount = fee["amount"]

            # Protocol: customer_id is in the fee_refund payload itself
            cid = p.get("customer_id") or fee["customer_id"]

        except (KeyError, InvalidOperation, TypeError, ValueError):
            raise Rejected("Invalid fee_refund payload")

        return [
            leg("1100", cid, debit=amount),
            leg("2010", cid, credit=amount)
        ]

    def on_interest_credited(self, p, ev):
        try:
            gross = money(D(str(p["gross_amount"])))
            customer_share = money(D(str(p["customer_share"])))
            cid = p["customer_id"]

            company_share = money(gross - customer_share)

        except (InvalidOperation, KeyError, TypeError, ValueError):
            raise Rejected("Invalid interest_credited payload")

        return [
            leg("1100", cid, debit=gross),
            leg("2010", cid, credit=customer_share),
            leg("4200", cid, credit=company_share)
        ]

    def on_transfer_between_customers(self, p, ev):
        try:
            amount = money(D(str(p["amount"])))
            from_customer = p["from_customer_id"]
            to_customer = p["to_customer_id"]

        except (InvalidOperation, KeyError, TypeError, ValueError):
            raise Rejected("Invalid transfer_between_customers payload")

        return [
            leg("2010", from_customer, debit=amount),
            leg("2010", to_customer, credit=amount)
        ]

    def on_fx_deposit(self, p, ev):
        try:
            market = money(D(str(p["usd_at_market_rate"])))
            customer = money(D(str(p["usd_at_customer_rate"])))
            cid = p["customer_id"]

            spread = money(market - customer)
            if spread < ZERO:
                raise Rejected("Negative FX spread")

        except (InvalidOperation, KeyError, TypeError, ValueError):
            raise Rejected("Invalid fx_deposit payload")

        return [
            leg("1100", cid, debit=market),
            leg("2010", cid, credit=customer),
            leg("4100", cid, credit=spread)
        ]

    def on_withdrawal_requested(self, p, ev):
        try:
            amount = money(D(str(p["amount"])))
            cid = p["customer_id"]
            wid = p.get("withdrawal_id", ev["event_id"])
        except (InvalidOperation, KeyError, TypeError, ValueError):
            raise Rejected("Invalid withdrawal_requested payload")

        req = {"amount": amount, "customer_id": cid, "req_event_id": ev["event_id"]}
        self.withdrawals[wid] = req
        self.withdrawals[ev["event_id"]] = req

        def undo_withdrawal():
            self.withdrawals.pop(wid, None)
            self.withdrawals.pop(ev["event_id"], None)
        self.undo_actions[ev["event_id"]].append(undo_withdrawal)

        return [
            leg("2010", cid, debit=amount),
            leg("2300", cid, credit=amount)
        ]

    def on_withdrawal_settled(self, p, ev):
        try:
            wid = p["withdrawal_id"]
            req = self.withdrawals[wid]
            amount = req["amount"]
            cid = req["customer_id"]
        except (KeyError, InvalidOperation, TypeError, ValueError):
            raise Rejected("Invalid withdrawal_settled payload or request not found")

        self.withdrawals.pop(wid, None)
        if "req_event_id" in req:
            self.withdrawals.pop(req["req_event_id"], None)

        return [
            leg("2300", cid, debit=amount),
            leg("1100", cid, credit=amount)
        ]

    def on_withdrawal_rejected(self, p, ev):
        try:
            wid = p["withdrawal_id"]
            req = self.withdrawals[wid]
            amount = req["amount"]
            cid = req["customer_id"]
        except (KeyError, InvalidOperation, TypeError, ValueError):
            raise Rejected("Invalid withdrawal_rejected payload or request not found")

        self.withdrawals.pop(wid, None)
        if "req_event_id" in req:
            self.withdrawals.pop(req["req_event_id"], None)

        return [
            leg("2300", cid, debit=amount),
            leg("2010", cid, credit=amount)
        ]

    def on_order_placed(self, p, ev):
        try:
            order = dict(p)
            if order.get("side") == "buy":
                qty = Decimal(str(order["quantity"]))
                limit = Decimal(str(order["limit_price"]))
                comm = Decimal(str(order.get("est_commission", "0")))
                hold = money(qty * limit + comm)
                order["hold"] = hold
                order["initial_quantity"] = qty
                order["initial_hold"] = hold
            self.orders[order["order_id"]] = order

            oid = p["order_id"]
            def undo_order_placed():
                self.orders.pop(oid, None)
            self.undo_actions[ev["event_id"]].append(undo_order_placed)

        except (KeyError, InvalidOperation, TypeError, ValueError):
            raise Rejected("Invalid order")

        return []

    def on_order_partially_filled(self, p, ev):
        return self.on_order_filled(p, ev)

    def on_order_filled(self, p, ev):
        try:
            cid = p["customer_id"]
            side = p["side"]
            symbol = p["symbol"]

            quantity = Decimal(str(p["quantity"]))
            principal = money(Decimal(str(p["principal"])))

            commission = money(Decimal(str(p.get("commission", "0.00"))))

            trade_id = p["trade_id"]

            if "order_id" in p:
                if ev["type"] == "order_filled":
                    # Final fill closes the order. Protocol: released hold stays released.
                    self.orders.pop(p["order_id"], None)
                elif ev["type"] == "order_partially_filled":
                    oid = p["order_id"]
                    if oid in self.orders and self.orders[oid].get("side") == "buy":
                        order = self.orders[oid]
                        if "initial_quantity" in order and "initial_hold" in order:
                            # Track cumulative filled quantity to avoid rounding drift.
                            # Remaining hold = initial_hold * (remaining_qty / initial_qty)
                            # This resets precision on every fill instead of accumulating
                            # independently-rounded subtractions.
                            filled_so_far = order.get("filled_quantity", ZERO) + quantity
                            order["filled_quantity"] = filled_so_far
                            remaining_qty = max(ZERO, order["initial_quantity"] - filled_so_far)
                            # Protocol: released hold stays released — no undo registered
                            order["hold"] = money(order["initial_hold"] * remaining_qty / order["initial_quantity"])

        except (KeyError, InvalidOperation, TypeError, ValueError):
            raise Rejected("Invalid order_filled payload")

        if side == "buy":
            # Store trade for later settlement
            self.trades[trade_id] = {
                "customer_id": cid,
                "principal": principal,
                "commission": commission,
                "side": side
            }

            # Add FIFO lot — capture the exact object for reversal
            new_lot = {
                "quantity": quantity, 
                "cost": principal,
                "initial_quantity": quantity,
                "initial_cost": principal
            }
            self.lots[cid][symbol].append(new_lot)

            # Update customer position
            self.positions[cid][symbol] += quantity

            def undo_buy():
                self.trades.pop(trade_id, None)
                try:
                    self.lots[cid][symbol].remove(new_lot)  # exact object, not last
                except ValueError:
                    pass
                self.positions[cid][symbol] -= quantity
                if self.positions[cid][symbol] <= 0:
                    del self.positions[cid][symbol]
                    if not self.positions[cid]:
                        del self.positions[cid]
            self.undo_actions[ev["event_id"]].append(undo_buy)

            return [
                leg("2010", cid, debit=principal + commission),
                leg("1200", cid, debit=principal),

                leg("2350", cid, credit=principal),
                leg("2100", cid, credit=principal),
                leg("4000", cid, credit=commission)
            ]

        if side == "sell":
            # FIFO cost of shares sold
            cost, undo_fifo = self.consume_fifo(cid, symbol, quantity)

            # Regulatory fee: principal x 0.0008, rounded to cent
            reg = money(principal * Decimal("0.0008"))

            # Store trade for later settlement
            self.trades[trade_id] = {
                "customer_id": cid,
                "principal": principal,
                "commission": commission,
                "cost": cost,
                "side": side
            }

            def undo_sell():
                self.trades.pop(trade_id, None)
                self.positions[cid][symbol] += quantity
                undo_fifo()
            self.undo_actions[ev["event_id"]].append(undo_sell)

            # Update customer position
            self.positions[cid][symbol] -= quantity

            # Remove position if fully sold out
            if self.positions[cid][symbol] <= 0:
                del self.positions[cid][symbol]
                if not self.positions[cid]:
                    del self.positions[cid]

            return [
                leg("1150", cid, debit=principal),
                leg("2100", cid, debit=cost),

                leg("2010", cid, credit=principal - commission - reg),
                leg("1200", cid, credit=cost),
                leg("4000", cid, credit=commission),
                leg("2400", cid, credit=reg)
            ]

        raise Rejected(f"Unknown order side: {side}")

    def on_trade_settled(self, p, ev):
        try:
            trade_id = p["trade_id"]
            trade = self.trades.pop(trade_id)

            amount = trade["principal"]
            cid = trade["customer_id"]
            side = trade["side"]

        except (KeyError, InvalidOperation, TypeError, ValueError):
            raise Rejected("Invalid trade_settled payload")

        if side == "buy":
            return [
                leg("2350", cid, debit=amount),
                leg("1100", cid, credit=amount)
            ]

        elif side == "sell":
            return [
                leg("1100", cid, debit=amount),
                leg("1150", cid, credit=amount)
            ]

        raise Rejected("Unknown trade side")

    def on_order_cancelled(self, p, ev):
        try:
            self.orders.pop(p["order_id"], None)
        except KeyError:
            raise Rejected("Invalid order_cancelled payload")

        return []

    def on_order_rejected(self, p, ev):
        return self.on_order_cancelled(p, ev)

    def on_broker_fees_settled(self, p, ev):
        try:
            cid = p["customer_id"]
            broker = p["broker"]
        except KeyError:
            raise Rejected("Invalid broker_fees_settled payload")
            
        acct_map = {"BRK-A": "2411", "BRK-B": "2412", "BRK-C": "2413"}
        acct = acct_map.get(broker)
        if not acct:
            raise Rejected(f"Unknown broker: {broker}")
            
        owed = -self.balances.get((cid, acct), ZERO)
        if owed <= ZERO:
            return []
            
        return [
            leg(acct, cid, debit=owed),
            leg("1100", cid, credit=owed)
        ]

    def on_custodian_fees_settled(self, p, ev):
        try:
            cid = p["customer_id"]
        except KeyError:
            raise Rejected("Invalid custodian_fees_settled payload")
            
        owed = -self.balances.get((cid, "2420"), ZERO)
        if owed <= ZERO:
            return []
            
        return [
            leg("2420", cid, debit=owed),
            leg("1100", cid, credit=owed)
        ]

    def on_partner_payout(self, p, ev):
        try:
            cid = p["customer_id"]
        except KeyError:
            raise Rejected("Invalid partner_payout payload")
            
        owed = -self.balances.get((cid, "2430"), ZERO)
        if owed <= ZERO:
            return []
            
        return [
            leg("2430", cid, debit=owed),
            leg("1100", cid, credit=owed)
        ]
    def on_reg_fees_remitted(self, p, ev):
        try:
            cid = p["customer_id"]
        except KeyError:
            raise Rejected("Invalid reg_fees_remitted payload")
            
        owed = -self.balances.get((cid, "2400"), ZERO)
        if owed <= ZERO:
            return []
            
        return [
            leg("2400", cid, debit=owed),
            leg("1100", cid, credit=owed)
        ]

    def on_dividend_cash(self, p, ev):
        try:
            net = money(D(str(p["net_amount"])))
            cid = p["customer_id"]
        except (KeyError, InvalidOperation, TypeError, ValueError):
            raise Rejected("Invalid dividend_cash payload")

        return [
            leg("1100", cid, debit=net),
            leg("2010", cid, credit=net)
        ]

    def on_dividend_reinvested(self, p, ev):
        try:
            net = money(D(str(p["net_amount"])))
            cid = p["customer_id"]
            symbol = p["symbol"]
            qty = Decimal(str(p["reinvest_quantity"]))
        except (KeyError, InvalidOperation, TypeError, ValueError):
            raise Rejected("Invalid dividend_reinvested payload")

        # Add a FIFO lot at cost = net
        new_lot = {
            "quantity": qty, 
            "cost": net,
            "initial_quantity": qty,
            "initial_cost": net
        }
        self.lots[cid][symbol].append(new_lot)
        self.positions[cid][symbol] += qty

        def undo_reinvest():
            try:
                self.lots[cid][symbol].remove(new_lot)
            except ValueError:
                pass
            self.positions[cid][symbol] -= qty
            if self.positions[cid][symbol] <= 0:
                del self.positions[cid][symbol]
                if not self.positions[cid]:
                    del self.positions[cid]
        self.undo_actions[ev["event_id"]].append(undo_reinvest)

        return [
            leg("1200", cid, debit=net),
            leg("2100", cid, credit=net)
        ]

    def on_stock_split(self, p, ev):
        try:
            cid = p["customer_id"]
            symbol = p["symbol"]
            ratio_from = Decimal(str(p["ratio_from"]))
            ratio_to = Decimal(str(p["ratio_to"]))
        except (KeyError, InvalidOperation, TypeError, ValueError):
            raise Rejected("Invalid stock_split payload")

        ratio = ratio_to / ratio_from

        # Scale quantity of each lot; total cost stays the same
        for lot in self.lots[cid][symbol]:
            lot["quantity"] = lot["quantity"] * ratio
            lot["initial_quantity"] = lot["initial_quantity"] * ratio

        # Update tracked position
        if symbol in self.positions[cid]:
            self.positions[cid][symbol] = self.positions[cid][symbol] * ratio

        inverse_ratio = ratio_from / ratio_to
        def undo_split():
            for lot in self.lots[cid][symbol]:
                lot["quantity"] = lot["quantity"] * inverse_ratio
                lot["initial_quantity"] = lot["initial_quantity"] * inverse_ratio
            if symbol in self.positions[cid]:
                self.positions[cid][symbol] = self.positions[cid][symbol] * inverse_ratio
        self.undo_actions[ev["event_id"]].append(undo_split)

        return []  # No legs

    def on_symbol_change(self, p, ev):
        try:
            cid = p["customer_id"]
            old_sym = p["old_symbol"]
            new_sym = p["new_symbol"]
        except KeyError:
            raise Rejected("Invalid symbol_change payload")

        # Re-key lots
        moved_lots = False
        if old_sym in self.lots[cid]:
            self.lots[cid][new_sym] = self.lots[cid].pop(old_sym)
            moved_lots = True

        # Re-key positions
        moved_pos = False
        if old_sym in self.positions[cid]:
            self.positions[cid][new_sym] = self.positions[cid].pop(old_sym)
            moved_pos = True

        def undo_symbol_change():
            if moved_lots and new_sym in self.lots[cid]:
                self.lots[cid][old_sym] = self.lots[cid].pop(new_sym)
            if moved_pos and new_sym in self.positions[cid]:
                self.positions[cid][old_sym] = self.positions[cid].pop(new_sym)
        self.undo_actions[ev["event_id"]].append(undo_symbol_change)

        return []  # No legs

    def on_reversal(self, p, ev):
        try:
            original_event = p["reverses_event_id"]
            original_legs = self.posted_legs.pop(original_event)
        except KeyError:
            raise Rejected("Original event not found")

        if original_event in self.reversed_events:
            raise Rejected("Already reversed")

        self.reversed_events.add(original_event)

        if original_event in self.undo_actions:
            for action in reversed(self.undo_actions.pop(original_event)):
                action()

        reversed_legs = []

        for l in original_legs:
            reversed_legs.append(
                leg(
                    l["account"],
                    l["customer_id"],
                    debit=l["credit"],
                    credit=l["debit"]
                )
            )

        return reversed_legs

    # -- helpers -------------------------------------------------------------
    def consume_fifo(self, customer_id: str, symbol: str, quantity: Decimal) -> tuple[Decimal, callable]:
        """Remove FIFO lots for a sell. Returns (total_cost_basis, undo_closure)."""
        lots = self.lots[customer_id][symbol]

        if sum(lot["quantity"] for lot in lots) < quantity:
            raise Rejected("Oversell")

        remaining = quantity
        total_cost = ZERO
        
        restored_lots = []

        while remaining > 0:
            lot = lots[0]

            if lot["quantity"] <= remaining:
                total_cost += lot["cost"]
                remaining -= lot["quantity"]
                restored_lots.append((lot.copy(), True))
                lots.pop(0)
            else:
                new_qty = lot["quantity"] - remaining
                # To prevent drift, we compute exactly what the residual fractional cost
                # basis SHOULD be based on the initial lot parameters.
                new_cost = money(lot["initial_cost"] * new_qty / lot["initial_quantity"])
                
                # The cost posted to the journal is the exact difference, forcing the 
                # ledger to balance without accumulating recursive rounding drift.
                cost_used = lot["cost"] - new_cost

                total_cost += cost_used
                restored_lots.append((lot.copy(), False))

                lot["cost"] = new_cost
                lot["quantity"] = new_qty

                remaining = Decimal("0")

        if not lots:
            del self.lots[customer_id][symbol]
            if not self.lots[customer_id]:
                del self.lots[customer_id]

        def undo():
            target_lots = self.lots[customer_id][symbol]
            for original_lot, was_removed in reversed(restored_lots):
                if was_removed:
                    target_lots.insert(0, original_lot)
                else:
                    target_lots[0] = original_lot

        return money(total_cost), undo

    # -- reporting ----------------------------------------------------------
    def snapshot(self) -> dict:
        """What a checkpoint_request wants: your whole state, right now.

        Report every account you have ever posted to, including any that have
        netted back to zero. Trial balance values are debit-positive, so
        liabilities carry a negative sign.
        """
        tb: dict[str, Decimal] = defaultdict(lambda: ZERO)
        
        # Explicitly seed all seen accounts so none are omitted if zero
        for acct in self.accounts_seen:
            tb[acct] = ZERO
            
        for (_cid, acct), bal in self.balances.items():
            tb[acct] += bal

        customers: dict[str, dict] = {}
        for (cid, acct), bal in self.balances.items():
            c = customers.setdefault(cid, {"wallet_cash": ZERO,
                                           "cash_hold": ZERO, "positions": {}})
            if acct == "2010":
                c["wallet_cash"] += -bal          # a liability, so credit-positive

        # Build positions from self.positions
        for cid, symbols in self.positions.items():
            c = customers.setdefault(cid, {"wallet_cash": ZERO,
                                           "cash_hold": ZERO, "positions": {}})
            for symbol, total_qty in symbols.items():
                if total_qty > ZERO:
                    lots = self.lots[cid][symbol]
                    total_cost = sum(lot["cost"] for lot in lots)
                    c["positions"][symbol] = {
                        "quantity": f"{total_qty:f}",
                        "cost_basis": str(money(total_cost))
                    }

        # Build cash hold from open buy orders
        for order_id, order in self.orders.items():
            cid = order.get("customer_id")
            if not cid:
                continue
            c = customers.setdefault(cid, {"wallet_cash": ZERO,
                                           "cash_hold": ZERO, "positions": {}})
            side = order.get("side")
            if side == "buy":
                try:
                    c["cash_hold"] += order.get("hold", ZERO)
                except (InvalidOperation, TypeError, ValueError):
                    pass

        return {
            "trial_balance": {a: str(money(v)) for a, v in sorted(tb.items())},
            "customers": {cid: {"wallet_cash": str(money(c["wallet_cash"])),
                                "cash_hold": str(money(c["cash_hold"])),
                                "positions": c["positions"]}
                          for cid, c in sorted(customers.items())},
        }


class Rejected(Exception):
    """Raise from a handler for an event you refuse to post.

    An oversell, a reversal of something you never received, a payload that
    will not parse. Rejecting one event and carrying on beats stopping: a
    server that stalls misses everything after it.
    """
