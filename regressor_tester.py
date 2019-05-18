import numpy as np
import time
import sys
sys.path.insert(0, "../")
from collections import namedtuple
import torch
import networks
from networks import *
import msgpack
import redis
import copy
from zeus.zeus import Zeus
import matplotlib.pyplot as plt

torch.set_default_tensor_type(torch.cuda.FloatTensor)

class Worker(object):

    def __init__(self, instrument, granularity, server_host, start, models_loc):
        self.server = redis.Redis(server_host)
        self.zeus = Zeus(instrument, granularity, margin=1)
        self.models_loc = models_loc

        self.window = networks.WINDOW
        self.start = start

        self.time_states = []
        self.last_time = None

        self.generator = Generator().cuda()
        try:
            self.generator.load_state_dict(torch.load(self.models_loc + 'generator.pt', map_location='cuda'))
        except FileNotFoundError as e:
            print("model load failed", e)
            self.generator = Generator().cuda()

        self.generator.eval()

        # for name, param in self.generator.named_parameters():
        #     print(param, name)

        self.n_future_samples = 1
        self.n_steps_future = 30

        self.plot = True

        self.step = 0

        self.last_price = None
        self.last_action = None
        self.steps_since_trade = 0
        self.n_correct = 0
        self.n_trades = 0

    def add_bar(self, bar):
        t_ = time.time()
        time_state = [[[bar.open, bar.high, bar.low, bar.close]]]

        if bar.date != self.last_time:
            self.time_states.append(time_state)

        if len(self.time_states) > self.window + self.n_steps_future:
            del self.time_states[0]

            if bar.date != self.last_time:
                input_time_states = []
                for time_state in self.time_states:
                    input_time_states.append(torch.Tensor(time_state).view(1, 1, networks.D_BAR))
                if self.plot:
                    time_state_values = torch.cat(input_time_states, dim=1)[0, :, 3].cpu().detach().numpy()
                    plt.plot(np.arange(0, self.window), time_state_values[:self.window], 'b-')
                    plt.plot(np.arange(self.window - 1, self.window + self.n_steps_future), time_state_values[self.window-1:], 'g-')

                input_time_states_original = []
                for i_time_state in range(self.window):
                    input_time_states_original.append(torch.Tensor(self.time_states[i_time_state]))
                input_time_states_original = torch.cat(input_time_states_original, dim=1)

                generation = self.generator(input_time_states_original.repeat(self.n_future_samples, 1, 1))

                if self.plot:
                    time_state_values = torch.cat([input_time_states_original[0, -1, 3].view(1, 1, 1).repeat(self.n_future_samples, 1, 1), generation], dim=1).cpu().detach().numpy()

                    x = np.arange(self.window - 1, self.window + self.n_steps_future).reshape(1, -1).repeat(self.n_future_samples, axis=0)
                    y = time_state_values

                    x = x.reshape(self.n_steps_future + 1, -1)
                    y = y.reshape(self.n_steps_future + 1, -1)

                    plt.plot(x, y, 'y-', alpha=1)

                predicted_future = generation[:, -1].mean().item()
                # predicted_future = input_time_states_original.mean().item()
                actual_future = self.time_states[-1][0][0][3]
                original = input_time_states[self.window-1][0, 0, 3].item()

                if (predicted_future > original and actual_future > original) or (predicted_future <= original and actual_future <= original):
                    self.n_correct += 1
                self.n_trades += 1

                print("trade: {trade}, time: {time}, actual change: {actual}, predicted change: {pred}, accuracy: {acc}".format(
                        trade=self.n_trades,
                        time=round(time.time() - t_, 5),
                        actual=round((actual_future - original) / input_time_states_original.std().item(), 3),
                        pred=round((predicted_future - original) / input_time_states_original.std().item(), 3),
                        acc=round(self.n_correct / (self.n_trades + 1e-9), 7)
                        ))
                print()

                if self.plot:
                    plt.show()

                self.time_states = []

        self.last_time = bar.date
        self.step += 1
        self.steps_since_trade += 1

    def run(self):
        t0 = time.time()
        with torch.no_grad():
            start = self.start
            while True:
                n_seconds = 60 * 1440 * 30
                self.zeus.stream_range(start, start + n_seconds, self.add_bar)
                start += n_seconds

        print("time: {time}".format(time=time.time()-t0))


if __name__ == "__main__":
    """
    1/1/06 -> 1136073600
    1/1/12 -> 1325376000
    1/1/17 -> 1483228800
    1/1/18 -> 1514764800
    1/1/19 -> 1546300800
    """
    worker = Worker("EUR_USD", "M1", "192.168.0.115", 1514764800, "./models/")
    worker.run()
