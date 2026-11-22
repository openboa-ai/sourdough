"""Isolated wire fixture; deliberately does not import any company code."""

import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path


PROFILE = {
    "ledger": "shadow-cash",
    "endpoint": "isolated-protocol-fixture",
    "termsHash": "sealed-common-terms",
    "settlement": "USD",
    "funded": 1000,
    "mode": "paper",
    "endpointId": "emulated-destination",
    "mark": 100,
    "takerBps": 50,
}


def handle(database, request):
    operation = request["op"]
    if operation == "initialize":
        database.execute(
            "CREATE TABLE fixture (withhold INTEGER NOT NULL, partial INTEGER NOT NULL, delay INTEGER NOT NULL)"
        )
        database.execute(
            "INSERT INTO fixture VALUES (?,?,?)",
            (request["withhold"], request["partial"], request["delay"]),
        )
        database.execute(
            """CREATE TABLE remote_order (
                   client_id TEXT PRIMARY KEY NOT NULL,
                   client_action TEXT NOT NULL,
                   amount INTEGER NOT NULL,
                   ceiling INTEGER NOT NULL
               )"""
        )
        database.execute(
            """CREATE TABLE remote_fill (
                   client_id TEXT NOT NULL, execution_id TEXT PRIMARY KEY,
                   executed INTEGER NOT NULL, price INTEGER NOT NULL, charge INTEGER NOT NULL
               )"""
        )
        database.execute(
            "CREATE TABLE remote_cancel (client_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, executed INTEGER NOT NULL)"
        )
        database.execute(
            "CREATE TABLE deferred_send (client_id TEXT PRIMARY KEY, request TEXT NOT NULL)"
        )
        return {}
    withheld, partial, delay = database.execute(
        "SELECT withhold,partial,delay FROM fixture"
    ).fetchone()
    profile = PROFILE | ({"takerBps": 0} if partial else {})
    if operation == "account":
        return profile
    if operation == "publish":
        database.execute("UPDATE fixture SET withhold=0")
        return {}
    if request.get("ledger") != profile["ledger"]:
        raise ValueError("Fixture does not observe or accept another account")
    identity = request["clientId"]
    if operation == "deliverFixture":
        (pending,) = database.execute(
            "SELECT request FROM deferred_send WHERE client_id=?", (identity,)
        ).fetchone()
        database.execute("UPDATE fixture SET delay=0")
        database.execute("DELETE FROM deferred_send WHERE client_id=?", (identity,))
        return handle(database, json.loads(pending))
    if operation == "send":
        if (
            request.get("termsHash") != profile["termsHash"]
            or request["ceiling"] < profile["mark"]
        ):
            raise ValueError("Intent is outside the emulated common subset")
        if delay:
            database.execute(
                "INSERT INTO deferred_send VALUES (?,?)",
                (identity, json.dumps(request)),
            )
            return {"ack": None}
        amount = request["amount"]
        executed = 2 if partial else amount
        fee = (executed * profile["mark"] * profile["takerBps"] + 5000) // 10000
        database.execute(
            "INSERT INTO remote_order VALUES (?,?,?,?)",
            (identity, request["clientAction"], amount, request["ceiling"]),
        )
        database.execute(
            "INSERT INTO remote_fill VALUES (?,?,?,?,?)",
            (identity, "execution:" + identity, executed, profile["mark"], fee),
        )
        return {"ack": None if withheld else identity}
    order = database.execute(
        "SELECT amount FROM remote_order WHERE client_id=?", (identity,)
    ).fetchone()
    if partial and order and operation == "cancelFixture":
        database.execute(
            "INSERT INTO remote_cancel VALUES (?,?,?)",
            (identity, "cancel:" + identity, 3),
        )
        return {}
    if partial and order and operation == "lateFillFixture":
        database.execute(
            "INSERT INTO remote_fill VALUES (?,?,?,?,?)",
            (identity, "late:" + identity, 1, profile["mark"], 0),
        )
        return {}
    cancellation = database.execute(
        "SELECT event_id,executed FROM remote_cancel WHERE client_id=?", (identity,)
    ).fetchone()
    (executed,) = database.execute(
        "SELECT COALESCE(sum(executed),0) FROM remote_fill WHERE client_id=?",
        (identity,),
    ).fetchone()
    corroborated = order and executed == (cancellation[1] if cancellation else order[0])
    if operation == "position":
        return {"openLots": executed} if not withheld and corroborated else {}
    if operation == "terminal":
        if withheld or cancellation is None or (request["sync"] and not corroborated):
            return {}
        return {"eventId": cancellation[0], "cumExecuted": cancellation[1]}
    if operation in ("history", "accountSync"):
        facts = (
            []
            if withheld or (operation == "accountSync" and not corroborated)
            else [
                dict(
                    zip(
                        (
                            "clientId",
                            "amount",
                            "ceiling",
                            "executionId",
                            "price",
                            "charge",
                            "executed",
                        ),
                        row,
                        strict=True,
                    )
                )
                for row in database.execute(
                    """SELECT orders.client_id,orders.amount,orders.ceiling,
                          fill.execution_id,fill.price,fill.charge,fill.executed
                   FROM remote_order orders JOIN remote_fill fill USING(client_id)
                   WHERE orders.client_id=? ORDER BY fill.rowid""",
                    (identity,),
                )
            ]
        )
        return {"trades" if operation == "accountSync" else "history": facts}
    raise ValueError("Unsupported fixture operation")


if __name__ == "__main__":
    journal = Path(sys.argv[1])
    with closing(sqlite3.connect(journal)) as database, database:
        result = handle(database, json.load(sys.stdin))
    print(json.dumps(result))
