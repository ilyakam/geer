QUASAR_LEDGER_CALLBACK_PATH = "/internal/quasar-ledger/callback-v7"


def validate_quasar_payload(payload: dict[str, object]) -> dict[str, object]:
    if "receipt_id" not in payload:
        raise ValueError("receipt_id is required")
    return payload
