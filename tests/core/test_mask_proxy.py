from __future__ import annotations

import logging

from unshackle.core.utils.redact import mask_proxy


def test_mask_proxy_hides_userinfo() -> None:
    uri = "https://user:secret@in-mum.prod.surfshark.com:443"
    assert mask_proxy(uri) == "https://xxxxx:xxxxx@in-mum.prod.surfshark.com:443"


def test_mask_proxy_hides_userinfo_even_in_debug() -> None:
    logger = logging.getLogger()
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        uri = "https://user:secret@in-mum.prod.surfshark.com:443"
        masked = mask_proxy(uri, allow_debug=True)
        assert "secret" not in masked
        assert masked == "https://xxxxx:xxxxx@in-mum.prod.surfshark.com:443"
    finally:
        logger.setLevel(previous)
