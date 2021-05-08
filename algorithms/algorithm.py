import pdb
import numpy as np
import torch
import gym
import time
import random
from tqdm import tqdm
from .utils import combined_shape

class ReplayBuffer:
    """
    A simple FIFO experience replay buffer for SAC agents.
    Utilizes lazy frames of FrameStack to save memory.
    """

    def __init__(self, max_size, device, action_dtype):
        self.max_size = max_size
        self.data = []
        self.ptr = 0
        self.unread = 0
        self.device = device
        self.action_dtype = action_dtype

    def store(self, obs, act, rew, next_obs, done):
        """ not batched """
        if len(self.data) == self.ptr:
            self.data.append({})
        self.data[self.ptr] = {'s':obs, 'a':act, 'r':rew, 's1':next_obs, 'd':done}
        # lazy frames here
        # cuts Q bootstrap if done (next_obs is arbitrary)
        self.ptr = (self.ptr+1) % self.max_size
        
    def storeBatch(self, obs, act, rew, next_obs, done):
        """ 
            explicitly tell if batched since the first dim may be batch or n_agent
            does not convert to tensor, in order to utilze gym FrameStack LazyFrame
        """
        for i in range(done.shape[0]):
            self.store(obs[i], act[i], rew[i], next_obs[i], done[i])

    def sampleBatch(self, batch_size):
        idxs = np.random.randint(0, len(self.data), size=batch_size)
        raw_batch = [self.data[i] for i in idxs]
        batch = {}
        for key in raw_batch[0]:
            if key == 'a':
                dtype = self.action_dtype
            else: # done should be float for convenience
                dtype = torch.float
            lst = [torch.as_tensor(dic[key], dtype=dtype) for dic in raw_batch]
            batch[key] = torch.stack(lst).to(self.device)

        return batch
    
    def iterBatch(self, batch_size):
        """ reads backwards from ptr to use the most recent samples """
        if self.unread == 0:
            return None
        batch_size =  min(batch_size, self.unread)
        read_ptr = self.ptr - (len(self.data) - self.unread)
        idxs = list(range(read_ptr-batch_size, read_ptr))
        idxs = [(i + batch_size*len(self.data))%len(self.data) for i in idxs] 
        # make them in the correct range
        self.unread -= batch_size
        
        raw_batch = [self.data[i] for i in idxs]
        batch = {}
        for key in raw_batch[0]:
            if key == 'a':
                dtype = self.action_dtype
            else: # done should be float for convenience
                dtype = torch.float
            lst = [torch.as_tensor(dic[key], dtype=dtype) for dic in raw_batch]
            batch[key] = torch.stack(lst).to(self.device)
        return batch
    
    def clear(self):
        self.data = []
        self.ptr = 0
        self._rewind()
        
    def _rewind(self):
        self.unread = len(self.data)


