from .NCS.large_grid_env import LargeGridEnv
import os
import numpy as np
import gym
import configparser

class ATSCWrapper(gym.Wrapper):
    def __init__(self):
        # k-hop
        config_path = "algorithms/envs/NCS/config/config_ma2c_nc_grid.ini"
        config_path = os.path.join(os.path.dirname("."), config_path)
        config = configparser.ConfigParser()
        config.read(config_path)
        config = config['ENV_CONFIG']
        env = LargeGridEnv(config)
        super().__init__(env)
        
    def reset(self):
        state = self.env.reset()
        state = np.array(state, dtype=np.float32)
        self.state = state
        return state    
        
    def step(self, action):
        state, reward, done, info = self.env.step(action)
        state = np.array(state, dtype=np.float32)
        reward = np.array(reward, dtype=np.float32)
        done = np.array([done]*25, dtype=np.float32)
        self.state=state
        return state, reward, done, None
    
def ATSCGrid():
    return ATSCWrapper()