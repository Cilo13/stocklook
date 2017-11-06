import asyncio
import logging
from time import sleep
from random import randint
from datetime import datetime, timedelta
from stocklook.config import config
from stocklook.crypto.gdax.api import Gdax, GdaxAPIError
from stocklook.crypto.gdax.order import GdaxOrder, GdaxOrderCancellationError
from stocklook.crypto.gdax.feeds.book_feed import GdaxBookFeed, BookSnapshot

logger = logging.getLogger(__name__)
logger.setLevel(config.get('LOG_LEVEL', logging.DEBUG))

class OrderLockError(Exception):
    pass

def generate_random_spread_value(spread_diff):
    spread_diff *= 100
    ri = randint(1, spread_diff)


class GdaxMMOrder(GdaxOrder):
    def __init__(self, market_maker, *args, op_order=None, **kwargs):
        self.market_maker = market_maker
        self._op_order = op_order
        self._fill_chain = list()
        self._locked = False
        self._prices = list()
        self._unlock_method = None
        GdaxOrder.__init__(self, *args, **kwargs)
    
    @property
    def m(self):
        return self.market_maker
    
    @property
    def op_order(self):
        return self._op_order

    @property
    def stop_amount(self):
        stop_pct = self.m.stop_pct
        op_order = self.op_order
        if not stop_pct:
            return None
        if op_order is None:
            return None

        if self.side == 'buy':
            return None

        op_price = op_order.price
        return op_price - (op_price * stop_pct)

    @property
    def locked(self):
        return self.locked

    def lock(self, unlock_method=None):
        self._locked = True
        self._unlock_method = unlock_method

    def unlock(self):
        if self._unlock_method is not None:
            try:
                if not self._unlock_method(self):
                    raise OrderLockError("Failed to unlock order")
            except Exception as e:
                raise OrderLockError("Unlock method failure: {}".format(e))

        self._locked = False

    def get_volume_until_fill(self):
        if self.side == 'buy':
            return self.m.book_feed.get_bid_depth(self.price)
        return self.m.book_feed.get_ask_depth(self.price)

    def get_amount_above_spread(self, spread=None, bid=None, ask=None):
        """
        Returns the difference between the order price and the current bid/ask based
        on a given spread target.

        :param spread: (int, float, default GdaxMMOrder.market_maker.max_spread)

        :param bid: (float, default stocklook.crypto.gdax.feeds.book_feed.BookSnapshot.lowest_ask)
        :param ask: (float, default stocklook.crypto.gdax.feeds.book_feed.BookSnapshot.highest_bid)
        :return:
        """
        if spread is None:
            spread = self.m.max_spread

        if self.side == 'sell':
            if bid is None:
                bid = self.m.get_book_snapshot().highest_bid[0]
            max_price = bid + spread
            return round(self.price - max_price, 2)
        else:
            if ask is None:
                ask = self.m.get_book_snapshot().lowest_ask[0]
            min_price = ask - spread
            return round(self.price - min_price, 2)

    def get_pnl(self, price=None):
        op = self._op_order
        if op is not None:
            if price is None:
                price = self.price
            if self.side == 'buy':
                buy_spend = self.size * price
                sell_spend = op.size * op.price
            else:
                buy_spend = op.size * op.price
                sell_spend = self.size * price
            return round(sell_spend - buy_spend, 2)
        return None
    
    def register_op_order(self, order):
        """
        Registers the opposide side of the trade to the order.
        For example:
            GdaxMMOrder1 BUY $300
            GdaxMMOrder2 SELL $300.40
            GdaxMMOrder1.register_op_order(GdaxMMOrder2)

        :param order:
        :return:
        """
        o_side = order.side
        my_side = self.side
        o_op_order = getattr(order, '_op_order', None)

        if o_side == my_side and o_op_order is not None:
            # This is an order replacement
            # of the same type.
            self._op_order = o_op_order
        elif o_side != my_side:
            # The order is a different side
            # and therefore an opposite order.
            self._op_order = order
        else:
            raise AttributeError("Cannot register op_order "
                                 "of same type '{}'".format(o_side))

    @staticmethod
    def from_gdax_order(order, market_maker, op_order=None):
        if isinstance(order, GdaxOrder):
            d = order.to_dict()
            mm_order = GdaxMMOrder(market_maker,
                                   order.gdax,
                                   order.product,
                                   op_order=op_order,
                                   **d)
            return mm_order
        return order

    def get_price_adjusted_to_spread(self, spread=None, aggressive=True, amount_above=None, factor=0.8, min_profit=0.01):

        if not amount_above:
            if not spread:
                if aggressive:
                    # aggressive orders use tight spreads
                    spread = self.m.min_spread
                else:
                    spread = self.m.max_spread
            amount_above = self.get_amount_above_spread(spread=spread)

        if amount_above:
            price = round(self.price - (amount_above*factor), 2)
        else:
            price = round(self.price, 2)

        op_order = self.op_order
        if op_order is not None and min_profit is not None:
            min_price = op_order.price + min_profit
            if price < min_price:
                price = min_price

        return price

    def get_price_adjusted_to_other_prices(self, other_prices=None, aggressive=True, step=0.03, min_profit=0.01):
        if other_prices is None:
            if self.side == 'buy':
                other_prices = list(self.m.buy_orders.values())
            else:
                other_prices = list(self.m.buy_orders.values())
            other_prices = list(sorted([o.price for o in other_prices]))

        other_prices = [p for p in other_prices
                        if p != self.price]

        my_price = self.price
        my_min = self.get_price_adjusted_to_spread(spread=None,
                                                   aggressive=aggressive,
                                                   min_profit=min_profit)
        if not other_prices:
            return my_min

        def adj_price(p, increment=True):
            max_p = p + (step*2)
            min_p = p - (step*2)
            check_p = [x for x in other_prices
                       if x > min_p and x < max_p]
            p = round(p, 2)
            while check_p:
                if increment:
                    p += step
                else:
                    p += step
                p = round(p, 2)
                max_p = p + (step * 2)
                min_p = p - (step * 2)
                check_p = [x for x in other_prices
                           if x >= min_p
                           and x <= max_p]
            return p

        min_price = min(other_prices)
        max_price = max(other_prices)
        max_and_step = max_price + step
        min_and_step = min_price - step

        if self.side == 'buy':

            if aggressive:
                # Aggressive buys need to be near top
                if my_min >= max_and_step:
                    return adj_price(my_min, increment=True)
                else:
                    return adj_price(my_min, increment=True)

            else:
                # Non aggressive buys - position somewhere healthy
                return adj_price(my_min, increment=False)

        elif self.side == 'sell':
            if aggressive:
                # Aggressive sells should be near bottom
                if my_min >= min_and_step:
                    return adj_price(my_min, increment=False)
                else:
                    return adj_price(my_min, increment=True)
            else:
                # Non aggressive sells - position somewhere healthy
                return adj_price(my_min, increment=True)

    def get_price_adjusted_to_profit_target(self, min_profit=0.01):
        """
        Returns a price that a sell order needs to be sold at
        in order to reach a given profit dollar amount.
        :param min_profit: (float, int, default 0.01)
            The minimum $ of profit that the sale must bring.
        :return: (None, float)

        """
        price = self.price
        pnl = self.get_pnl(price)
        if pnl is None:
            return self.price
        while pnl < min_profit:
            price += 0.01
        return price

    def get_other_order_prices(self, side='buy'):
        """
        Returns a list of order prices for a given side (buy or sell).
        :param side:
        :return:
        """
        return [round(o.price,2) for o in self.m._orders.values() if o.side == side]

    def get_price_adjusted_to_ticker(self, price=None, ticker=None, aggressive=True, adjust_vs_open=True):
        """
        Returns a price adjusted against the current ticker.
            - buy price greater than ticker gets decreased by the spread
            - sell price lower than ticker gets increased by the spread

        :param price: (float, default GdaxMMOrder.price)
            The price to evaluate against the ticker.

        :param ticker: (dict, default GdaxMMOrder.market_maker.book_feed.get_current_ticker())
            A dictionary containing ticker details.

        :param aggressive: (bool, default True)
            aggressive orders use GdaxMMOrder.market_maker.min_spread
            non-aggressive prices use GdaxMMOrder.market_market.max_spread
            This is used to increment or decrement the price

        :param adjust_vs_open (bool, default True)
             True adjusts price in half-spread increments (more profitably) to make unique
                  from other buy or sell orders.
            False just returns the ticker-adjusted price.
        :return:
        """
        if ticker is None:
            ticker = self.m.book_feed.get_current_ticker()
        if price is None:
            price = self.price

        if aggressive:
            spread = self.m.min_spread
        else:
            spread = self.m.max_spread

        if ticker:
            ticker_price = float(ticker['price'])
            if self.side == 'buy':
                if price >= ticker_price - spread:

                    price = ticker_price - spread

            elif price <= ticker_price + spread:
                    price = ticker_price + spread

        o_prices = self.get_other_order_prices(side=self.side)
        spread_add = round(spread/2, 2)

        while price in o_prices:
            if self.side == 'buy':
                # decrease buy price
                price -= spread_add
            else:
                # increase sell price
                price += spread_add

        return price

    def get_price_adjusted_to_wall(self, min_idx=2, wall_size=50, bump_value=0.01):
        snap = self.m.get_book_snapshot()
        data = (snap.bids if self.side == 'buy' else snap.asks)

        for idx, contents in enumerate(data):
            if contents[1] >= wall_size and idx >= min_idx:
                if self.side == 'buy':
                    return contents[0] + bump_value
                else:
                    return contents[0] - bump_value