class RL(object):
    def __init__(self, logger, device,
       env_fn, agent_args,
        n_warmup, batch_size, replay_size,
       max_ep_len, test_interval, save_interval,
       seed, n_step, log_interval,
       p_update_interval=None, q_update_interval=None, pi_update_interval=None,
       checkpoint_dir=None, start_step = 0,
       **kwargs):
        """ 
        a generic algorithm for single agent model-based actor-critic, 
        can also be used for model-free, actor-free or crtici-free RL
        For MARL, it is better to overload the agent into a meta-agent instead of overloading RL
        warmup:
            model, q, and policy each warmup for n_warmup steps before used
        """
        torch.manual_seed(seed)
        np.random.seed(seed)

        agent = agent_args.agent(logger=logger, **agent_args._toDict())
        agent = agent.to(device)
        if not checkpoint_dir is None:
            agent.load_state_dict(torch.load(f"checkpoints/{checkpoint_dir}/{start_step}.pt"))
            logger.log(interaction=start_step)
        
        self.env, self.test_env = env_fn(), env_fn()
        s, self.episode_len, self.episode_reward = self.env.reset(), 0, 0
        self.agent_args = agent_args
        self.agent = agent
        
        self.batch_size = batch_size
        self.start_step = start_step
        self.n_step = n_step
        self.max_ep_len = max_ep_len

        self.logger = logger
        self.device=device
        
        self.save_interval = save_interval
        self.log_interval = log_interval
        self.test_interval = test_interval
        
        # Experience buffer
        if isinstance(self.env.action_space, gym.spaces.Discrete):
            action_dtype = torch.long
        else:
            action_dtype = torch.float
            
        self.env_buffer = ReplayBuffer(max_size=replay_size, device=device, action_dtype=action_dtype)
        if hasattr(agent, "ps"): # use the model buffer if there is a model
            self.buffer = ReplayBuffer(max_size=replay_size, device=device, action_dtype=action_dtype)
        else:
            self.buffer = self.env_buffer  

        # warmups
        self.n_warmup = n_warmup
        if hasattr(agent, "ps"):
            self.q_update_start = n_warmup + start_step
            # p and q starts at the same time, since q update also need p
            # warmup after loading a checkpoint, sicne I do not store replay buffer
            self.pi_update_start = n_warmup + start_step
            self.act_start = 2*n_warmup + start_step
        else:
            self.q_update_start = 0 + start_step
            self.pi_update_start = 0 + start_step
            self.act_start = n_warmup + start_step

        # update frequency
        p_args, q_args, pi_args = agent_args.p_args, agent_args.q_args, agent_args.pi_args
        # multiple gradient steps per sample if model based RL
        self.p_update_steps = 1
        self.q_update_steps = 1
        self.pi_update_steps = 1
        if hasattr(agent, "ps"):
            self.branch = agent_args.p_args.branch
            self.refresh_interval = self.agent_args.p_args.refresh_interval
            self.p_update_interval = p_args.update_interval
            if self.p_update_interval < 1:
                self.p_update_steps = int(1/self.p_update_interval)
                self.p_update_interval = 1

        if hasattr(agent, "pi"):
            self.pi_update_interval = pi_args.update_interval
            if self.pi_update_interval < 1:
                self.pi_update_steps = int(1/self.pi_update_interval)
                self.pi_update_interval = 1

        self.q_update_interval = q_args.update_interval
        if self.q_update_interval < 1:
            self.q_update_steps = int(1/self.q_update_interval)
            self.q_update_interval = 1

    def test(self):
        test_env = self.test_env
        test_env.reset()
        d, ep_ret, ep_len = np.array([False]), 0, 0
        while not(d.all() or (ep_len == self.max_ep_len)):
            # Take deterministic actions at test time 
            state = torch.as_tensor(test_env.state, dtype=torch.float).to(self.device)
            action = self.agent.act(state.unsqueeze(0), deterministic=True).squeeze(0)
            # [b=1, (n_agent), ...]
            _, r, d, _ = test_env.step(action.cpu().numpy().squeeze())
            d=np.array(d)
            ep_ret += r
            ep_len += 1
        self.logger.log(TestEpRet=ep_ret.mean(), TestEpLen=ep_len, test_episode=None)
        
    def updateAgent(self):
        agent = self.agent
        batch_size = self.batch_size
        env_buffer, buffer = self.env_buffer, self.buffer
        t = self.t
        # Update handling
        if hasattr(agent, "ps") and (t % self.p_update_interval) == 0 and t>batch_size:
            for i in range(self.p_update_steps):
                batch = env_buffer.sampleBatch(batch_size)
                agent.updateP(**batch)

        if hasattr(agent, "q1") and t>self.q_update_start and t % self.q_update_interval == 0:
            for i in range(self.q_update_steps):
                batch = buffer.sampleBatch(batch_size)
                agent.updateQ(**batch)

        if hasattr(agent, "pi") and t>self.pi_update_start and t % self.pi_update_interval == 0:
            for i in range(self.pi_update_steps):
                batch = buffer.sampleBatch(batch_size)
                agent.updatePi(**batch)
                
    def roll(self):
        """
            updates the buffer using model rollouts, using the most recent samples in env_buffer
            stops when the buffer is full (max_size + bacthsize -1) or the env_buffer is exhausted
        """
        env_buffer = self.env_buffer
        buffer = self.buffer
        batch_size = self.batch_size
        env_buffer._rewind()
        buffer.clear()
        batch = env_buffer.iterBatch(self.batch_size)
        while not batch is None and len(buffer.data) < buffer.max_size:
            s = batch['s']
            a = self.agent.act(s)
            for i in range(self.branch):
                r, s1, d = self.agent.roll(s=s, a=a)
                buffer.storeBatch(s, a, r, s1, d)
                if len(buffer.data) >= buffer.max_size:
                    break
            batch = env_buffer.iterBatch(batch_size)
            
    def step(self):
        env = self.env
        state = env.state
        self.logger.log(interaction=None)
        state = torch.as_tensor(state, dtype=torch.float).to(self.device)
        eps = (self.act_start - self.t)/(self.act_start - self.pi_update_start)
        self.agent.eps = np.clip(eps, 0, 1)
        a = self.agent.act(torch.as_tensor(state, dtype=torch.float).to(self.device).unsqueeze(0))    
        a = a.squeeze(0).detach().cpu().numpy()
        # Step the env
        s1, r, d, _ = env.step(a)
        self.episode_reward += r
        self.episode_len += 1
        if self.episode_len == self.max_ep_len:
            """
                some envs return done when episode len is maximum,
                this breaks the Markov property
            """
            d = np.zeros(d.shape, dtype=np.float32)
        d = np.array(d)
        self.env_buffer.store(state, a, r, s1, d)
        if d.all() or (self.episode_len == self.max_ep_len):
            """ for compatibility, allow different agents to have different done"""
            self.logger.log(episode_reward=self.episode_reward.mean(), episode_len=self.episode_len, episode=None)
            _, self.episode_reward, self.episode_len = self.env.reset(), 0, 0
        
    def run(self):
        # Main loop: collect experience in env and update/log each epoch
        last_save = 0
        pbar = iter(tqdm(range(int(1e8))))
        for t in range(self.start_step, self.n_step): 
            next(pbar)
            self.t = t
            self.step()
            if hasattr(self.agent, "ps") and t >=self.n_warmup+self.start_step \
                and (t% self.refresh_interval == 0 or len(self.buffer.data) is 0):
                self.roll()
                
            self.updateAgent()
            
            if time.time() - last_save >= self.save_interval:
                self.logger.save(self.agent)
                last_save = time.time()

            if t % self.test_interval == 0:
                self.test()

            if t % self.log_interval == 0:
                self.logger.flush()    