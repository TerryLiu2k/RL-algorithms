import os
from algorithms.config.CACC_MBPO_radius2 import main
from algorithms.envs.CACC import CACC_catchup, CACC_slowdown
os.environ["CUDA_VISIBLE_DEVICES"] = "2"
#init_checkpoint = "checkpoints/CACC_catchup_MultiagentMBPO_20905/12001_-247.26344310442602.pt"
main(env_fn=CACC_slowdown, init_checkpoint=None, debug=False, test=False, seed=None, device=0)
