"""Polygon (Bor): длинный extraData — без POA middleware web3 падает с ExtraDataLengthError."""

from __future__ import annotations

from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware


def make_polygon_web3(rpc_url: str) -> Web3:
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    return w3
