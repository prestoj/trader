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

torch.manual_seed(0)
torch.set_default_tensor_type(torch.cuda.FloatTensor)


class Optimizer(object):

    def __init__(self, models_loc):
        self.models_loc = models_loc
        # networks
        # this is the lstm's version
        # self.MEN = MarketEncoder().cuda()
        # this is the attention version
        self.MEN = CNNEncoder().cuda()
        self.ETO = EncoderToOthers().cuda()
        self.PN = ProbabilisticProposer().cuda()
        self.ACN = ActorCritic().cuda()
        try:
            self.MEN.load_state_dict(torch.load(self.models_loc + 'market_encoder.pt'))
            self.ETO.load_state_dict(torch.load(self.models_loc + 'encoder_to_others.pt'))
            self.PN.load_state_dict(torch.load(self.models_loc + 'proposer.pt'))
            self.ACN.load_state_dict(torch.load(self.models_loc + 'actor_critic.pt'))
        except FileNotFoundError:
            self.MEN = CNNEncoder().cuda()
            self.ETO = EncoderToOthers().cuda()
            self.PN = ProbabilisticProposer().cuda()
            self.ACN = ActorCritic().cuda()

            torch.save(self.MEN.state_dict(), self.models_loc + 'market_encoder.pt')
            torch.save(self.ETO.state_dict(), self.models_loc + 'encoder_to_others.pt')
            torch.save(self.PN.state_dict(), self.models_loc + 'proposer.pt')
            torch.save(self.ACN.state_dict(), self.models_loc + 'actor_critic.pt')
        self.ACN_ = ActorCritic().cuda()
        self.ACN_.load_state_dict(self.ACN.state_dict())

        self.server = redis.Redis("localhost")
        self.actor_temp_cooldown = float(self.server.get("actor_temp_cooldown").decode("utf-8"))
        self.gamma = float(self.server.get("gamma").decode("utf-8"))
        self.trajectory_steps = int(self.server.get("trajectory_steps").decode("utf-8"))
        self.max_rho = torch.Tensor([float(self.server.get("max_rho").decode("utf-8"))]).cuda()
        self.max_c = torch.Tensor([float(self.server.get("max_c").decode("utf-8"))]).cuda()

        self.proposed_v_weight = float(self.server.get("proposed_v_weight").decode("utf-8"))
        self.proposed_entropy_weight = float(self.server.get("proposed_entropy_weight").decode("utf-8"))
        self.critic_weight = float(self.server.get("critic_weight").decode("utf-8"))
        self.actor_v_weight = float(self.server.get("actor_v_weight").decode("utf-8"))
        self.actor_entropy_weight = float(self.server.get("actor_entropy_weight").decode("utf-8"))
        self.weight_penalty = float(self.server.get("weight_penalty").decode("utf-8"))

        self.learning_rate = float(self.server.get("learning_rate").decode("utf-8"))

        self.queued_batch_size = int(self.server.get("queued_batch_size").decode("utf-8"))
        self.prioritized_batch_size = int(self.server.get("prioritized_batch_size").decode("utf-8"))

        self.queued_experience = []
        self.prioritized_experience = []
        try:
            self.optimizer = optim.Adam([param for param in self.MEN.parameters()] +
                                        [param for param in self.ETO.parameters()] +
                                        [param for param in self.PN.parameters()] +
                                        [param for param in self.ACN.parameters()],
                                        lr=self.learning_rate,
                                        weight_decay=self.weight_penalty)
            checkpoint = torch.load(models_loc + "rl_train.pt")
            self.optimizer.load_state_dict(checkpoint['optimizer'])
            self.start_step = checkpoint['steps']
            self.start_n_samples = checkpoint['n_samples']
            self.actor_temp = checkpoint['actor_temp']

        except:
            self.optimizer = optim.Adam([param for param in self.MEN.parameters()] +
                                        [param for param in self.ETO.parameters()] +
                                        [param for param in self.PN.parameters()] +
                                        [param for param in self.ACN.parameters()],
                                        lr=self.learning_rate,
                                        weight_decay=self.weight_penalty)
            self.start_step = 1
            self.start_n_samples = 0
            self.actor_temp = 1
            cur_state = {
                'n_samples':self.start_n_samples,
                'steps':self.start_step,
                'actor_temp':self.actor_temp,
                'optimizer':self.optimizer.state_dict()
            }
            torch.save(self.optimizer.state_dict(), self.models_loc + 'rl_train.pt')

        self.original_actor_temp = 1
        self.server.set("actor_temp", self.actor_temp)

    def run(self):
        self.MEN.train()
        self.ETO.train()
        self.PN.train()
        self.ACN.train()
        self.ACN_.train()

        prev_reward_ema = None
        prev_reward_emsd = None
        n_samples = self.start_n_samples
        step = self.start_step
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
                elif (step != 1 or (step == 1 and len(self.queued_experience) == self.queued_batch_size)) and len(self.queued_experience) + len(self.prioritized_experience) > 1:
                    break
                else:
                    experience = self.server.blpop("experience")[1]
                    experience = msgpack.unpackb(experience, raw=False)
                    self.queued_experience.append(experience)
                    n_experiences += 1

            experiences = self.queued_experience + self.prioritized_experience
            batch_size = len(experiences)

            # if step == 25000 or step == 250000 or step == 2500000:
            #     for param_group in self.optimizer.param_groups:
            #         param_group['lr'] = param_group['lr'] / 10

            if step == 10000 or step == 100000 or step == 1000000:
                for param_group in self.optimizer.param_groups:
                    param_group['lr'] = param_group['lr'] / 10

            # start grads anew
            self.optimizer.zero_grad()

            # get the inputs to the networks in the right form
            batch = Experience(*zip(*experiences))
            time_states = [*zip(*batch.time_states)]
            for i, time_state_ in enumerate(time_states):
                time_states[i] = torch.Tensor(time_state_).view(batch_size, 1, networks.D_BAR)
            percent_in = [*zip(*batch.percents_in)]
            spread = [*zip(*batch.spreads)]
            mu = [*zip(*batch.mus)]
            queried_mus = [*zip(*batch.proposed_mus)]
            proposed_actions = [*zip(*batch.proposed_actions)]
            place_action = [*zip(*batch.place_actions)]
            reward = [*zip(*batch.rewards)]

            window = len(time_states) - len(percent_in)
            assert window == networks.WINDOW
            assert len(percent_in) == self.trajectory_steps

            reward_ema = float(self.server.get("reward_ema").decode("utf-8"))
            reward_emsd = float(self.server.get("reward_emsd").decode("utf-8"))

            critic_loss = torch.Tensor([0]).cuda()
            actor_v_loss = torch.Tensor([0]).cuda()
            actor_entropy_loss = torch.Tensor([0]).cuda()
            proposed_v_loss = torch.Tensor([0]).cuda()
            proposed_entropy_loss = torch.Tensor([0]).cuda()

            time_states_ = torch.cat(time_states[-window:], dim=1).detach().cuda()
            mean = time_states_[:, :, :4].contiguous().view(batch_size, -1).mean(1).view(batch_size, 1, 1)
            std = time_states_[:, :, :4].contiguous().view(batch_size, -1).std(1).view(batch_size, 1, 1)
            time_states_[:, :, :4] = (time_states_[:, :, :4] - mean) / std
            spread_ = torch.Tensor(spread[-1]).view(-1, 1, 1).cuda() / std
            time_states_ = time_states_.transpose(0, 1)

            market_encoding = self.MEN.forward(time_states_)
            market_encoding = self.ETO.forward(market_encoding, spread_, torch.Tensor(percent_in[-1]).cuda())
            queried = torch.Tensor(proposed_actions[-1]).cuda().view(batch_size, -1)
            policy, value = self.ACN.forward(market_encoding, queried)

            v_next = value.detach()
            v_trace = value.detach()
            for i in range(1, self.trajectory_steps):

                time_states_ = torch.cat(time_states[-window-i:-i], dim=1).cuda()
                mean = time_states_[:, :, :4].contiguous().view(batch_size, -1).mean(1).view(batch_size, 1, 1)
                std = time_states_[:, :, :4].contiguous().view(batch_size, -1).std(1).view(batch_size, 1, 1)
                time_states_[:, :, :4] = (time_states_[:, :, :4] - mean) / std
                spread_ = torch.Tensor(spread[-i-1]).view(-1, 1, 1).cuda() / std
                time_states_ = time_states_.transpose(0, 1)

                market_encoding = self.MEN.forward(time_states_)
                market_encoding = self.ETO.forward(market_encoding, spread_, torch.Tensor(percent_in[-i-1]).cuda())
                queried = torch.Tensor(proposed_actions[-i-1]).cuda().view(batch_size, -1)
                proposed, proposed_pi, p_x_w, p_x_mu, p_x_sigma = self.PN.forward(market_encoding, return_params=True)
                queried_pi = self.PN.p(queried.detach(), p_x_w, p_x_mu, p_x_sigma)
                queried_pi_combined = queried_pi.prod(1).view(-1, 1)
                policy, value = self.ACN.forward(market_encoding, queried)

                pi_ = policy.gather(1, torch.Tensor(place_action[-i-1]).cuda().long().view(-1, 1))
                mu_ = torch.Tensor(mu[-i-1]).cuda().view(-1, 1)
                queried_mu = torch.Tensor(queried_mus[-i-1]).cuda().view(-1, 1)

                r = torch.Tensor(reward[-i-1]).cuda().view(-1, 1)

                if i == self.trajectory_steps - 1:
                    rho = torch.min(self.max_rho, (pi_/(mu_ + 1e-9)) * (queried_pi_combined / (queried_mu + 1e-9)))
                    advantage_v = rho * (r + self.gamma * v_trace - value)
                    actor_v_loss += (-torch.log(pi_ + 1e-9) * advantage_v.detach()).mean()
                    proposed_v_loss += (-torch.log(queried_pi_combined + 1e-9) * advantage_v.detach()).mean()

                    actor_entropy_loss += (policy * torch.log(policy + 1e-9)).mean()
                    proposed_entropy_loss += (proposed_pi * torch.log(proposed_pi + 1e-9)).mean()
                    # proposed_entropy_loss += (proposed * torch.log(proposed + 1e-9)).mean() # not exactly entropy, but ehh

                rho = torch.min(self.max_rho, (pi_ / (mu_ + 1e-9)) * (queried_pi_combined / (queried_mu + 1e-9)))
                delta_v = rho * (r + self.gamma * v_next - value)
                c = torch.min(self.max_c, (pi_ / (mu_ + 1e-9)) * (queried_pi_combined / (queried_mu + 1e-9)))

                v_trace = (value + delta_v + self.gamma * c * (v_trace - v_next)).detach()

                if i == self.trajectory_steps - 1:
                    critic_loss += nn.MSELoss()(value, v_trace.detach())

                v_next = value.detach()

            total_loss = torch.Tensor([0]).cuda()
            total_loss += proposed_entropy_loss * self.proposed_entropy_weight
            total_loss += proposed_v_loss * self.proposed_v_weight
            total_loss += critic_loss * self.critic_weight
            total_loss += actor_v_loss * self.actor_v_weight
            total_loss += actor_entropy_loss * self.actor_entropy_weight

            try:
                assert torch.isnan(total_loss).sum() == 0
            except AssertionError:
                print("proposed_v_loss", proposed_v_loss)
                print("proposed_entropy_loss", proposed_entropy_loss)
                print("critic_loss", critic_loss)
                print("actor_v_loss", actor_v_loss)
                print("actor_entropy_loss", actor_entropy_loss)
                raise AssertionError("total loss is not 0")

            total_loss.backward()
            self.optimizer.step()

            step += 1
            n_samples += len(self.queued_experience)

            prev_reward_ema = reward_ema
            prev_reward_emsd = reward_emsd

            try:
                torch.save(self.MEN.state_dict(), self.models_loc + 'market_encoder.pt')
                torch.save(self.ETO.state_dict(), self.models_loc + 'encoder_to_others.pt')
                torch.save(self.PN.state_dict(), self.models_loc + "proposer.pt")
                torch.save(self.ACN.state_dict(), self.models_loc + "actor_critic.pt")
                cur_state = {
                    'n_samples':n_samples,
                    'steps':step,
                    'actor_temp':self.actor_temp,
                    'optimizer':self.optimizer.state_dict()
                }
                torch.save(cur_state, self.models_loc + 'rl_train.pt')
            except Exception:
                print("failed to save")

            dif = value.detach() - v_trace.detach()
            if batch_size > len(self.queued_experience):
                smallest, i_smallest = torch.min(torch.abs(dif[len(self.queued_experience):]), dim=0)
            for i, experience in enumerate(self.queued_experience):
                if len(self.prioritized_experience) == self.prioritized_batch_size and self.prioritized_batch_size != 0 and len(self.queued_experience) != batch_size:
                    if torch.abs(dif[i]) > smallest:
                        self.prioritized_experience[i_smallest] = experience
                        dif[len(self.queued_experience) + i_smallest] = torch.abs(dif[i])
                        smallest, i_smallest = torch.min(torch.abs(dif[len(self.queued_experience):]), dim=0)
                elif len(self.prioritized_experience) < self.prioritized_batch_size:
                    self.prioritized_experience.append(experience)

            self.actor_temp = 1 + (self.original_actor_temp - 1) * self.actor_temp_cooldown ** step
            self.server.set("actor_temp", self.actor_temp)
            self.queued_experience = []
            self.ACN_.load_state_dict(self.ACN.state_dict())
            torch.cuda.empty_cache()

            print('-----------------------------------------------------------')
            print("n samples: {n}, batch size: {b}, steps: {s}, time: {t}".format(n=n_samples, b=batch_size, s=step, t=round(time.time()-t0, 5)))
            print()

            print("policy[0] min mean std max:\n", round(policy[:, 0].cpu().detach().min().item(), 7), round(policy[:, 0].cpu().detach().mean().item(), 7), round(policy[:, 0].cpu().detach().std().item(), 7), round(policy[:, 0].cpu().detach().max().item(), 7))
            print("policy[1] min mean std max:\n", round(policy[:, 1].cpu().detach().min().item(), 7), round(policy[:, 1].cpu().detach().mean().item(), 7), round(policy[:, 1].cpu().detach().std().item(), 7), round(policy[:, 1].cpu().detach().max().item(), 7))
            print("policy[2] min mean std max:\n", round(policy[:, 2].cpu().detach().min().item(), 7), round(policy[:, 2].cpu().detach().mean().item(), 7), round(policy[:, 2].cpu().detach().std().item(), 7), round(policy[:, 2].cpu().detach().max().item(), 7))
            print()

            print("proposed[0] min mean std max:\n", round(proposed[:, 0].cpu().detach().min().item(), 7), round(proposed[:, 0].cpu().detach().mean().item(), 7), round(proposed[:, 0].cpu().detach().std().item(), 7), round(proposed[:, 0].cpu().detach().max().item(), 7))
            print("proposed[1] min mean std max:\n", round(proposed[:, 1].cpu().detach().min().item(), 7), round(proposed[:, 1].cpu().detach().mean().item(), 7), round(proposed[:, 1].cpu().detach().std().item(), 7), round(proposed[:, 1].cpu().detach().max().item(), 7))
            print()

            print("proposed probabilities min mean std max:\n", round(proposed_pi.cpu().detach().min().item(), 7), round(proposed_pi.cpu().detach().mean().item(), 7), round(proposed_pi.cpu().detach().std().item(), 7), round(proposed_pi.cpu().detach().max().item(), 7))
            print("queried probabilities min mean std max:\n", round(queried_pi.cpu().detach().min().item(), 7), round(queried_pi.cpu().detach().mean().item(), 7), round(queried_pi.cpu().detach().std().item(), 7), round(queried_pi.cpu().detach().max().item(), 7))
            print()

            queried_actions = torch.Tensor(proposed_actions)
            print("queried[0] min mean std max:\n", round(queried_actions[:, :, :, 0].cpu().detach().min().item(), 7), round(queried_actions[:, :, :, 0].cpu().detach().mean().item(), 7), round(queried_actions[:, :, :, 0].cpu().detach().std().item(), 7), round(queried_actions[:, :, :, 0].cpu().detach().max().item(), 7))
            print("queried[1] min mean std max:\n", round(queried_actions[:, :, :, 1].cpu().detach().min().item(), 7), round(queried_actions[:, :, :, 1].cpu().detach().mean().item(), 7), round(queried_actions[:, :, :, 1].cpu().detach().std().item(), 7), round(queried_actions[:, :, :, 1].cpu().detach().max().item(), 7))
            print()

            print("value min mean std max:\n", round(value.cpu().detach().min().item(), 7), round(value.cpu().detach().mean().item(), 7), round(value.cpu().detach().std().item(), 7), round(value.cpu().detach().max().item(), 7))
            print("v_trace min mean std max:\n", round(v_trace.cpu().detach().min().item(), 7), round(v_trace.cpu().detach().mean().item(), 7), round(v_trace.cpu().detach().std().item(), 7), round(v_trace.cpu().detach().max().item(), 7))
            print()

            print("weighted critic loss:", round(float(critic_loss * self.critic_weight), 7))
            print("weighted actor v loss:", round(float(actor_v_loss * self.actor_v_weight), 7))
            print("weighted actor entropy loss:", round(float(actor_entropy_loss * self.actor_entropy_weight), 7))
            print("weighted proposer v loss:", round(float(proposed_v_loss * self.proposed_v_weight), 7))
            print("weighted proposer entropy loss:", round(float(proposed_entropy_loss * self.proposed_entropy_weight), 7))
            print('-----------------------------------------------------------')
