from .ledger import ReceiptLedger
from .router import validate_quasar_payload


def handle_quasar_event(
    payload: dict[str, object],
    ledger: ReceiptLedger,
) -> None:
    validated = validate_quasar_payload(payload)
    ledger.record(validated)
