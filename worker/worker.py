import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
import numpy as np
import time
import sys
sys.path.insert(0, "../")
from collections import namedtuple
import networks
from networks import *
from environment import *
import redis
import msgpack
import math
from zeus.zeus import Zeus

# np.random.seed(0)
# torch.manual_seed(0)
torch.set_default_tensor_type(torch.FloatTensor)
torch.set_num_threads(1)

class Worker(object):

    def __init__(self, name, instrument, granularity, models_loc, start, test=False):

        while True:
            self.market_encoder = LSTMCNNEncoder().cpu()
            self.encoder_to_others = EncoderToOthers().cpu()
            self.actor_critic = ActorCritic().cpu()
            try:
                self.market_encoder.load_state_dict(torch.load(models_loc + 'market_encoder.pt'))
                self.encoder_to_others.load_state_dict(torch.load(models_loc + 'encoder_to_others.pt'))
                self.actor_critic.load_state_dict(torch.load(models_loc + 'actor_critic.pt'))
                self.market_encoder = self.market_encoder.cpu()
                self.encoder_to_others = self.encoder_to_others.cpu()
                self.actor_critic = self.actor_critic.cpu()
                break
            except Exception:
                print("Failed to load models")
                time.sleep(0.1)

        self.name = name
        self.models_loc = models_loc

        self.server = redis.Redis("localhost")
        self.instrument = instrument
        self.granularity = granularity

        self.time_states = []
        self.percents_in = []
        self.spreads = []
        self.mus = []
        self.actions = []
        self.rewards = []

        self.total_actual_reward = 0
        self.reward_tau = float(self.server.get("reward_tau").decode("utf-8"))

        self.window = networks.WINDOW

        self.start = start
        self.trajectory_steps = int(self.server.get("trajectory_steps").decode("utf-8"))

        self.p_new_proposal = float(self.server.get("p_new_proposal").decode("utf-8"))


        self.test = test
        if self.test:
            self.zeus = Zeus(instrument, granularity, margin=0.1)
            self.tradeable_percentage = 1
            self.n_steps_left = self.window + 1440
            self.n_total_experiences = 0
            self.trade_percent = self.tradeable_percentage / 1000

            self.actor_temp = 1
            self.proposer_temps = {"w":1, "mu":1, "sigma":1}
            self.proposer_gate_temp = 1
        else:
            self.zeus = Zeus(instrument, granularity, margin=0.1)
            self.tradeable_percentage = 0.01
            self.n_total_experiences = 100
            self.n_steps_left = self.window + self.trajectory_steps * self.n_total_experiences
            self.trade_percent = self.tradeable_percentage / 1000

            actor_base_temp = float(self.server.get("actor_temp").decode("utf-8"))
            self.actor_temp = np.random.exponential(actor_base_temp / np.log(2))

        self.i_step = 0
        self.steps_since_push = 0
        self.steps_between_experiences = self.trajectory_steps

        self.prev_value = self.zeus.unrealized_balance()

    def add_bar(self, bar):
        time_state = [[[bar.open, bar.high, bar.low, bar.close, np.log(bar.volume + 1e-1)]]]

        if len(self.time_states) == 0 or time_state != self.time_states[-1]:
            self.time_states.append(time_state)
        else:
            return

        if len(self.time_states) >= self.window:

            in_ = self.zeus.position_size()
            available_ = self.zeus.units_available()
            percent_in = (in_ / (abs(in_) + available_ + 1e-9)) / self.tradeable_percentage

            input_time_states = torch.Tensor(self.time_states[-self.window:]).view(self.window, 1, networks.D_BAR).cpu()
            mean = input_time_states[:, 0, :4].mean()
            std = input_time_states[:, 0, :4].std()
            input_time_states[:, 0, :4] = (input_time_states[:, 0, :4] - mean) / std
            assert torch.isnan(input_time_states).sum() == 0
            spread_normalized = bar.spread / std

            market_encoding = self.market_encoder.forward(input_time_states)
            market_encoding = self.encoder_to_others.forward(market_encoding, torch.Tensor([spread_normalized]), torch.Tensor([percent_in]))

            policy, value = self.actor_critic.forward(market_encoding, temp=self.actor_temp)

            if self.test:
                # action = torch.argmax(policy).item()
                action = torch.multinomial(policy, 1).item()
                # action = torch.multinomial(policy[0, :8], 1).item()
            else:
                action = torch.multinomial(policy, 1).item()
                # try:
                #     action = torch.multinomial(policy[0, :8], 1).item()
                # except Exception:
                #     # action = torch.multinomial(policy, 1).item()
                #     action = np.random.randint(0, 8)

            mu = policy[0, action].item()

            def place_action(desired_percent):
                current_percent_in = percent_in * self.tradeable_percentage

                if desired_percent == 0 and current_percent_in != 0:
                    self.zeus.close_units(self.zeus.position_size())
                elif desired_percent > 0 and current_percent_in > 0:
                    if desired_percent > current_percent_in:
                        total_tradeable = abs(self.zeus.position_size()) + self.zeus.units_available()
                        self.zeus.place_trade(int(abs(desired_percent - current_percent_in) * total_tradeable), "Long")
                    else:
                        total_tradeable = abs(self.zeus.position_size()) + self.zeus.units_available()
                        self.zeus.close_units(int(abs((desired_percent - current_percent_in)) * total_tradeable))

                elif desired_percent > 0 and current_percent_in <= 0:
                    self.zeus.close_units(self.zeus.position_size())
                    total_tradeable = abs(self.zeus.position_size()) + self.zeus.units_available()
                    self.zeus.place_trade(int(abs(desired_percent) * total_tradeable), "Long")

                elif desired_percent < 0 and current_percent_in > 0:
                    self.zeus.close_units(self.zeus.position_size())
                    total_tradeable = abs(self.zeus.position_size()) + self.zeus.units_available()
                    self.zeus.place_trade(int(abs(desired_percent) * total_tradeable), "Short")

                elif desired_percent < 0 and current_percent_in <= 0:
                    if desired_percent <= current_percent_in:
                        total_tradeable = abs(self.zeus.position_size()) + self.zeus.units_available()
                        self.zeus.place_trade(int(abs(desired_percent - current_percent_in) * total_tradeable), "Short")
                    else:
                        total_tradeable = abs(self.zeus.position_size()) + self.zeus.units_available()
                        self.zeus.close_units(int(abs((desired_percent - current_percent_in)) * total_tradeable))

            action_amounts = {0:1, 1:3, 2:5, 3:10, 4:-1, 5:-3, 6:-5, 7:-10}
            if action in action_amounts:
                desired_percent_in = np.clip((percent_in * self.tradeable_percentage) + (self.trade_percent * action_amounts[action]), -self.tradeable_percentage, self.tradeable_percentage)
                place_action(desired_percent_in)
            elif action == 8:
                place_action(0)

            new_val = self.zeus.unrealized_balance()
            self.total_actual_reward += new_val - self.prev_value
            reward = (new_val - self.prev_value) / (2000 * self.trade_percent)
            # reward *= 10
            self.prev_value = new_val
            # if self.n_steps_left % 10 == action:
            #     reward = 1
            #     self.total_actual_reward += 1
            # else:
            #     reward = -1
            #     self.total_actual_reward += -1


            if self.test:
                reward_ema = self.server.get("test_reward_ema")
                reward_emsd = self.server.get("test_reward_emsd")
                if reward_ema != None:
                    reward_ema = float(reward_ema.decode("utf-8"))
                    reward_emsd = float(reward_emsd.decode("utf-8"))
                else:
                    reward_ema = 0
                    reward_emsd = 0

                in_ = self.zeus.position_size()
                available_ = self.zeus.units_available()
                percent_in_ = (in_ / (abs(in_) + available_ + 1e-9)) / self.tradeable_percentage

                print("step: {s} \
                \npercent in: {p_in} \
                \naction: {a} \
                \nunrealized_balance: {u_b} \
                \npolicy: {p} \
                \nvalue: {v} \
                \nrewards: {r} \
                \nreward_ema: {ema} \
                \nreward_emsd: {emsd} \
                \nbar close: {close} \
                \ninstrument: {ins} \
                \nstart: {start}\n".format(s=self.i_step,
                                        p_in=round(percent_in_, 8),
                                        a=action,
                                        u_b=round(new_val, 5),
                                        p=[round(policy_, 5) for policy_ in policy[0].tolist()],
                                        v=round(value.item(), 5),
                                        r=round(self.total_actual_reward, 5),
                                        ema=round(reward_ema, 5),
                                        emsd=round(reward_emsd, 5),
                                        close=bar.close,
                                        ins=self.instrument,
                                        start=self.start))

                for inst in ["EUR_USD", "GBP_USD", "AUD_USD", "NZD_USD"]:
                    inst_ema = self.server.get("test_ema_" + inst)
                    if inst_ema is not None:
                        print("ema " + inst, inst_ema.decode("utf-8"))
                        print("emsd " + inst, self.server.get("test_emsd_" + inst).decode("utf-8"))
                        print()

            if not self.test:
                reward_ema = self.server.get("reward_ema").decode("utf-8")
                if reward_ema == 'None':
                    self.server.set("reward_ema", reward)
                    self.server.set("reward_emsd", 0)
                else:
                    reward_ema = float(reward_ema)
                    reward_emsd = float(self.server.get("reward_emsd").decode("utf-8"))
                    delta = reward - reward_ema
                    self.server.set("reward_ema", reward_ema + self.reward_tau * delta)
                    self.server.set("reward_emsd", math.sqrt((1 - self.reward_tau) * (reward_emsd**2 + self.reward_tau * (delta**2))))

            self.percents_in.append(percent_in)
            self.spreads.append(bar.spread)
            self.rewards.append(reward)
            self.actions.append(action)
            self.mus.append(mu)

            if len(self.time_states) == self.window + self.trajectory_steps + 1:
                del self.time_states[0]
            if len(self.percents_in) == self.trajectory_steps + 1:
                del self.percents_in[0]
                del self.spreads[0]
                del self.rewards[0]
                del self.actions[0]
                del self.mus[0]

            if (self.steps_since_push == self.steps_between_experiences) and (not self.test) and len(self.time_states) == self.window + self.trajectory_steps:
                experience = Experience(
                time_states=self.time_states,
                percents_in=self.percents_in,
                spreads=self.spreads,
                mus=self.mus,
                place_actions=self.actions,
                rewards=self.rewards
                )

                experience = msgpack.packb(experience, use_bin_type=True)
                n_experiences = self.server.llen("experience")
                if n_experiences > 0:
                    try:
                        loc = np.random.randint(0, n_experiences)
                        ref = self.server.lindex("experience", loc)
                        self.server.linsert("experience", "before", ref, experience)
                    except redis.exceptions.DataError:
                        self.server.lpush("experience", experience)
                else:
                    self.server.lpush("experience", experience)
                self.steps_since_push = 1

                p = np.random.rand()
                desired_percent_in = np.random.normal(0, 0.25) * self.tradeable_percentage * p + np.random.normal(percent_in, 0.1) * (1 - p)
                desired_percent_in = np.clip(desired_percent_in, -self.tradeable_percentage, self.tradeable_percentage)
                place_action(desired_percent_in)
                self.prev_value = self.zeus.unrealized_balance()
            else:
                self.steps_since_push += 1

            self.i_step += 1

        self.n_steps_left -= 1
        torch.cuda.empty_cache()

    def run(self):
        self.market_encoder.eval()
        self.encoder_to_others.eval()
        self.actor_critic.eval()

        t0 = time.time()
        while self.n_steps_left > 0:
            n_seconds = min(self.n_steps_left, 500) * 60
            if self.granularity == "M5":
                n_seconds *= 5
            self.zeus.stream_range(self.start, self.start + n_seconds, self.add_bar)
            self.start += n_seconds

        print(("time: {time}, "
                "rewards: {reward} %, "
                "temp: {actor_temp}, "
                "n exp: {n_experiences}, "
                "instr: {instrument}, "
                "start: {start}").format(
                    time=round(time.time()-t0, 2),
                    reward=round(100 * self.total_actual_reward / (2000 * self.tradeable_percentage), 5),
                    actor_temp=round(self.actor_temp, 3),
                    n_experiences=self.n_total_experiences,
                    instrument=self.instrument,
                    start=self.start
                )
        )

        if self.test:
            reward_tau = float(self.server.get("test_reward_tau").decode("utf-8"))
            reward_ema = self.server.get("test_reward_ema")
            if reward_ema == None:
                self.server.set("test_reward_ema", self.total_actual_reward)
                self.server.set("test_reward_emsd", 0)
            else:
                reward_ema = float(reward_ema.decode("utf-8"))
                reward_emsd = float(self.server.get("test_reward_emsd").decode("utf-8"))
                delta = self.total_actual_reward - reward_ema
                self.server.set("test_reward_ema", reward_ema + reward_tau * delta)
                self.server.set("test_reward_emsd", math.sqrt((1 - reward_tau) * (reward_emsd**2 + reward_tau * (delta**2))))

            instrument_tau = reward_tau * 4
            instrument_ema = self.server.get("test_ema_" + self.instrument)
            if instrument_ema == None:
                self.server.set("test_ema_" + self.instrument, self.total_actual_reward)
                self.server.set("test_emsd_" + self.instrument, 0)
            else:
                instrument_ema = float(instrument_ema.decode("utf-8"))
                instrument_emsd = float(self.server.get("test_emsd_" + self.instrument).decode("utf-8"))
                delta = self.total_actual_reward - instrument_ema
                self.server.set("test_ema_" + self.instrument, instrument_ema + instrument_tau * delta)
                self.server.set("test_emsd_" + self.instrument, math.sqrt((1 - instrument_tau) * (instrument_emsd**2 + instrument_tau * (delta**2))))



        return self.total_actual_reward

Experience = namedtuple('Experience', ('time_states',
                                       'percents_in',
                                       'spreads',
                                       'mus',
                                       'place_actions',
                                       'rewards'))
