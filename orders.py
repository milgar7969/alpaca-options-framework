"""
Order manager.

Alpaca options reality:
  - BUY:    simple limit order works fine
  - SELL:   submit_order fails ("insufficient buying power") — treated as new short
  - CLOSE:  close_position() works — Alpaca recognises it as closing a long
  - BRACKET: not supported for options ("complex orders not supported")

So: buy_limit() for entry, close_position() for all exits.
"""

import asyncio
import datetime
import logging
from typing import Optional

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import (
    OrderSide, TimeInForce, OrderStatus, OrderType,
    AssetClass, PositionIntent, QueryOrderStatus,
)
from alpaca.trading.requests import (
    LimitOrderRequest, ClosePositionRequest, GetOrdersRequest,
)
from alpaca.trading.models import Order

import config

logger = logging.getLogger(__name__)

FILL_POLL_INTERVAL = 2    # seconds between fill-status polls
FILL_TIMEOUT       = 30   # seconds before giving up on a fill


def _make_client() -> TradingClient:
    return TradingClient(
        api_key    = config.ALPACA_API_KEY,
        secret_key = config.ALPACA_API_SECRET,
        paper      = config.PAPER,
    )


class OrderManager:
    def __init__(self):
        self._client = _make_client()

    # ── Entry ─────────────────────────────────────────────────────────────────

    async def buy_limit(
        self,
        symbol:   str,
        qty:      int,
        limit_px: float,
    ) -> Optional[Order]:
        """Simple limit buy. Returns filled Order or None."""
        limit_px = round(limit_px, 2)
        req = LimitOrderRequest(
            symbol          = symbol,
            qty             = qty,
            side            = OrderSide.BUY,
            time_in_force   = TimeInForce.DAY,
            limit_price     = limit_px,
            position_intent = PositionIntent.BUY_TO_OPEN,
        )
        try:
            order = self._client.submit_order(req)
            logger.info("BUY submitted: %s qty=%d limit=%.2f id=%s",
                        symbol, qty, limit_px, order.id)
        except Exception as e:
            logger.error("BUY submit failed: %s", e)
            return None

        try:
            filled = await self._wait_for_fill(order.id)
        except asyncio.CancelledError:
            # Task was cancelled while waiting for fill — cancel the Alpaca order
            # before propagating so it doesn't sit open unmonitored.
            logger.warning("BUY fill-wait cancelled — cancelling Alpaca order %s", order.id)
            self._cancel(order.id)
            raise

        if filled is None:
            logger.warning("BUY timed out, cancelling: %s", order.id)
            self._cancel(order.id)
            # Give Alpaca a moment then verify the order didn't fill after timeout
            await asyncio.sleep(1)
            try:
                status = self._client.get_order_by_id(str(order.id))
                if status.status == OrderStatus.FILLED:
                    logger.warning(
                        "BUY filled after timeout — recovering fill: %s avg=%.2f",
                        order.id, float(status.filled_avg_price or 0),
                    )
                    return status   # return filled order so position is tracked locally
            except Exception as e:
                logger.error("Post-timeout order check failed: %s", e)

        return filled

    # ── Exit ──────────────────────────────────────────────────────────────────

    async def close_position(
        self,
        symbol: str,
        qty:    int,
    ) -> Optional[Order]:
        """
        Close an existing long via Alpaca's close_position endpoint.
        Works where submit_order(SELL) fails with margin errors.
        """
        try:
            order = self._client.close_position(
                symbol, close_options=ClosePositionRequest(qty=str(qty))
            )
            logger.info("CLOSE submitted: %s qty=%d id=%s", symbol, qty, order.id)
        except Exception as e:
            logger.error("CLOSE failed: %s", e)
            return None

        filled = await self._wait_for_fill(order.id)
        if filled is None:
            logger.warning("CLOSE timed out, cancelling: %s", order.id)
            self._cancel(order.id)
        return filled

    # ── Position polling ──────────────────────────────────────────────────────

    def get_open_positions(self) -> list:
        try:
            return self._client.get_all_positions()
        except Exception as e:
            logger.error("get_open_positions failed: %s", e)
            return []

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _wait_for_fill(self, order_id: str) -> Optional[Order]:
        deadline = asyncio.get_event_loop().time() + FILL_TIMEOUT
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(FILL_POLL_INTERVAL)
            try:
                order = self._client.get_order_by_id(order_id)
            except Exception as e:
                logger.error("Error polling order %s: %s", order_id, e)
                continue
            if order.status == OrderStatus.FILLED:
                logger.info("Filled: %s avg=%.2f", order_id,
                            float(order.filled_avg_price or 0))
                return order
            if order.status in (OrderStatus.CANCELLED, OrderStatus.EXPIRED,
                                OrderStatus.REJECTED):
                logger.warning("Order %s terminal: %s", order_id, order.status)
                return None
        return None

    def _cancel(self, order_id: str):
        try:
            self._client.cancel_order_by_id(order_id)
        except Exception as e:
            logger.error("Cancel failed %s: %s", order_id, e)

    def cancel_all_options(self):
        try:
            for o in self._client.get_orders():
                if o.asset_class == AssetClass.US_OPTION:
                    self._cancel(str(o.id))
        except Exception as e:
            logger.error("cancel_all_options failed: %s", e)

    def get_fill_price(self, order: Order) -> float:
        try:
            return float(order.filled_avg_price)
        except (TypeError, ValueError):
            return 0.0
