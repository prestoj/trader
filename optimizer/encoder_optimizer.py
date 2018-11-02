import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
import numpy as np
import math
import time
import heapq
import sys
sys.path.insert(0, '../worker')
from simple_worker import Experience
import networks
from networks import *
from environment import *
import redis
import pickle

class Optimizer(object):

    def __init__(self, models_loc):
        torch.set_default_tensor_type(torch.cuda.FloatTensor)

        self.server = redis.Redis("localhost")
        self.weight_penalty = float(self.server.get("weight_penalty").decode("utf-8"))
        self.learning_rate = float(self.server.get("learning_rate").decode("utf-8"))

        self.models_loc = models_loc

        self.encoder = AttentionMarketEncoder().cuda()
        self.decoder = Decoder().cuda()
        self.optimizer = optim.Adam(list(self.encoder.parameters()) + list(self.decoder.parameters()), lr=self.learning_rate, weight_decay=self.weight_penalty)
        try:
            self.encoder.load_state_dict(torch.load(self.models_loc + 'market_encoder.pt'))
            self.decoder.load_state_dict(torch.load(self.models_loc + 'decoder.pt'))
            checkpoint = torch.load(self.models_loc + 'encoder_train.pt')
            self.optimizer.load_state_dict(checkpoint['optimizer'])
            self.start_step = checkpoint['steps']
            self.start_n_samples = checkpoint['n_samples']
        except FileNotFoundError:
            torch.save(self.encoder.state_dict(), self.models_loc + 'market_encoder.pt')
            torch.save(self.decoder.state_dict(), self.models_loc + 'decoder.pt')
            self.start_n_samples = 0
            self.start_step = 0
            cur_state = {
            'n_samples':self.start_n_samples,
            'steps':self.start_step,
            'optimizer':self.optimizer.state_dict()
            }
            torch.save(cur_state, self.models_loc + 'encoder_train.pt')

        self.advantage_weight = float(self.server.get("advantage_weight").decode("utf-8"))
        self.time_weight = float(self.server.get("time_weight").decode("utf-8"))
        self.batch_size = int(self.server.get("queued_batch_size").decode("utf-8"))
        self.trajectory_steps = int(self.server.get("trajectory_steps").decode("utf-8"))
        self.samples_per_trajectory = int(self.server.get("samples_per_trajectory").decode("utf-8"))

        self.window = networks.WINDOW

        self.optimizer = optim.Adam(list(self.encoder.parameters()) + list(self.decoder.parameters()), lr=self.learning_rate, weight_decay=self.weight_penalty)

    def run(self):
        n_samples = self.start_n_samples
        step = self.start_step
        t0 = time.time()
        t = 0
        t_tau = 0.01
        while True:
            n_experiences = 0
            # read in experience from the queue
            experiences = []
            while True:
                if (len(experiences) < self.batch_size and self.server.llen("experience") > 0):
                    experience = self.server.lpop("experience")
                    experience = pickle.loads(experience)
                    experiences.append(experience)
                    n_experiences += 1
                elif (step != 1 or (step == 1 and len(experiences) == self.batch_size)) and len(experiences) > 0:
                    break
                else:
                    experience = self.server.blpop("experience")[1]
                    experience = pickle.loads(experience)
                    experiences.append(experience)
                    n_experiences += 1

            self.optimizer.zero_grad()

            batch = Experience(*zip(*experiences))
            time_states = [*zip(*batch.time_states)]
            for i, time_state_ in enumerate(time_states):
                time_states[i] = torch.cat(time_state_)
            spread = [*zip(*batch.spreads)]

            total_loss = torch.Tensor([0])

            samples = np.random.choice(np.arange(1, self.trajectory_steps), self.samples_per_trajectory)
            samples.sort()

            for i in samples[::-1]:
                sample_start = np.random.randint(self.window, self.window + self.trajectory_steps - i)

                time_states_ = torch.cat(time_states[sample_start-self.window:sample_start], dim=1).cuda()
                mean = time_states_[:, :, :4].contiguous().view(len(experiences), -1).mean(1).view(len(experiences), 1, 1)
                std = time_states_[:, :, :4].contiguous().view(len(experiences), -1).std(1).view(len(experiences), 1, 1)
                time_states_[:, :, :4] = (time_states_[:, :, :4] - mean) / std
                spread_ = torch.Tensor(spread[-i]).view(-1, 1, 1).cuda() / std
                time_states_ = time_states_.transpose(0, 1)

                market_encoding = self.encoder.forward(time_states_, spread_)
                advantage_ = self.decoder.forward(market_encoding, torch.Tensor([i]).repeat(market_encoding.size()[0], 1).log().cuda())

                future_value = time_states[sample_start][:,:,3].cuda()
                potential_gain_buy = future_value.clone()
                potential_gain_buy -= time_states[-i][:,:,3].cuda()
                potential_gain_buy -= torch.Tensor(spread[-i]).view(-1, 1) / 2
                potential_gain_buy = potential_gain_buy / (std.view(-1, 1) * math.sqrt(i))

                potential_gain_sell = time_states[-i][:,:,3].cuda()
                potential_gain_sell -= future_value
                potential_gain_sell -= torch.Tensor(spread[-i]).view(-1, 1) / 2
                potential_gain_sell = potential_gain_sell / (std.view(-1, 1) * math.sqrt(i))

                potential_gain_stay = torch.zeros_like(potential_gain_buy)

                # print(i)
                # print(potential_gain_buy)
                # print(potential_gain_sell)
                # print(potential_gain_stay)
                # print()

                actor_pot_mean = (potential_gain_buy + potential_gain_sell + potential_gain_stay) / 3

                advantage_buy = potential_gain_buy - actor_pot_mean
                advantage_sell = potential_gain_sell - actor_pot_mean
                advantage_stay = potential_gain_stay - actor_pot_mean

                # print(i)
                # print(advantage_[:, 0].view(-1, 1), advantage_buy.detach())
                # print(advantage_[:, 1].view(-1, 1), advantage_sell.detach())
                # print(advantage_[:, 2].view(-1, 1), advantage_stay.detach())
                # print()

                actor_pot_loss_buy = F.l1_loss(advantage_[:, 0].view(-1, 1), advantage_buy.detach())
                actor_pot_loss_sell = F.l1_loss(advantage_[:, 1].view(-1, 1), advantage_sell.detach())
                actor_pot_loss_stay = F.l1_loss(advantage_[:, 2].view(-1, 1), advantage_stay.detach())

                total_loss += (actor_pot_loss_buy.mean() + actor_pot_loss_sell.mean() + actor_pot_loss_stay.mean()) / self.samples_per_trajectory

            assert torch.isnan(total_loss).sum() == 0
            total_loss.backward()
            self.optimizer.step()

            step += 1
            n_samples += n_experiences * self.samples_per_trajectory

            print("n samples: {n}, steps: {s}, time ema: {t}".format(n=n_samples, s=step, t=t))
            print("total_loss: {t}".format(t=total_loss))

            try:
                torch.save(self.encoder.state_dict(), self.models_loc + "market_encoder.pt")
                torch.save(self.decoder.state_dict(), self.models_loc + "decoder.pt")
                cur_state = {
                    'n_samples':n_samples,
                    'steps':step,
                    'optimizer':self.optimizer.state_dict()
                }
                torch.save(cur_state, self.models_loc + 'encoder_train.pt')
            except Exception:
                print("failed to save")

            if t == 0:
                t = time.time() - t0
            t = (time.time() - t0) * t_tau + t * (1 - t_tau)
            t0 = time.time()
