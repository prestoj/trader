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
from worker import Experience
import networks
from networks import *
from environment import *
import redis
import msgpack
import os
import io
import pickle

torch.manual_seed(0)
torch.set_default_tensor_type(torch.cuda.FloatTensor)


class Optimizer(object):

    def __init__(self, models_loc, server_host):
        self.models_loc = models_loc
        self.server = redis.Redis(server_host)


        self.weight_penalty = float(self.server.get("weight_penalty").decode("utf-8"))
        self.learning_rate = float(self.server.get("learning_rate").decode("utf-8"))

        self.MEN = Encoder().cuda()
        self.ACN = ActorCritic().cuda()

        self.optimizer = optim.Adam([param for param in self.MEN.parameters()] +
                                    [param for param in self.ACN.parameters()],
                                    lr=self.learning_rate,
                                    weight_decay=self.weight_penalty)
        try:
            # models
            self.MEN.load_state_dict(torch.load(self.models_loc + 'market_encoder.pt'))
            self.ACN.load_state_dict(torch.load(self.models_loc + 'actor_critic.pt'))

            MEN_state_dict_buffer = io.BytesIO()
            ACN_state_dict_buffer = io.BytesIO()

            torch.save(self.MEN.state_dict(), MEN_state_dict_buffer)
            torch.save(self.ACN.state_dict(), ACN_state_dict_buffer)

            MEN_state_dict_compressed = pickle.dumps(MEN_state_dict_buffer)
            ACN_state_dict_compressed = pickle.dumps(ACN_state_dict_buffer)

            self.server.set("market_encoder", MEN_state_dict_compressed)
            self.server.set("actor_critic", ACN_state_dict_compressed)

            # optimizer
            checkpoint = torch.load(self.models_loc + "rl_train.pt")

            self.optimizer.load_state_dict(checkpoint['optimizer'])
            self.start_step = checkpoint['steps']
            self.start_n_samples = checkpoint['n_samples']
            self.original_actor_temp = checkpoint['original_actor_temp']

            meta_state_buffer = io.BytesIO()
            torch.save(checkpoint, meta_state_buffer)
            meta_state_compressed = pickle.dumps(meta_state_buffer)
            self.server.set("meta_state", meta_state_compressed)
            torch.save(checkpoint, self.models_loc + 'rl_train.pt')

        except (FileNotFoundError, AssertionError) as e:
            # models
            self.MEN = Encoder().cuda()
            self.ACN = ActorCritic().cuda()

            MEN_state_dict_buffer = io.BytesIO()
            ACN_state_dict_buffer = io.BytesIO()

            torch.save(self.MEN.state_dict(), MEN_state_dict_buffer)
            torch.save(self.ACN.state_dict(), ACN_state_dict_buffer)

            MEN_state_dict_compressed = pickle.dumps(MEN_state_dict_buffer)
            ACN_state_dict_compressed = pickle.dumps(ACN_state_dict_buffer)

            self.server.set("market_encoder", MEN_state_dict_compressed)
            self.server.set("actor_critic", ACN_state_dict_compressed)

            torch.save(self.MEN.state_dict(), self.models_loc + 'market_encoder.pt')
            torch.save(self.ACN.state_dict(), self.models_loc + 'actor_critic.pt')

            # optimizer
            self.optimizer = optim.Adam([param for param in self.MEN.parameters()] +
                                        [param for param in self.ACN.parameters()],
                                        lr=self.learning_rate,
                                        weight_decay=self.weight_penalty)

            self.start_step = 0
            self.start_n_samples = 0
            self.original_actor_temp = 5
            cur_meta_state = {
                'n_samples':self.start_n_samples,
                'steps':self.start_step,
                'original_actor_temp':self.original_actor_temp,
                'optimizer':self.optimizer.state_dict()
            }

            meta_state_buffer = io.BytesIO()
            torch.save(cur_meta_state, meta_state_buffer)
            torch.save(cur_meta_state, self.models_loc + 'rl_train.pt')

            cur_meta_state_compressed = pickle.dumps(cur_meta_state)
            self.server.set("optimizer", cur_meta_state_compressed)

        self.actor_temp_cooldown = float(self.server.get("actor_temp_cooldown").decode("utf-8"))
        self.gamma = float(self.server.get("gamma").decode("utf-8"))
        self.trajectory_steps = int(self.server.get("trajectory_steps").decode("utf-8"))
        self.max_rho = torch.Tensor([float(self.server.get("max_rho").decode("utf-8"))]).cuda()
        self.max_c = torch.Tensor([float(self.server.get("max_c").decode("utf-8"))]).cuda()

        self.critic_weight = float(self.server.get("critic_weight").decode("utf-8"))
        self.actor_v_weight = float(self.server.get("actor_v_weight").decode("utf-8"))
        self.actor_entropy_weight = float(self.server.get("actor_entropy_weight").decode("utf-8"))

        self.queued_batch_size = int(self.server.get("queued_batch_size").decode("utf-8"))

        self.queued_experience = []
        self.prioritized_experience = []

        self.step = self.start_step
        self.actor_temp = 1 + (self.original_actor_temp - 1) * self.actor_temp_cooldown ** self.step
        self.server.set("actor_temp", self.actor_temp)

    def run(self):
        self.MEN.train()
        self.ACN.train()

        n_samples = self.start_n_samples
        while True:
            t0 = time.time()
            n_experiences = 0
            # read in experience from the queue
            while True:
                if (len(self.queued_experience) < self.queued_batch_size and self.server.llen("experience") > 0):
                    experience = self.server.lpop("experience")
                    experience = msgpack.unpackb(experience, raw=False)
                    self.queued_experience.append(experience)
                    n_experiences += 1
                elif len(self.queued_experience) == self.queued_batch_size:
                    break
                elif self.step != 0:
                    time.sleep(0.01)
                else:
                    experience = self.server.blpop("experience")[1]
                    experience = msgpack.unpackb(experience, raw=False)
                    self.queued_experience.append(experience)
                    n_experiences += 1

            # # get some experiences from the replay buffer
            # buffer_size = min(self.server.llen("replay_buffer"), int(self.server.get("replay_buffer_size").decode("utf-8")))
            # n_replay = 0
            # if buffer_size > len(self.queued_experience):
            #     while n_replay < len(self.queued_experience):
            #         try:
            #             buffer_size = min(self.server.llen("replay_buffer"), int(self.server.get("replay_buffer_size").decode("utf-8")))
            #             loc = np.random.randint(0, buffer_size)
            #             experience = self.server.lindex("replay_buffer", int(loc))
            #             experience = msgpack.unpackb(experience, raw=False)
            #             self.prioritized_experience.append(experience)
            #             n_replay += 1
            #         except Exception:
            #             pass

            experiences = self.queued_experience + self.prioritized_experience
            batch_size = len(experiences)

            # if self.step in [10000]:
            #     for param_group in self.optimizer.param_groups:
            #         param_group['lr'] = param_group['lr'] / 10

            # start grads anew
            self.optimizer.zero_grad()

            # get the inputs to the networks in the right form
            batch = Experience(*zip(*experiences))
            time_states = [*zip(*batch.time_states)]
            percent_in = [*zip(*batch.percents_in)]
            trade_open = [*zip(*batch.trades_open)]
            spread = [*zip(*batch.spreads)]
            mu = [*zip(*batch.mus)]
            place_action = [*zip(*batch.place_actions)]
            reward = [*zip(*batch.rewards)]

            assert len(time_states) == self.trajectory_steps
            assert len(reward) == self.trajectory_steps - 1

            critic_loss = torch.Tensor([0]).cuda()
            actor_v_loss = torch.Tensor([0]).cuda()
            actor_entropy_loss = torch.Tensor([0]).cuda()

            time_states_ = torch.Tensor(time_states[self.trajectory_steps-1]).view(batch_size, networks.WINDOW, networks.D_BAR).cuda()
            spread_ = torch.Tensor(spread[self.trajectory_steps-1]).cuda()
            percent_in_ = torch.Tensor(percent_in[self.trajectory_steps-1]).cuda()
            trade_open_ = torch.Tensor(trade_open[self.trajectory_steps-1]).cuda()

            # print('<final>')
            # print(time_states_)
            # print('</final>')

            market_encoding = self.MEN.forward(time_states_, spread_, percent_in_, trade_open_)
            policy, value = self.ACN.forward(market_encoding)

            v_next = value.detach()
            v_trace = value.detach()
            for i in range(self.trajectory_steps - 2, -1, -1):

                time_states_ = torch.Tensor(time_states[i]).view(batch_size, networks.WINDOW, networks.D_BAR).cuda()
                spread_ = torch.Tensor(spread[i]).cuda()
                percent_in_ = torch.Tensor(percent_in[i]).cuda()
                trade_open_ = torch.Tensor(trade_open[i]).cuda()

                market_encoding = self.MEN.forward(time_states_, spread_, percent_in_, trade_open_)
                policy, value = self.ACN.forward(market_encoding)

                pi_ = policy.gather(1, torch.Tensor(place_action[i]).cuda().long().view(batch_size, 1))
                mu_ = torch.Tensor(mu[i]).cuda().view(batch_size, 1)

                r = torch.Tensor(reward[i]).cuda().view(batch_size, 1)

                # print('<{i}>'.format(i=i))
                # print(place_action[i], reward[i], time_states_)
                # print('</{i}>'.format(i=i))

                if i == 0:
                    rho = torch.min(self.max_rho, pi_ / (mu_ + 1e-9))

                    advantage_v = r + self.gamma * v_trace - value
                    # actor_v_loss += (-torch.log(pi_ + 1e-9) * (rho * advantage_v).detach()).mean()
                    actor_v_loss += (-pi_ * (rho * advantage_v).detach()).mean()

                    actor_entropy_loss += (torch.log(policy + 1e-9) * policy).mean()

                rho = torch.min(self.max_rho, pi_ / (mu_ + 1e-9))
                c = torch.min(self.max_c, pi_ / (mu_ + 1e-9))
                delta_v = rho * (r + self.gamma * v_next - value)

                v_trace = (value + delta_v + self.gamma * c * (v_trace - v_next)).detach()

                if i == 0:
                    critic_loss += nn.MSELoss()(value, v_trace.detach())

                v_next = value.detach()

            total_loss = torch.Tensor([0]).cuda()
            total_loss += critic_loss * self.critic_weight
            total_loss += actor_v_loss * self.actor_v_weight
            total_loss += actor_entropy_loss * self.actor_entropy_weight

            try:
                assert torch.isnan(total_loss).sum() == 0
            except AssertionError:
                print("critic_loss", critic_loss)
                print("actor_v_loss", actor_v_loss)
                print("actor_entropy_loss", actor_entropy_loss)
                raise AssertionError("total loss is not 0")


            total_loss.backward()
            # for name, param in self.ACN.named_parameters():
            #     print(name, param.grad.std())
            self.optimizer.step()

            self.step += 1
            n_samples += len(self.queued_experience)

            try:
                if self.step % 100 == 0:
                    cur_meta_state = {
                    'n_samples':n_samples,
                    'steps':self.step,
                    'original_actor_temp':self.original_actor_temp,
                    'optimizer':self.optimizer.state_dict()
                    }

                    MEN_state_dict_buffer = io.BytesIO()
                    ACN_state_dict_buffer = io.BytesIO()
                    meta_state_buffer = io.BytesIO()

                    torch.save(self.MEN.state_dict(), MEN_state_dict_buffer)
                    torch.save(self.ACN.state_dict(), ACN_state_dict_buffer)
                    torch.save(cur_meta_state, meta_state_buffer)

                    MEN_state_dict_compressed = pickle.dumps(MEN_state_dict_buffer)
                    ACN_state_dict_compressed = pickle.dumps(ACN_state_dict_buffer)
                    meta_state_compressed = pickle.dumps(meta_state_buffer)

                    self.server.set("market_encoder", MEN_state_dict_compressed)
                    self.server.set("actor_critic", ACN_state_dict_compressed)
                    self.server.set("meta_state", meta_state_compressed)

                    torch.save(self.MEN.state_dict(), self.models_loc + 'market_encoder.pt')
                    torch.save(self.ACN.state_dict(), self.models_loc + "actor_critic.pt")
                    torch.save(cur_meta_state, self.models_loc + 'rl_train.pt')
            except Exception:
                print("failed to save")

            if self.step % 10000 == 0:
                try:
                    if not os.path.exists(self.models_loc + 'model_history'):
                        os.makedirs(self.models_loc + 'model_history')
                    if not os.path.exists(self.models_loc + 'model_history/{step}'.format(step=self.step)):
                        os.makedirs(self.models_loc + 'model_history/{step}'.format(step=self.step))
                    torch.save(self.MEN.state_dict(), self.models_loc + 'model_history/{step}/market_encoder.pt'.format(step=self.step))
                    torch.save(self.ACN.state_dict(), self.models_loc + "model_history/{step}/actor_critic.pt".format(step=self.step))
                    cur_meta_state = {
                        'n_samples':n_samples,
                        'steps':self.step,
                        'original_actor_temp':self.original_actor_temp,
                        'optimizer':self.optimizer.state_dict()
                    }
                    torch.save(cur_meta_state, self.models_loc + 'model_history/{step}/rl_train.pt'.format(step=self.step))

                    MEN_state_dict_buffer = io.BytesIO()
                    ACN_state_dict_buffer = io.BytesIO()
                    meta_state_buffer = io.BytesIO()

                    torch.save(self.MEN.state_dict(), MEN_state_dict_buffer)
                    torch.save(self.ACN.state_dict(), ACN_state_dict_buffer)
                    torch.save(cur_meta_state, meta_state_buffer)

                    MEN_state_dict_compressed = pickle.dumps(MEN_state_dict_buffer)
                    ACN_state_dict_compressed = pickle.dumps(ACN_state_dict_buffer)
                    meta_state_compressed = pickle.dumps(meta_state_buffer)

                    self.server.set("market_encoder", MEN_state_dict_compressed)
                    self.server.set("actor_critic", ACN_state_dict_compressed)
                    self.server.set("meta_state", meta_state_compressed)

                except Exception:
                    print("failed to save")

            self.actor_temp = 1 + (self.original_actor_temp - 1) * self.actor_temp_cooldown ** self.step
            self.server.set("actor_temp", self.actor_temp)
            self.queued_experience = []
            self.prioritized_experience = []
            # torch.cuda.empty_cache()

            print('-----------------------------------------------------------')
            print("n samples: {n}, batch size: {b}, steps: {s}, time: {t}".format(n=n_samples, b=batch_size, s=self.step, t=round(time.time()-t0, 5)))
            print("actor temp: {at}".format(at=self.actor_temp))
            print()

            print("policy means:", policy.cpu().detach().mean(dim=0))
            print()

            print("value min mean std max:\n", round(value.cpu().detach().min().item(), 7), round(value.cpu().detach().mean().item(), 7), round(value.cpu().detach().std().item(), 7), round(value.cpu().detach().max().item(), 7))
            print("v_trace min mean std max:\n", round(v_trace.cpu().detach().min().item(), 7), round(v_trace.cpu().detach().mean().item(), 7), round(v_trace.cpu().detach().std().item(), 7), round(v_trace.cpu().detach().max().item(), 7))
            print()

            print("weighted critic loss:", round(float(critic_loss * self.critic_weight), 7))
            print("weighted actor v loss:", round(float(actor_v_loss * self.actor_v_weight), 7))
            print("weighted actor entropy loss:", round(float(actor_entropy_loss * self.actor_entropy_weight), 7))
            print('-----------------------------------------------------------')