class GdaxMarketMaker:
    def __init__(self,
                 book_feed=None,
                 product_id=None,
                 gdax=None,
                 auth=True,
                 max_spread=0.10,
                 min_spread=0.05,
                 stop_pct=0.05,
                 interval=2,
                 wall_size=None,
                 spend_pct=0.01,
                 max_open_buys=6,
                 max_open_sells=12,
                 manage_existing_orders=True,
                 aggressive=True):
        if book_feed is None:
            book_feed = GdaxBookFeed(product_id=product_id,
                                     gdax=gdax,
                                     auth=auth)
        if gdax is None:
            gdax = book_feed.gdax

        if product_id is None:
            product_id = book_feed.product_id

        self.book_feed = book_feed
        self.product_id = product_id
        self.gdax = gdax
        self.auth = auth
        self._wall_size = wall_size
        self.interval = interval
        self._t_time = datetime.now()
        self.spend_pct = spend_pct
        self.max_spread = max_spread
        self.min_spread = min_spread
        self.stop_pct = stop_pct
        self.max_open_buys = max_open_buys
        self.max_open_sells = max_open_sells
        self.manage_existing_orders = manage_existing_orders
        self.aggressive = aggressive

        self.stop = False
        self._book_snapshot = None
        self._last_ticker = dict()
        self._orders = dict()
        self._fills = dict()

    def place_order(self, price, size, side='buy', op_order=None, adjust_vs_open=True,
                    adjust_vs_wall=True, check_size=True, check_ticker=True, aggressive=True):
        """
        Places new buy and sell orders.

        :param price: (float)
            The desired price to place the order on.
            This price may be altered depending on settings.

        :param size: (float)
            The desired quantity to buy or sell.
            This quantiy may be altered depending on settings.

        :param side: (str, ('buy', 'sell), default 'buy')
            buying or selling

        :param op_order: (GdaxMMOrder, default None)
            The opposite order (or previous canceled order)
            Gets registered to the GdaxMMOrder if not None.

        :param adjust_vs_open: (bool, default True)
            True sets price to GdaxMMOrder.get_price_adjusted_to_other_prices

        :param adjust_vs_wall: (bool, default True)
            -- IN PROGRESS --
            True adjusts buy orders to be just above the next bid wall
            False adjusts sell orders just beneath the next ask wall
        :param check_size: (bool, default True)
            True adjusts the size of buy orders to match GdaxMMOrder.position_size

        :param check_ticker: (bool, default
        :param aggressive:
        :return:
        """

        if adjust_vs_wall:
            o_order = self.lowest_open_order
            if o_order is not None:
                o_price = o_order.price
                if side == 'buy':
                    pass
                # TODO: Finish this and adjust price against the wall

        # Adjust position size down on buy orders
        # based on total value of account
        if check_size and side == 'buy':
            pos_size = self.position_size
            if pos_size < 0.01:
                return None
            if size > pos_size:
                size = pos_size

        order = GdaxMMOrder(self,
                            self.gdax,
                            self.product_id,
                            op_order=op_order,
                            order_type='limit',
                            side=side,
                            price=price,
                            size=size)

        if adjust_vs_open:
            open_price = order.get_price_adjusted_to_other_prices(step=round(self.max_spread/2, 2),
                                                                   min_profit=self.min_spread,
                                                                   aggressive=aggressive)
            if open_price:
                order.price = open_price

        # Adjust price against ticker if needed
        # As we don't want a market order.
        if check_ticker:
            tick_price = order.get_price_adjusted_to_ticker(aggressive=aggressive)
            if tick_price:
                order.price = tick_price

        logger.debug("new: {} {} {} @ {}".format(
            side, size, self.product_id, price))

        order.post()

        assert order.id is not None
        self._orders[order.id] = order

        return order

    @property
    def orders(self):
        open_orders = self.gdax.get_orders(status='open',
                                           paginate=False   #
                                           )
        open_ids = [o['id'] for o in open_orders]
        existing_keys = self._orders.keys()
        filled = [o_id for o_id in existing_keys
                  if o_id not in open_ids]
        [self.handle_fill(o) for o in filled]

        if self.manage_existing_orders:
            # Cache any orders placed on the account.
            new_keys = [o['id'] for o in open_orders
                        if o['id'] not in existing_keys]
            for o_data in open_orders:
                if o_data['id'] not in new_keys:
                    continue
                o = GdaxMMOrder(self, self.gdax, self.product_id)
                o.update(data=o_data)
                self._orders[o.id] = o

        return self._orders

    @property
    def wall_size(self):
        if self._wall_size is None or self._timeout():
            snap = self.get_book_snapshot()
            self._wall_size = snap.calculate_wall_size()
        return self._wall_size

    @property
    def lowest_open_order(self):
        if self._orders:
            orders = list(self._orders.values())
            return min(orders, key=lambda o: o.price)
        return None

    def _timeout(self):
        n = datetime.now()
        out = self._t_time + timedelta(seconds=5)
        if n > out:
            self._t_time = n
            return True
        return False

    @property
    def position_size(self):
        usd_acc = self.gdax.accounts['USD']
        snap = self.get_book_snapshot()
        balance = usd_acc.balance
        bid = float(snap.lowest_ask[0])
        spend_avail = balance * self.spend_pct
        size_avail = spend_avail / bid
        buy_orders = len(self.buy_orders)
        sells_open = len(self.sell_orders)
        if size_avail > 0.01 \
                and buy_orders < self.max_open_buys \
                and sells_open < self.max_open_sells:
            return size_avail
        return 0


    @property
    def position_spend(self):
        usd_acc = self.gdax.accounts['USD']
        balance = usd_acc.balance
        spend_avail = balance * self.spend_pct
        return spend_avail

    def shift_orders(self, snap: BookSnapshot, exclude=None):
        """
        Algorithm for shifting buy and sell orders up and down. Called once every
        interval and cancels/replaces many orders.

        Roughly follows these steps:
            - Compare buy and sell orders to ensure they aren't stacked on the same bid/asks
                - If stacked, shift the price down a fraction on bids, up a fraction on asks
            - Roughly evaluate MarketMaker.min_spread/max_spread on all positions
                - If a buy position is at a loss, do not sell
                  under MarketMaker.min_spread until MarketMaker.stop_pct has been reached.
                  Move sell order to min_spread at a minimum.
                - If a bid is too far under MarketMaker.max_spread - shift it up a few points
                  to encourage a taker to fill.

        Note: This could probably be made more efficient because orders are constantly being cancelled and replaced
        at different price points along the bid and ask, however, I kind of like it because
        I think the randomness can make it more difficult for bots to trade against your account.

        Note: With tight stops or serious downward momentum i expect this algorithm to lose...
              we need bullish or choppy market conditions in order for this to stay profitable.

        :param snap: (stocklook.crypto.gdax.feeds.boook_feed.BookSnapshot)
        :param exclude: (list, default None)
            A list of GdaxMMOrder.id to exclude.
        :return:
        """
        # TODO: Adjust spread based on market volatility/price action.
        orders = self.orders
        if not orders:
            return None

        exclude = ([] if not exclude else exclude)
        cancels = list()
        new_orders = list()
        ticker = self.book_feed.get_current_ticker()

        if ticker:
            p = float(ticker['price'])
        else:
            p = 0

        if not self._last_ticker and p > 0:
            self._last_ticker = p
        elif self._last_ticker != p and p > 0:
            self._last_ticker = p
            spread = (self.min_spread if self.aggressive else self.max_spread)

            for order_id, order in self._orders.copy().items():
                if order_id in exclude:
                    logger.debug("Excluding {} order at price "
                                 "{}".format(order.side, order.price))
                    continue

                if order.side == 'sell':
                    # check if order is stopped
                    stop = order.stop_amount
                    if stop and stop >= p:
                        logger.debug("shift_prices: order stopped. price: {}, "
                                     "stop: {}, ticker: {}".format(
                                      order.price, stop, p))
                        cancels.append((order_id, p + 0.01))
                        continue

                min_price = order.get_price_adjusted_to_spread(
                    aggressive=True, min_profit=spread)
                max_price = order.get_price_adjusted_to_spread(
                    aggressive=False, min_profit=spread)

                min_diff = round(order.price - min_price, 2)
                max_diff = round(max_price - order.price, 2)
                logger.debug("Evaluating {} order:\n"
                             "price {}\n"
                             "min {}\n"
                             "max {}\n"
                             "ticker {}\n"
                             "min diff {}\n"
                             "max diff {}".format(order.side, order.price, min_price,
                                                  max_price, p, min_diff, max_diff))

                check_price = order.get_price_adjusted_to_other_prices(
                    aggressive=self.aggressive, step=round(spread / 2, 2), )

                if order.side == 'buy':
                    # min_diff = max buy
                    # max_diff = min buy
                    if max_diff > spread:
                        # go for minimum spread
                        if check_price > order.price:
                            self.cancel_order(order.id)
                            new_order = self.place_order(check_price,
                                                         order.size,
                                                         side=order.side,
                                                         op_order=order,
                                                         adjust_vs_open=False)
                            new_orders.append(new_order)

                elif order.side == 'sell':
                    stop = order.stop_amount
                    stop_sell = round(p+(spread/2), 2)
                    if stop and stop >= p and order.price > stop_sell:
                        logger.debug("shift_prices: order stopped. price: {}, "
                                     "stop: {}, ticker: {}".format(
                            order.price, stop, p))
                        self.cancel_order(order.id)
                        new_order = self.place_order(stop_sell,
                                                     order.size,
                                                     side=order.side,
                                                     op_order=order,
                                                     adjust_vs_open=False,
                                                     check_size=False)
                        new_orders.append(new_order)

                    else:
                        if order.price > min_price:
                            # go for minimum spread
                            # first check against others
                            if order.price > check_price:
                                # clear for min spread
                                self.cancel_order(order.id)
                                new_order = self.place_order(check_price,
                                                             order.size,
                                                             side=order.side,
                                                             op_order=order,
                                                             adjust_vs_open=False)
                                new_orders.append(new_order)

        return new_orders

    @property
    def buy_orders(self):
        return {o_id: o for o_id, o in self._orders.items()
                if o.side == 'buy'}

    @property
    def sell_orders(self):
        return {o_id: o for o_id, o in self._orders.items()
                if o.side == 'sell'}

    def handle_fill(self, order_id, replace=True):
        order = self._orders.pop(order_id)
        if not order.is_filled():
            raise Exception("Order {} is not "
                            "filled.".format(order))
        self._fills[order_id] = order

        # Log PNL if possible.
        pnl = getattr(order, 'pnl', None)
        if pnl is not None:
            logger.info("Closed two-sided {} order, pnl: "
                        "${}".format(self.product_id, round(pnl, 2)))

        # Place the opposite order immediately
        if replace:
            buy_orders = self.buy_orders
            sell_orders = self.sell_orders
            spread = (self.min_spread if self.aggressive else self.max_spread)
            if order.side == 'buy':
                maxed = None
                new_side = 'sell'
                new_price = order.price + spread
            else:
                maxed = (len(buy_orders) > self.max_open_buys or
                         len(sell_orders) > self.max_open_sells)
                new_side = 'buy'
                new_price = order.price - spread

            if not maxed:
                new_order = self.place_order(new_price,
                                             order.size,
                                             side=new_side,
                                             op_order=order,
                                             adjust_vs_open=True,
                                             check_size=True,
                                             check_ticker=True,
                                             aggressive=self.aggressive)
            else:
                new_order = None
                logger.debug("Not replacing order {} as we're maxed.\n"
                             "Buys {}/{}\n"
                             "Sells {}/{}".format(
                    order_id, len(buy_orders), self.max_open_buys,
                    len(sell_orders), self.max_open_sells))
        else:
            new_order = None
        return new_order

    def cancel_order(self, order_id):
        order = self._orders.pop(order_id, None)
        logger.debug("Cancelling order: {}".format(order))
        try:
            check = order.cancel()
            logger.debug("Order cancel return: {}".format(check))
            assert check[0] in (order_id, None)
        except (GdaxOrderCancellationError,
                AttributeError,
                AssertionError,
                KeyError) as e:
            logger.error("Error cancelling order '{}': {}\n"
                         "Order: {}".format(order_id, e, order))
            raise
        except GdaxAPIError as e:
            if 'done' in str(e):
                self._orders[order_id] = order
                self.handle_fill(order_id, replace=True)
            elif 'not found' in str(e):
                pass
            else:
                raise

        return order

    def get_book_snapshot(self):
        if self._book_snapshot is None:
            book = self.book_feed.get_current_book()
            book['bids'].reverse()
            self._book_snapshot = BookSnapshot(book, self.book_feed)
        elif self._timeout():
            self._book_snapshot.refresh()
        return self._book_snapshot

    def map_open_orders_to_fills(self):
        fills = self.gdax.get_fills(product_id=self.product_id, paginate=False)
        open_orders = self.gdax.get_orders(status='open',
                                           paginate=False  #
                                           )
        existing_keys = [k for k, o in self._orders.items()
                         if o._op_order is not None and o.side == 'sell']
        added = list()

        for f in fills:
            f['price'] = round(float(f['price']), 2)
            f['fee'] = float(f['fee'])
            f['size'] = float(f['size'])

        for o_data in open_orders:
            if o_data['id'] in existing_keys:
                continue
            if o_data['side'] == 'buy':
                continue

            try:
                o = self._orders[o_data['id']]
            except KeyError:
                o = GdaxMMOrder(self, self.gdax, self.product_id)
                o.update(data=o_data)

            for f in fills:
                if f['side'] == 'sell':
                    continue
                if f['order_id'] in added:
                    continue
                if f['size'] == o.size:
                    op_order = GdaxMMOrder(self, self.gdax, self.product_id,
                                           size=f['size'],
                                           price=f['price'],
                                           id=f['order_id'],)
                    o.register_op_order(op_order)
                    self._orders[o.id] = o
                    added.append(o.id)
                    break

        if added:
            logger.debug("Mapped {} open orders to fills: "
                         "{}".format(len(added), added))

    @property
    def ticker_price(self):
        try:
            return float(self.book_feed.get_current_ticker()['price'])
        except TypeError:
            return None

    def run(self):
        self.book_feed.start()
        sleep(10)

        while not self.stop:

            snap = self.get_book_snapshot()
            bids = snap.bids
            wall_size = self.wall_size
            bid = float(snap.lowest_ask[0])
            size_avail = self.position_size
            tick_price = self.ticker_price
            spend_avail = size_avail * tick_price
            size_avail = spend_avail / bid
            new_orders = list()

            logger.debug("Spend available: {}\n"
                         "Size Available: {}\n".format(
                          spend_avail, size_avail))

            if size_avail > 0.01 and bids and tick_price:
                # We can place a spread order.
                bid_idx = None
                for idx, data in enumerate(bids):
                    price, size, o_id = data
                    if size >= wall_size and idx >= 3:
                        bid_idx = idx-1
                        break

                if bid_idx:
                    b_price, b_size, b_id = bids[bid_idx]
                    while not b_price:
                        bid_idx += 1
                        try:
                            b_price, b_size, b_id = bids[bid_idx]
                        except IndexError:
                            continue

                    logger.debug("Found price: {} & size "
                                 "{}".format(b_price, b_size))

                    o = self.place_order(b_price,
                                         size_avail,
                                         side='buy',
                                         aggressive=False,
                                         adjust_vs_open=True,
                                         check_ticker=True)
                    new_orders.append(o.id)

                else:
                    logger.debug("No bid index found so no buy.")
            else:
                logger.debug("{} open buy orders & {} open sell orders, ticker ${}".format(
                    len(self.buy_orders), len(self.sell_orders), tick_price))

            self.shift_orders(snap, exclude=new_orders)
            sleep(self.interval)

        self.book_feed.close()

    def __del__(self):
        for o_id, order in self.buy_orders.items():
            try:
                order.cancel()
            except Exception as e:
                logger.error("Error during shutdown & "
                             "canceling order {}: {}".format(o_id, e))


if __name__ == '__main__':
    PRODUCT_ID = 'ETH-USD'
    MAX_SPREAD = .50
    MIN_SPREAD = 0.20
    STOP_PCT = 0.05
    INTERVAL = 10
    SPEND_PERCENT = 0.02
    MAX_OPEN_BUYS = 3
    MAX_OPEN_SELLS = 27
    MANAGE_OUTSIDE_ORDERS = False

    m = GdaxMarketMaker(product_id=PRODUCT_ID,
                        gdax=Gdax(),
                        max_spread=MAX_SPREAD,
                        min_spread=MIN_SPREAD,
                        stop_pct=STOP_PCT,
                        interval=INTERVAL,
                        spend_pct=SPEND_PERCENT,
                        max_open_buys=MAX_OPEN_BUYS,
                        max_open_sells=MAX_OPEN_SELLS,
                        manage_existing_orders=MANAGE_OUTSIDE_ORDERS)

    m.map_open_orders_to_fills()
    m.run()


