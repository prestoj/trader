import torch
import pandas as pd
import numpy as np
import time


"""
add functionality to get some random price within the high and low of the
current price. this can be used to continually check whether a trade is worth
it, not just at the very beginning of the tick.
"""
class Env(object):

    def __init__(self, source, start, n_steps, spread_func_param=0, time_window=256, get_time=False):
        self.data = pd.DataFrame(pd.read_csv(source)).iloc[start:start+n_steps]

        self.time_window = time_window
        self.cur_i = start
        self.start = start
        self.n_steps = n_steps

        self.time_states = []
        self.orders = []
        self.balance = 1
        self.value = 1
        self.prev_value = 1

        self.spread_func = lambda: np.random.gamma(spread_func_param, 2 / 10000)

        self.get_time = get_time

        for _ in range(self.time_window):
            time_state = TimeState(open=self.data['open'][self.cur_i],
                                         high=self.data['high'][self.cur_i],
                                         low=self.data['low'][self.cur_i],
                                         close=self.data['close'][self.cur_i],
                                         time=self.data['time'][self.cur_i],
                                         spread=self.spread_func())

            self.time_states.append(time_state)

    def get_state(self):
        # print(str(self.balance) + "\t" + str(self.value))

        if self.cur_i == self.start + self.n_steps:
            return False

        torch_time_states = []
        for time_state in self.time_states:
            torch_time_states.append(time_state.as_tensor(with_time=self.get_time))

        if len(self.orders) > 0 and self.orders[-1].quantity > 0:
            coef = 1
        else:
            coef = -1
        return torch_time_states, coef*(self.value - self.balance) / self.value, self.time_states[-1].spread, self.reward()

    def step(self, placed_order):

        if self.cur_i == self.start + self.n_steps - 1:
            return False

        # buy or sell as necessary
        if placed_order[0] == 0:
            # print("BUY")
            # if a buy order, but have already placed sell orders, close all
            # before buying
            if len(self.orders) > 0 and self.orders[0].quantity < 0:
                for i, _ in enumerate(self.orders):
                    self.close_order(i)
            self.buy(placed_order[1] * self.balance)
        elif placed_order[0] == 1:
            # print("SELL")
            # if a sell order, but have already placed buy orders, close all
            # before selling
            if len(self.orders) > 0 and self.orders[0].quantity > 0:
                for i, _ in enumerate(self.orders):
                    self.close_order(i)
            self.sell(placed_order[1] * self.balance)

        elif placed_order[0] == 2:
            # close all open orders
            if len(self.orders) > 0:
                for i, _ in enumerate(self.orders):
                    self.close_order(i)

        self.cur_i += 1

        if len(self.time_states) == self.time_window:
            del self.time_states[0]

        self.update_value()

        new_time_state = TimeState(open=self.data['open'][self.cur_i],
                                   high=self.data['high'][self.cur_i],
                                   low=self.data['low'][self.cur_i],
                                   close=self.data['close'][self.cur_i],
                                   time=self.data['time'][self.cur_i],
                                   spread=self.spread_func())

        self.time_states.append(new_time_state)

    def buy(self, amount):
        self.balance -= amount
        self.balance -= amount * self.time_states[-1].spread / 2
        new_order = Order(open_time=self.data['time'][self.cur_i + 1],
                          open_price=self.data['close'][self.cur_i] + (self.time_states[-1].spread / 2),
                          quantity=amount / self.data['close'][self.cur_i])

        self.orders.append(new_order)

    def sell(self, amount):
        self.balance -= amount
        self.balance -= amount * self.time_states[-1].spread / 2
        new_order = Order(open_time=self.data['time'][self.cur_i + 1],
                          open_price=self.data['close'][self.cur_i] - (self.time_states[-1].spread / 2),
                          quantity=-amount / self.data['close'][self.cur_i])
        self.orders.append(new_order)

    def close_order(self, order_i):
        self.balance += self.orders[order_i].value(self.data['close'][self.cur_i])
        del self.orders[order_i]

    def update_value(self):
        self.prev_value = self.value
        self.value = self.balance
        for order in self.orders:
            self.value += order.value(self.time_states[-1].close)

        return self.value

    def reward(self):
        return self.value - self.prev_value

    def orders_rewards(self):
        rewards = []
        for order in self.orders:
            rewards.append(self.time_states[-1].close - order.open_price)

        return rewards

    def reset(self):
        self.prev_value = 1
        self.value = 1
        self.balance = 1
        for _ in range(len(self.orders)):
            self.orders.pop()


class Order(object):

    def __init__(self, open_time, open_price, quantity):

        self.quantity = quantity

        self.open_price = open_price
        self.open_time = open_time

    def as_ndarray(self):
        return np.array([self.open_time, self.open_price, self.quantity])

    def value(self, price):
        if self.quantity >= 0:
            return price * self.quantity
        else:
            return self.quantity * (price - self.open_price) - self.quantity * self.open_price

    def __repr__(self):
        return "order of quantity: {quant} at price: {price}".format(quant=self.quantity, price=self.open_price)

class TimeState(object):

    def __init__(self, open, high, low, close, time, spread):
        self.open = open
        self.high = high
        self.low = low
        self.close = close
        self.time = time
        self.spread = spread
        self.nd_repr = None
        self.tensor_repr = None

    def as_ndarray(self, with_time=False):
        if self.nd_repr is not None:
            return self.nd_repr
        elif with_time:
            self.nd_repr = np.array([self.open,
                                     self.high,
                                     self.low,
                                     self.close,
                                     self.time])
        else:
            self.nd_repr = np.array([self.open,
                                     self.high,
                                     self.low,
                                     self.close])

        return self.nd_repr

    def as_tensor(self, with_time=False):
        if self.tensor_repr is not None:
            return self.tensor_repr
        elif with_time:
            self.tensor_repr = torch.Tensor([self.open,
                                             self.high,
                                             self.low,
                                             self.close,
                                             self.time]).float().view(1, 1, -1)
        else:
            self.tensor_repr = torch.Tensor([self.open,
                                             self.high,
                                             self.low,
                                             self.close]).float().view(1, 1, -1)

        return self.tensor_repr

if __name__ == "__main__":
    start = 0
    n_steps = 10
    env = Env("C:\\Users\\Preston\\Programming\\trader\\normalized_data\\DAT_MT_EURUSD_M1_2010-1.3261691621962404.csv", start, n_steps)
    for _ in range(n_steps):
        print(env.get_state())
        print(env.value)
        action = int(input("action: "))
        if action in [0, 1]:
            quantity = float(input("quantity: "))
            env.step([action, quantity])
        else:
            env.step([action])
