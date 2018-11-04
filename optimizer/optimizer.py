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
import pickle

torch.manual_seed(0)
torch.set_default_tensor_type(torch.cuda.FloatTensor)


class Optimizer(object):

    def __init__(self, models_loc):
        self.models_loc = models_loc
        # networks
        # this is the lstm's version
        # self.MEN = MarketEncoder().cuda()
        # this is the attention version
        self.MEN = AttentionMarketEncoder().cuda()
        self.ETO = EncoderToOthers().cuda()
        self.PN = Proposer().cuda()
        self.ACN = ActorCritic().cuda()
        self.ACN_ = ActorCritic().cuda()
        try:
            self.MEN.load_state_dict(torch.load(self.models_loc + 'market_encoder.pt'))
            self.ETO.load_state_dict(torch.load(self.models_loc + 'encoder_to_others.pt'))
            self.PN.load_state_dict(torch.load(self.models_loc + 'proposer.pt'))
            self.ACN.load_state_dict(torch.load(self.models_loc + 'actor_critic.pt'))
            self.ACN_.load_state_dict(torch.load(self.models_loc + 'actor_critic.pt'))
        except FileNotFoundError:
            self.ETO = EncoderToOthers().cuda()
            self.PN = Proposer().cuda()
            self.ACN = ActorCritic().cuda()
            self.ACN_ = ActorCritic().cuda()

            torch.save(self.ETO.state_dict(), self.models_loc + 'encoder_to_others.pt')
            torch.save(self.PN.state_dict(), self.models_loc + 'proposer.pt')
            torch.save(self.ACN.state_dict(), self.models_loc + 'actor_critic.pt')

        self.server = redis.Redis("localhost")
        self.gamma = float(self.server.get("gamma").decode("utf-8"))
        self.trajectory_steps = int(self.server.get("trajectory_steps").decode("utf-8"))
        self.max_rho = torch.Tensor([float(self.server.get("max_rho").decode("utf-8"))]).cuda()
        self.max_c = torch.Tensor([float(self.server.get("max_c").decode("utf-8"))]).cuda()

        self.proposer_tau = float(self.server.get("proposer_tau").decode("utf-8"))
        self.critic_tau = float(self.server.get("critic_tau").decode("utf-8"))
        self.actor_v_tau = float(self.server.get("actor_v_tau").decode("utf-8"))
        self.entropy_tau = float(self.server.get("entropy_tau").decode("utf-8"))

        self.proposed_weight = float(self.server.get("proposed_weight").decode("utf-8"))
        self.critic_weight = float(self.server.get("critic_weight").decode("utf-8"))
        self.actor_v_weight = float(self.server.get("actor_v_weight").decode("utf-8"))
        self.entropy_weight = float(self.server.get("entropy_weight").decode("utf-8"))
        self.weight_penalty = float(self.server.get("weight_penalty").decode("utf-8"))

        self.learning_rate = float(self.server.get("learning_rate").decode("utf-8"))

        self.queued_batch_size = int(self.server.get("queued_batch_size").decode("utf-8"))
        self.prioritized_batch_size = int(self.server.get("prioritized_batch_size").decode("utf-8"))

        self.queued_experience = []
        self.prioritized_experience = []
        try:
            self.optimizer = optim.Adam([params for params in self.ETO.parameters()] +
                                        [params for params in self.PN.parameters()] +
                                        [params for params in self.ACN.parameters()],
                                        lr=self.learning_rate,
                                        weight_decay=self.weight_penalty)
            checkpoint = torch.load(models_loc + "rl_train.pt")
            self.optimizer.load_state_dict(checkpoint['optimizer'])
            self.start_step = checkpoint['steps']
            self.start_n_samples = checkpoint['n_samples']

        except:
            self.optimizer = optim.Adam([params for params in self.ETO.parameters()] +
                                        [params for params in self.PN.parameters()] +
                                        [params for params in self.ACN.parameters()],
                                        lr=self.learning_rate,
                                        weight_decay=self.weight_penalty)
            self.start_n_samples = 0
            self.start_step = 0
            cur_state = {
                'n_samples':self.start_n_samples,
                'steps':self.start_step,
                'optimizer':self.optimizer.state_dict()
            }
            torch.save(self.optimizer.state_dict(), self.models_loc + 'rl_train.pt')

    def run(self):
        prev_reward_ema = None
        prev_reward_emsd = None
        n_samples = self.start_n_samples
        step = self.start_step
        while True:
            n_experiences = 0
            # read in experience from the queue
            while True:
                if (len(self.queued_experience) < self.queued_batch_size and self.server.llen("experience") > 0):
                    experience = self.server.lpop("experience")
                    experience = pickle.loads(experience)
                    self.queued_experience.append(experience)
                    n_experiences += 1
                elif (step != 1 or (step == 1 and len(self.queued_experience) == self.queued_batch_size)) and len(self.queued_experience) + len(self.prioritized_experience) > 0:
                    break
                else:
                    experience = self.server.blpop("experience")[1]
                    experience = pickle.loads(experience)
                    self.queued_experience.append(experience)
                    n_experiences += 1

            experiences = self.queued_experience + self.prioritized_experience

            # start grads anew
            self.optimizer.zero_grad()
            self.ACN_.zero_grad()

            # get the inputs to the networks in the right form
            batch = Experience(*zip(*experiences))
            time_states = [*zip(*batch.time_states)]
            for i, time_state_ in enumerate(time_states):
                time_states[i] = torch.cat(time_state_)
            percent_in = [*zip(*batch.percents_in)]
            spread = [*zip(*batch.spreads)]
            mu = [*zip(*batch.mus)]
            proposed_actions = [*zip(*batch.proposed_actions)]
            place_action = [*zip(*batch.place_actions)]
            reward = [*zip(*batch.rewards)]

            window = len(time_states) - len(percent_in)
            assert window == networks.WINDOW
            trajectory_steps = len(percent_in)
            assert trajectory_steps == self.trajectory_steps
            reward_ema = float(self.server.get("reward_ema").decode("utf-8"))
            reward_emsd = float(self.server.get("reward_emsd").decode("utf-8"))

            """
            plan of attack for working through trajectories:
            values = []
            v_traces = []
            fetch and normalize the final time states
            calculate encoding and proposal
            value = critic(encoding, proposal)
            v_next = value
            v_trace = value
            values.append(value)
            v_traces.append(v_trace)
            initialize c to 1
            loop backwards through the trajectories:
                fetch and normalize the time states
                value = critic(encoding)
                c *= pi / mu
                delta_v = (pi / mu) * (reward + gamma * v_next - value)
                v_trace = value + delta_v + gamma * c * (v_trace - v_next)
                values.append(value)
                v_traces.append(v_trace)
            """

            critic_loss = torch.Tensor([0]).cuda()
            actor_v_loss = torch.Tensor([0]).cuda()
            entropy_loss = torch.Tensor([0]).cuda()
            proposed_loss = torch.Tensor([0]).cuda()

            values = []
            v_traces = []
            time_states_ = torch.cat(time_states[-window:], dim=1).detach().cuda()
            mean = time_states_[:, :, :4].contiguous().view(len(experiences), -1).mean(1).view(len(experiences), 1, 1)
            std = time_states_[:, :, :4].contiguous().view(len(experiences), -1).std(1).view(len(experiences), 1, 1)
            time_states_[:, :, :4] = (time_states_[:, :, :4] - mean) / std
            spread_ = torch.Tensor(spread[-1]).view(-1, 1, 1).cuda() / std
            time_states_ = time_states_.transpose(0, 1)
            # lstm
            # market_encoding = self.MEN.forward(time_states_, torch.Tensor(percent_in[-1]).cuda(), spread_, 'cuda')
            # attention
            market_encoding = self.MEN.forward(time_states_)
            market_encoding = self.ETO.forward(market_encoding, spread_, torch.Tensor(percent_in[-1]).cuda())
            proposed = self.PN.forward(market_encoding)

            _, target_value = self.ACN_.forward(market_encoding, proposed)
            proposed_loss_ = (-target_value).mean()
            proposed_loss += proposed_loss_ / self.trajectory_steps

            policy, value = self.ACN.forward(market_encoding, proposed)
            v_next = value
            v_trace = value
            values.append(value)
            v_traces.append(v_trace.detach())
            c = 1
            for i in range(1, self.trajectory_steps):
                time_states_ = torch.cat(time_states[-window-i:-i], dim=1).cuda()
                mean = time_states_[:, :, :4].contiguous().view(len(experiences), -1).mean(1).view(len(experiences), 1, 1)
                std = time_states_[:, :, :4].contiguous().view(len(experiences), -1).std(1).view(len(experiences), 1, 1)
                time_states_[:, :, :4] = (time_states_[:, :, :4] - mean) / std
                spread_ = torch.Tensor(spread[-i]).view(-1, 1, 1).cuda() / std
                time_states_ = time_states_.transpose(0, 1)
                # lstm
                # market_encoding = self.MEN.forward(time_states_, torch.Tensor(percent_in[-i-1]).cuda(), torch.Tensor(spread[-i-1]), 'cuda')
                # attention
                market_encoding = self.MEN.forward(time_states_)
                market_encoding = self.ETO.forward(market_encoding, spread_, torch.Tensor(percent_in[-i-1]).cuda())

                proposed = self.PN.forward(market_encoding)

                _, target_value = self.ACN_.forward(market_encoding, proposed)
                proposed_loss_ = (-target_value).mean()
                proposed_loss += proposed_loss_ / self.trajectory_steps

                policy, value = self.ACN.forward(market_encoding, proposed)
                pi_ = policy.gather(1, torch.Tensor(place_action[-i-1]).cuda().long().view(-1, 1))
                mu_ = torch.Tensor(mu[-i-1]).cuda().view(-1, 1)

                c *= torch.min(self.max_c, pi_ / mu_)
                r = (torch.Tensor(reward[-i]).cuda().view(-1, 1) - reward_ema) / (reward_emsd + 1e-9)

                actor_v_loss_ = -torch.max(torch.Tensor([-10]).cuda(), torch.log(pi_))
                actor_v_loss_ *= torch.min(self.max_rho, pi_/mu_)
                actor_v_loss_ *= r + self.gamma * v_trace - value
                actor_v_loss_ = actor_v_loss_.mean()
                actor_v_loss += actor_v_loss_ / self.trajectory_steps

                entropy_loss_ = (policy * torch.max(torch.Tensor([-10]).cuda(), torch.log(policy))).mean()
                entropy_loss += entropy_loss_ / self.trajectory_steps

                delta_v = (pi_ / mu_) * (r + self.gamma * v_next - value)
                v_trace = value + delta_v + self.gamma * c * (v_trace - v_next)

                critic_loss_ = F.l1_loss(value, v_trace)
                critic_loss += critic_loss_ / self.trajectory_steps

                values.append(value)
                v_traces.append(v_trace.detach())
                v_next = value.detach()

            normalized_proposed_loss = self.server.get("proposer_ema").decode("utf-8")
            normalized_critic_loss = self.server.get("critic_ema").decode("utf-8")
            normalized_actor_v_loss = self.server.get("actor_v_ema").decode("utf-8")
            normalized_entropy_loss = self.server.get("entropy_ema").decode("utf-8")
            if normalized_proposed_loss == 'None':
                normalized_proposed_loss = float(proposed_loss)
                normalized_critic_loss = float(critic_loss)
                normalized_actor_v_loss = float(actor_v_loss)
                normalized_entropy_loss = float(entropy_loss)
                self.server.set("proposer_ema", normalized_proposed_loss)
                self.server.set("critic_ema", normalized_critic_loss)
                self.server.set("actor_v_ema", normalized_actor_v_loss)
                self.server.set("entropy_ema", normalized_entropy_loss)
            else:
                normalized_proposed_loss = float(self.proposer_tau * proposed_loss + (1 - self.proposer_tau) * float(normalized_proposed_loss))
                normalized_critic_loss = float(self.critic_tau * critic_loss + (1 - self.critic_tau) * float(normalized_critic_loss))
                normalized_actor_v_loss = float(self.actor_v_tau * actor_v_loss + (1 - self.actor_v_tau) * float(normalized_actor_v_loss))
                normalized_entropy_loss = float(self.entropy_tau * entropy_loss + (1 - self.entropy_tau) * float(normalized_entropy_loss))
                self.server.set("proposer_ema", normalized_proposed_loss)
                self.server.set("critic_ema", normalized_critic_loss)
                self.server.set("actor_v_ema", normalized_actor_v_loss)
                self.server.set("entropy_ema", normalized_entropy_loss)

            proposed_weight = self.proposed_weight
            critic_weight = self.critic_weight
            actor_v_weight = self.actor_v_weight
            entropy_weight = self.entropy_weight

            total_loss = proposed_loss * proposed_weight
            total_loss += critic_loss * critic_weight
            total_loss += actor_v_loss * actor_v_weight
            total_loss += entropy_loss * entropy_weight

            assert torch.isnan(total_loss).sum() == 0
            total_loss.backward()
            self.optimizer.step()

            step += 1
            n_samples += n_experiences * self.trajectory_steps

            if prev_reward_ema != None:
                self.ACN.state_dict()['critic2.weight'] = self.ACN.state_dict()['critic2.weight'] * reward_emsd / (prev_reward_emsd + 1e-9)
                self.ACN.state_dict()['critic2.bias'] = self.ACN.state_dict()['critic2.bias'] * ((reward_emsd + 1e-9) + reward_ema - prev_reward_ema) / (prev_reward_emsd + 1e-9)

            prev_reward_ema = reward_ema
            prev_reward_emsd = reward_emsd

            print("n samples: {n}, steps: {s}".format(n=n_samples, s=step))
            print("weighted losses: \n\tproposed: {p} \
            \n\tcritic: {c} \
            \n\tactor_v: {a_v} \
            \n\tentropy: {e} \n".format(p=float(proposed_loss * proposed_weight),
            c=float(critic_loss * critic_weight),
            a_v=float(actor_v_loss * actor_v_weight),
            e=float(entropy_loss * entropy_weight)))

            print("loss emas: \n\tproposed: {p} \
            \n\tcritic: {c} \
            \n\tactor_v: {a_v} \
            \n\tentropy: {e} \n".format(p=normalized_proposed_loss,
            c=normalized_critic_loss,
            a_v=normalized_actor_v_loss,
            e=normalized_entropy_loss))

            try:
                torch.save(self.ETO.state_dict(), self.models_loc + 'encoder_to_others.pt')
                torch.save(self.PN.state_dict(), self.models_loc + "proposer.pt")
                torch.save(self.ACN.state_dict(), self.models_loc + "actor_critic.pt")
                cur_state = {
                    'n_samples':n_samples,
                    'steps':step,
                    'optimizer':self.optimizer.state_dict()
                }
                torch.save(cur_state, self.models_loc + 'rl_train.pt')
                self.ACN_.load_state_dict(torch.load(self.models_loc + 'actor_critic.pt'))
            except Exception:
                print("failed to save")

            # for i, experience in enumerate(self.queued_experience):
            #     if len(self.prioritized_experience) == self.prioritized_batch_size and self.prioritized_batch_size != 0 and len(self.queued_experience) != len(experiences):
            #         smallest, i_smallest = torch.min(torch.abs((initial_value - v_trace)[len(self.queued_experience):]), dim=0)
            #         if torch.abs((initial_value - v_trace)[i]) > smallest:
            #             self.prioritized_experience[i_smallest] = experience
            #             (initial_value - v_trace)[len(self.queued_experience) + i_smallest] = torch.abs((initial_value - v_trace)[i])
            #     elif self.prioritized_batch_size != 0:
            #         self.prioritized_experience.append(experience)

            self.queued_experience = []
