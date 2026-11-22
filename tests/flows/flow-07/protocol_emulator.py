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
        database.execute("CREATE TABLE visibility (withhold INTEGER NOT NULL)")
        database.execute("INSERT INTO visibility VALUES (?)", (request["withhold"],))
        database.execute(
            """CREATE TABLE remote_order (
                   client_id TEXT PRIMARY KEY NOT NULL,
                   client_action TEXT NOT NULL,
                   amount INTEGER NOT NULL,
                   ceiling INTEGER NOT NULL,
                   execution_id TEXT NOT NULL,
                   price INTEGER NOT NULL,
                   charge INTEGER NOT NULL
               )"""
        )
        return {}
    if operation == "account":
        return PROFILE
    if operation == "publish":
        database.execute("UPDATE visibility SET withhold=0")
        return {}
    if request.get("ledger") != PROFILE["ledger"]:
        raise ValueError("Fixture does not observe or accept another account")
    (withheld,) = database.execute("SELECT withhold FROM visibility").fetchone()
    if operation == "send":
        if request.get("termsHash") != PROFILE["termsHash"] or request["ceiling"] < 100:
            raise ValueError("Intent is outside the emulated common subset")
        identity = request["clientId"]
        amount = request["amount"]
        fee = (amount * 100 * 50 + 5000) // 10000
        database.execute(
            "INSERT INTO remote_order VALUES (?,?,?,?,?,?,?)",
            (
                identity,
                request["clientAction"],
                amount,
                request["ceiling"],
                "execution:" + identity,
                100,
                fee,
            ),
        )
        return {"ack": None if withheld else identity}
    if operation == "position":
        row = database.execute(
            "SELECT amount FROM remote_order WHERE client_id=?", (request["clientId"],)
        ).fetchone()
        return {"openLots": row[0]} if not withheld and row is not None else {}
    if operation in ("history", "accountSync"):
        row = database.execute(
            "SELECT client_id,amount,ceiling,execution_id,price,charge FROM remote_order WHERE client_id=?",
            (request["clientId"],),
        ).fetchone()
        result = (
            {}
            if withheld or row is None
            else dict(
                zip(
                    ("clientId", "amount", "ceiling", "executionId", "price", "charge"),
                    row,
                    strict=True,
                )
            )
        )
        return (
            {"trades": [result] if result else []}
            if operation == "accountSync"
            else result
        )
    raise ValueError("Unsupported fixture operation")


if __name__ == "__main__":
    journal = Path(sys.argv[1])
    with closing(sqlite3.connect(journal)) as database, database:
        result = handle(database, json.load(sys.stdin))
    print(json.dumps(result))
