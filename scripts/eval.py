"""
Usage:
python eval.py --checkpoint data/image/pusht/diffusion_policy_cnn/train_0/checkpoints/latest.ckpt -o data/pusht_eval_output
"""

import sys
# use line-buffering for both stdout and stderr
sys.stdout = open(sys.stdout.fileno(), mode='w', buffering=1)
sys.stderr = open(sys.stderr.fileno(), mode='w', buffering=1)

try:
    from isaacgym.torch_utils import *
except:
    print("Isaac Gym Not Installed")

import os
import pathlib
import click
import hydra
import torch
import dill
import wandb
import json
from omegaconf import OmegaConf

from diffusion_policy.workspace.base_workspace import BaseWorkspace

@click.command()
@click.option('-c', '--checkpoint', required=True)
@click.option('-o', '--output_dir', required=True)
@click.option('-d', '--device', default='cuda:0')
@click.option('--headless', default=False)
@click.option('--online', default=True)
@click.option('--generate_data', default=False)
def main(checkpoint, output_dir, device, online, generate_data, **kwargs):
    # if os.path.exists(output_dir):
    #     click.confirm(f"Output path {output_dir} already exists! Overwrite?", abort=True)
    pathlib.Path(output_dir).mkdir(parents=True, exist_ok=True)
    
    # load checkpoint
    payload = torch.load(open(checkpoint, 'rb'), pickle_module=dill)
    cfg = payload['cfg']
    # action_steps = 4

    print(cfg['task']['env_runner']['_target_'], '\n\n')
    cfg['task']['env_runner']['_target_'] = 'diffusion_policy.env_runner.diffsionrobot_lowdim_cyber_runner.LeggedRunner'
    print(cfg['task']['env_runner']['n_obs_steps'])

    # cfg['n_action_steps'] = action_steps
    # cfg['task']['env_runner']['n_action_steps'] = action_steps
    # cfg['policy']['n_action_steps'] = action_steps
    OmegaConf.set_struct(cfg, False)

    cfg.task.env_runner['device'] = device
    
    cls = hydra.utils.get_class(cfg._target_)
    print(f"Loading workspace {cls.__name__}")
    workspace = cls(cfg, output_dir=output_dir)
    workspace: BaseWorkspace
    workspace.load_payload(payload, exclude_keys=None, include_keys=None)
    
    # get policy from workspace
    policy = workspace.model
    if cfg.training.use_ema:
        policy = workspace.ema_model
    
    device = torch.device(device)
    policy.to(device)
    policy.eval()
    
    # run eval
    env_runner = hydra.utils.instantiate(
        cfg.task.env_runner,
        output_dir=output_dir)
    runner_log = env_runner.run(policy, online=online, generate_data=generate_data)
    
    # dump log to json
    json_log = dict()
    for key, value in runner_log.items():
        if isinstance(value, wandb.sdk.data_types.video.Video):
            json_log[key] = value._path
        else:
            json_log[key] = value
    out_path = os.path.join(output_dir, 'eval_log.json')
    json.dump(json_log, open(out_path, 'w'), indent=2, sort_keys=True)

if __name__ == '__main__':
    main()
