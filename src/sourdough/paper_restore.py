"""Reconcile a settled original managed paper journal into an isolated replacement."""

from contextlib import closing
from pathlib import Path
from sqlite3 import Connection

from .company_checkpoint import CheckpointRefused
from .company_restore import CoreRestore, _restore_checkpoint
from .company_run import _recover
from .duty_run import POSITION_REVIEW
from .nautilus_paper import CONNECTOR, NautilusPaperVenue
from .nautilus_runtime import paper_venue
from .paper_account import read_current_binding, reopen_binding
from .paper_sender_lease import hold_sender_lease, verify_sender_lease
from .storage.db import open_reader, read_tx
from .storage.schema import is_domain_installed
from .writer_lease import hold_company_writer


def _settle_candidate(shadow: Path, company_id: str, venue: NautilusPaperVenue) -> None:
    binding = reopen_binding(shadow, company_id)
    account = binding.account
    if (
        account.connector_revision != CONNECTOR
        or venue.observe_account(account.account_id) != account
    ):
        raise CheckpointRefused("Restore requires the original managed paper binding")
    scope = (account.account_id, binding.revision)
    cut = venue.read_restore_cut(account.account_id)
    with closing(open_reader(shadow)) as candidate, read_tx(candidate):
        orders = (
            tuple(
                candidate.execute(
                    """SELECT order_id FROM paper_order
                       WHERE account_id = ? AND mandate_revision = ?
                       ORDER BY order_id""",
                    scope,
                )
            )
            if is_domain_installed(candidate, "paper")
            else ()
        )
    if orders != tuple((order_id,) for order_id in cut.order_ids):
        raise CheckpointRefused(
            "Paper restore does not account for every original order"
        )

    shadow_leases = shadow.parent / ".restore-leases"
    with hold_company_writer(
        shadow, company_id, lease_directory=shadow_leases
    ) as writer:
        recovery, blocked = _recover(writer, venue, POSITION_REVIEW)
        if recovery.unresolved or blocked:
            raise CheckpointRefused(
                "Original paper effects could not settle on the replacement"
            )
        with closing(open_reader(shadow)) as candidate, read_tx(candidate):
            fills = (
                tuple(
                    candidate.execute(
                        """SELECT f.fill_id, f.order_id, f.quantity,
                                  f.price_minor_units, p.fee_minor_units
                           FROM paper_fill f
                           JOIN paper_order o ON o.order_id = f.order_id
                           LEFT JOIN paper_fill_fee p ON p.fill_id = f.fill_id
                           WHERE o.account_id = ? AND o.mandate_revision = ?
                           ORDER BY f.fill_id""",
                        scope,
                    )
                )
                if is_domain_installed(candidate, "paper")
                else ()
            )
        if any(fee is None for _, _, _, _, fee in fills):
            raise CheckpointRefused("Paper fill is missing fee record")
        if (
            fills != cut.fills
            or sum(fact[2] for fact in fills) != cut.balance.position_lots
        ):
            raise CheckpointRefused(
                "Replacement fill history does not match the original venue"
            )
        if venue.read_restore_cut(account.account_id) != cut:
            raise CheckpointRefused(
                "Original venue changed while verifying the replacement"
            )


def restore_managed_paper_checkpoint(
    store: Path, company_id: str, archive: Path, destination: Path, engine: Path
) -> CoreRestore:
    venue = paper_venue(store, engine)
    with hold_sender_lease(venue.journal) as (descriptor, key):
        scope_id, plan = venue.configuration()
        if plan.company_id != company_id or plan.tape_revision is not None:
            raise CheckpointRefused(
                "Managed venue does not belong to this paper company"
            )

        def check_original(connection: Connection) -> None:
            verify_sender_lease(venue.journal, descriptor, key)
            binding = read_current_binding(connection, company_id)
            if (
                binding.account.connector_revision != CONNECTOR
                or binding.account.destination_id != scope_id
            ):
                raise CheckpointRefused(
                    "Managed venue is not the original company binding"
                )

        def reconcile(shadow: Path) -> None:
            _settle_candidate(shadow, company_id, venue)
            verify_sender_lease(venue.journal, descriptor, key)

        return _restore_checkpoint(
            store,
            company_id,
            archive,
            destination,
            check_original=check_original,
            reconcile=reconcile,
            external_scope=(scope_id, *key),
        )
