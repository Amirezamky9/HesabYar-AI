"""Moadian test fixtures (US-404).

The valid invoice payload is modelled on the worked example in the
official technical instruction «دستورالعمل فنی نحوه اتصال به سامانه
مودیان» (§"فرآیند امضای صورتحساب"): same header/body values, with
``inty`` set to 1 (the common type-1 invoice) and ``tsstam`` added
(the example excerpt cuts off before it). The seller economic code
is the example's own 14-digit code (32652362589632) — see
``docs/moadian.md`` §"Assumptions" on the 10-vs-14-digit ambiguity.

All values are the instruction's published test data — no real
taxpayer data.
"""

from __future__ import annotations

import copy
from typing import Any

#: The instruction's example tax id (22 characters).
EXAMPLE_TAXID = "AA56CD0E0620002F2B4E78"


def taxid_for(n: int) -> str:
    """Return a distinct 22-character fixture tax id for ``n``."""
    return f"TS{n:020d}"


def valid_invoice_payload(
    *,
    taxid: str = EXAMPLE_TAXID,
    serial: str = "0002F2B4E7",
    **header_overrides: Any,
) -> dict[str, Any]:
    """Return a fresh valid Moadian invoice payload (Table 7).

    ``header_overrides`` replace header fields (e.g.
    ``inty=2``); pass ``body=[...]`` via ``header_overrides`` is not
    supported — mutate the returned dict instead.
    """
    header: dict[str, Any] = {
        "taxid": taxid,
        "indatim": "1655620821274",
        "indati2m": "1655620821274",
        "inty": 1,
        "inno": serial,
        "irtaxid": None,
        "inp": 1,
        "ins": 1,
        "tins": 32652362589632,
        "tob": 1,
        "bid": None,
        "tinb": None,
        "sbc": None,
        "bpc": None,
        "bbc": None,
        "ft": None,
        "bpn": None,
        "scln": None,
        "scc": None,
        "crn": None,
        "billid": None,
        "tprdis": 1000000,
        "tdis": 0,
        "tadis": 1000000,
        "tvam": 90000,
        "todam": 0,
        "tbill": 1090000,
        "setm": 1,
        "cap": 90000,
        "insp": 0,
        "tvop": 90000,
        "dpvb": None,
        "tax17": None,
    }
    header.update(header_overrides)
    return {
        "header": header,
        "body": [
            {
                "sstid": 2153265989636,
                "sstt": "پاستیل میوه ای شیبابا",
                "mu": 96,
                "am": 1,
                "fee": 1000000,
                "cfee": None,
                "cut": None,
                "exr": None,
                "prdis": 1000000,
                "dis": 0,
                "adis": 1000000,
                "vra": 0.09,
                "vam": 90000,
                "odt": None,
                "odr": None,
                "odam": None,
                "tsstam": 1090000,
            }
        ],
    }


#: One ready-made valid payload (deep-copy before mutating).
VALID_INVOICE_PAYLOAD: dict[str, Any] = valid_invoice_payload()


def copy_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Deep-copy a payload fixture."""
    return copy.deepcopy(payload)
