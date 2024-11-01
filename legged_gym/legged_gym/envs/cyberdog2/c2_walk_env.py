import torch
from collections import deque
from legged_gym.envs.base.legged_robot import LeggedRobot
from legged_gym.envs.cyberdog2.c2_env import CyberEnv
from legged_gym.envs.cyberdog2.c2_env import normalize_range
from isaacgym.torch_utils import quat_apply, quat_rotate_inverse, quat_conjugate
from legged_gym.utils.math import wrap_to_pi, quat_apply_yaw_inverse, quat_apply_yaw
import numpy as np
from isaacgym import gymtorch
from isaacgym.torch_utils import *
import pickle
import os, copy
from PIL import Image as im
from PIL import ImageDraw
from isaacgym.terrain_utils import *

# import rospy
# from std_msgs.msg import Float32MultiArray

class CyberWalkEnv(CyberEnv):

    def get_diffusion_action(self):
        return self.actions
    def get_diffusion_observation(self):
        commands = self.commands.clone()
        mask = commands[:,1] != 0
        commands[mask, 0] = commands[mask, 1]
        commands[:, 2] = 0
        commands[:, 1] = -1
        return  torch.cat((  
            self.projected_gravity,
            self.projected_forward_vec,
            commands[:, :3] * self.commands_scale[:3],
            (self.dof_pos - self.default_dof_pos) * self.obs_scales.dof_pos,
            self.dof_vel * self.obs_scales.dof_vel,
            self.actions,       
        ), dim=-1)    
    

    def _compute_common_obs(self):
        # self.commands[::2,2] = 0.05*np.pi
        # self.commands[1::2,2] = -0.05*np.pi
        # self.commands[self.episode_length_buf < 50, 2] = 0.
        obs_commands = self.commands[:, :3]
        obs_commands[:, [0,1]] = obs_commands[:, [1,0]]
        obs_commands[:, 2] = 0. 
        common_obs_buf = torch.cat((self.projected_gravity,
                                    self.projected_forward_vec,
                                    obs_commands * self.commands_scale,
                                    (self.dof_pos - self.default_dof_pos) * self.obs_scales.dof_pos,
                                    self.dof_vel * self.obs_scales.dof_vel,
                                    self.actions,
                                    self.clock_inputs[:, -2:],
                                    ),dim=-1)
        if self.cfg.env.obs_t: #default is False
            common_obs_buf = torch.cat([
                common_obs_buf, 
                torch.clamp(self.episode_length_buf / self.cfg.rewards.allow_contact_steps, 0., 1.).unsqueeze(dim=-1)
            ], dim=-1)
        # self.pub.publish(Float32MultiArray(data=(self.root_states[:,:2]).reshape(-1).cpu().numpy()))

        return common_obs_buf
    
    def _get_noise_scale_vec(self, cfg):#+6 hand targets
        """ Sets a vector used to scale the noise added to the observations.
            [NOTE]: Must be adapted when changing the observations structure

        Args:
            cfg (Dict): Environment config file

        Returns:
            [torch.Tensor]: Vector of scales used to multiply a uniform distribution in [-1, 1]
        """
        noise_vec = torch.zeros(self.cfg.env.num_single_state, dtype=torch.float, device=self.device)
        self.add_noise = self.cfg.noise.add_noise
        noise_scales = self.cfg.noise.noise_scales
        noise_level = self.cfg.noise.noise_level
        start_index = 0
        noise_vec[start_index:start_index + 3] = noise_scales.gravity * noise_level
        noise_vec[start_index + 3: start_index + 6] = noise_scales.gravity * noise_level
        start_index += 6
        noise_vec[start_index: start_index + 3] = 0.
        noise_vec[start_index + 3:start_index + 15] = noise_scales.dof_pos * noise_level * self.obs_scales.dof_pos
        noise_vec[start_index + 15:start_index + 27] = noise_scales.dof_vel * noise_level * self.obs_scales.dof_vel
        noise_vec[start_index + 27:start_index + 39] = 0. # previous actions
        noise_vec[start_index + 39: start_index + 41] = 0. # clock input
        start_index = start_index + 41
        assert start_index == self.cfg.env.num_single_state
        return noise_vec
    
    def check_termination(self):
        """ Check if environments need to be reset
        """
        # only explicitly allow foot contact in these mercy steps
        # self.reset_buf = torch.logical_and(
        #     torch.any(torch.norm(self.contact_forces[:, self.termination_contact_indices, :], dim=-1) > 1., dim=1),
        #     torch.logical_not(torch.logical_and(
        #         torch.any(torch.norm(self.contact_forces[:, self.allow_initial_contact_indices, :], dim=-1) > 1., dim=1),
        #         self.episode_length_buf <= self.cfg.rewards.allow_contact_steps
        #     ))
        # )
        self.reset_buf = 0 * torch.any(torch.norm(self.contact_forces[:, self.termination_contact_indices, :], dim=-1) > 1., dim=1)
        
        self.time_out_buf = self.episode_length_buf > self.max_episode_length # no terminal reward for time-outs
        self.reset_buf |= self.time_out_buf


    def update_command_curriculum(self, env_ids):
        if "tracking_lin_vel" in self.episode_sums and torch.mean(self.episode_sums["tracking_lin_vel"][env_ids]) / self.max_episode_length > 0.8 * self.reward_scales["tracking_lin_vel"]:
            # self.command_ranges["lin_vel_x"][0] = 0. # no backward vel
            self.command_ranges["lin_vel_x"][0] = np.clip(self.command_ranges["lin_vel_x"][0] - 0.2, -self.cfg.commands.max_curriculum, 0.)
            self.command_ranges["lin_vel_x"][1] = np.clip(self.command_ranges["lin_vel_x"][1] + 0.2, 0., self.cfg.commands.max_curriculum)
            # no side vel
            self.command_ranges["lin_vel_y"][0] = np.clip(self.command_ranges["lin_vel_y"][0] - 0.2, -self.cfg.commands.max_curriculum, 0.)
            self.command_ranges["lin_vel_y"][1] = np.clip(self.command_ranges["lin_vel_y"][1] + 0.2, 0., self.cfg.commands.max_curriculum)
        if "tracking_ang_vel" in self.episode_sums and torch.mean(self.episode_sums["tracking_ang_vel"][env_ids]) / self.max_episode_length > 0.8 * self.reward_scales["tracking_ang_vel"]:
            self.command_ranges["ang_vel_yaw"][0] = np.clip(self.command_ranges["ang_vel_yaw"][0] - 0.2, -self.cfg.commands.max_curriculum, 0.)
            self.command_ranges["ang_vel_yaw"][1] = np.clip(self.command_ranges["ang_vel_yaw"][1] + 0.2, 0., self.cfg.commands.max_curriculum)

    def _init_buffers(self):
        super()._init_buffers()
        self.last_heading = torch.zeros((self.num_envs,), dtype=torch.float, device=self.device)
        self.init_feet_positions = torch.zeros((self.num_envs, 4, 3), dtype=torch.float, device=self.device)
        # rospy.init_node('traj', anonymous=True)
        # self.pub = rospy.Publisher('/traj', Float32MultiArray, queue_size=10)
        
    def _resample_commands(self, env_ids):
        super()._resample_commands(env_ids)
        if self.cfg.commands.discretize:
            conti_velx_cmd = self.commands[env_ids, 0:1]
            self.commands[env_ids, 0:1] = torch.sign(conti_velx_cmd) * torch.round(torch.abs(conti_velx_cmd) / 0.1) * 0.1

    def reset_idx(self, env_ids):
        super().reset_idx(env_ids)
        heading = self._get_cur_heading()
        self.last_heading[env_ids] = heading[env_ids]
    
    def post_physics_step(self):
        """ check terminations, compute observations and rewards
            calls self._post_physics_step_callback() for common computations 
            calls self._draw_debug_vis() if needed
        """
        self.gym.refresh_actor_root_state_tensor(self.sim)
        self.gym.refresh_net_contact_force_tensor(self.sim)
        self.gym.refresh_rigid_body_state_tensor(self.sim)
        self.gym.refresh_jacobian_tensors(self.sim)

        self.episode_length_buf += 1
        self.common_step_counter += 1

        # prepare quantities
        self.base_pos[:] = self.root_states[:, :3]
        self.base_quat[:] = self.root_states[:, 3:7]
        self.base_lin_vel[:] = quat_rotate_inverse(self.base_quat, self.root_states[:, 7:10])
        self.base_ang_vel[:] = quat_rotate_inverse(self.base_quat, self.root_states[:, 10:13])
        self.projected_gravity[:] = quat_rotate_inverse(self.base_quat, self.gravity_vec)
        self.projected_forward_vec[:] = quat_rotate_inverse(self.base_quat, self.forward_vec)
        self.foot_velocities = self.rigid_body_state.view(self.num_envs, self.num_bodies, 13
                                                          )[:, self.feet_indices, 7:10]
        self.foot_velocities_ang = self.rigid_body_state.view(self.num_envs, self.num_bodies, 13
                                                              )[:, self.feet_indices, 10:13]
        self.foot_positions = self.rigid_body_state.view(self.num_envs, self.num_bodies, 13)[:, self.feet_indices, 0:3]

        self.calf_positions = self.rigid_body_state.view(self.num_envs, self.num_bodies, 13)[:, self.calf_indices, 0:3]
        self._post_physics_step_callback()

        # compute observations, rewards, resets, ...
        self.check_termination()
        self.compute_reward()
        env_ids = self.reset_buf.nonzero(as_tuple=False).flatten()
        self.reset_idx(env_ids)
        self.compute_observations() # in some cases a simulation step might be required to refresh some obs (for example body positions)

        self.last_actions[:] = self.actions[:]
        self.last_dof_vel[:] = self.dof_vel[:]
        self.last_root_vel[:] = self.root_states[:, 7:13]
        self.last_dof_pos[:] = self.dof_pos[:]

        if self.viewer and self.enable_viewer_sync and self.debug_viz:
            self._draw_debug_vis()

        self.last_heading[:] = self._get_cur_heading()
        self.init_feet_positions[self.episode_length_buf == 1] = self.foot_positions[self.episode_length_buf == 1]

    def _reset_dofs_rand(self, env_ids):
        """ Resets DOF position and velocities of selected environmments
        Positions are randomly selected within 0.5:1.5 x default positions.
        Velocities are set to zero.

        Args:
            env_ids (List[int]): Environemnt ids
        """
        self.dof_pos[env_ids] = self.init_dof_pos_range[:, 0] + torch_rand_float(0., 1., (len(env_ids), self.num_dof), device=self.device) * (self.init_dof_pos_range[:, 1] - self.init_dof_pos_range[:, 0])
        
        self.dof_vel[env_ids] = torch_rand_float(-0.1, 0.1, (len(env_ids), self.num_dof), device=self.device)

        # TODO: Important! should feed actor id, not env id
        env_ids_int32 = self.num_actors * env_ids.to(dtype=torch.int32)
        self.gym.set_dof_state_tensor_indexed(self.sim,
                                              gymtorch.unwrap_tensor(self.dof_state),
                                              gymtorch.unwrap_tensor(env_ids_int32), len(env_ids_int32))
    
    def _reset_robot_states(self, env_ids):
        self._reset_dofs_rand(env_ids)       
        self._reset_root_states_rand(env_ids)
    
    def _reset_root_states_rand(self, env_ids): #changed!
        """ Resets ROOT states position and velocities of selected environmments
            Sets base position based on the curriculum
            Selects randomized base velocities within -0.5:0.5 [m/s, rad/s]
        Args:
            env_ids (List[int]): Environemnt ids
        """
        # base position
        if self.custom_origins:
            self.root_states[env_ids] = self.base_init_state
            self.root_states[env_ids, :3] += self.env_origins[env_ids]
            if self.cfg.mode == "train":
                self.root_states[env_ids, 1:2] += torch_rand_float(-2., 2., (len(env_ids), 1), device=self.device) # y position within 2m of the center
            self.env_origins_new[env_ids] = self.root_states[env_ids, :3]
        else:
            self.root_states[env_ids] = self.base_init_state
            self.root_states[env_ids, :3] += self.env_origins[env_ids]
        if self.cfg.init_state.randomize_rot:
            rand_rpy = torch_rand_float(-np.pi*15/180.0, np.pi*15/180.0, (len(env_ids), 3), device=self.device) #参数：rand在正负15度
            rand_rpy=rand_rpy+torch.Tensor(get_euler_xyz(self.base_init_state[3:7].unsqueeze(0))).to(self.device)
            self.root_states[env_ids, 3: 7] = quat_from_euler_xyz(rand_rpy[:, 0], rand_rpy[:, 1], rand_rpy[:, 2])  #!!!changed to +=
        # base velocities
        self.root_states[env_ids, 7:13] = torch_rand_float(-0.1, 0.1, (len(env_ids), 6), device=self.device)  # [7:10]: lin vel, [10:13]: ang vel
        env_ids_int32 = env_ids.to(dtype=torch.int32)
        self.gym.set_actor_root_state_tensor_indexed(self.sim,
                                                     gymtorch.unwrap_tensor(self.root_states),
                                                     gymtorch.unwrap_tensor(env_ids_int32), len(env_ids_int32))
        
    
    def _recompute_ang_vel(self):
        # self.commands[::2, 3] = self.episode_length_buf[::2] / 500 * np.pi
        # self.commands[1::2, 3] = -self.episode_length_buf[1::2] / 500 * np.pi
        heading = self._get_cur_heading()
        self.commands[:, 2] = torch.clip(
            0.5*wrap_to_pi(self.commands[:, 3] - heading), -self.cfg.commands.clip_ang_vel, self.cfg.commands.clip_ang_vel
        ) * (0.5 * np.pi / self.cfg.commands.clip_ang_vel)
    
    def _reward_lift_up(self):
        root_height = self.root_states[:, 2]
        root_height -= torch.mean(self._get_heights_at_points(self.foot_positions[:, -2:, :2]), dim = 1)
        delta_height = root_height - self.cfg.rewards.liftup_target
        error = torch.square(delta_height)
        reward = torch.exp(- error / self.cfg.rewards.tracking_liftup_sigma) #use tracking sigma
        return reward

    def _reward_lift_up_linear(self):
        root_height = self.root_states[:, 2]
        root_height -= torch.mean(self._get_heights_at_points(self.foot_positions[:, -2:, :2]), dim = 1)
        reward = (root_height - self.cfg.rewards.lift_up_threshold[0]) / (self.cfg.rewards.lift_up_threshold[1] - self.cfg.rewards.lift_up_threshold[0])
        reward = torch.clamp(reward, 0., 1.)
        return reward
    
    def _reward_tracking_lin_vel(self):
        if not self.cfg.env.vel_cmd:
            self.commands[:, :2] = 0.
        actual_lin_vel = quat_apply_yaw_inverse(self.base_quat, self.root_states[:, 7:10])
        lin_vel_error = torch.sum(torch.square(self.commands[:, :2] - actual_lin_vel[:, :2]), dim=1)
        reward = torch.exp(-lin_vel_error/self.cfg.rewards.tracking_sigma)
        forward = quat_apply(self.base_quat, self.forward_vec)
        upright_vec = quat_apply_yaw(self.base_quat, self.upright_vec)
        is_stand = (torch.sum(forward * upright_vec, dim=-1) / torch.norm(upright_vec, dim=-1)) > 0.9

        scale_factor_low = self.cfg.rewards.scale_factor_low
        scale_factor_high = self.cfg.rewards.scale_factor_high
        scaling_factor = (torch.clip(
            self.root_states[:, 2] - torch.mean(self._get_heights_at_points(self.foot_positions[:, -2:, :2]), dim = 1), min=scale_factor_low, max=scale_factor_high
        ) - scale_factor_low) / (scale_factor_high - scale_factor_low)
        reward = reward * is_stand.float() * scaling_factor
        return reward
    
    def _reward_tracking_ang_vel(self):
        if not self.cfg.env.vel_cmd:
            self.commands[:, 3] = 0.
        heading = self._get_cur_heading()
        if self.cfg.rewards.ang_rew_mode == "heading":
            # old
            heading_error = torch.square(wrap_to_pi(self.commands[:, 3] - heading) / np.pi)
            # new head error
            # heading_error = torch.square(torch.clip(0.5 * wrap_to_pi(self.commands[:, 3] - heading), -1, 1))
            reward = torch.exp(-heading_error / self.cfg.rewards.tracking_sigma)
        elif self.cfg.rewards.ang_rew_mode == "heading_with_pen":
            heading_error = torch.square(wrap_to_pi(self.commands[:, 3] - heading) / np.pi)
            reward = torch.exp(-heading_error / self.cfg.rewards.tracking_sigma)
            est_ang_vel = wrap_to_pi(heading - self.last_heading) / 0.02
            penalty = (torch.abs(est_ang_vel) - 1.0).clamp(min=0)
            reward = reward - 0.1 * penalty
        else:
            # new, trying
            est_ang_vel = wrap_to_pi(heading - self.last_heading) / 0.02
            # ang_vel_error = torch.abs(self.commands[:, 2] - est_ang_vel) / torch.abs(self.commands[:, 2]).clamp(min=1e-6)
            ang_vel_error = torch.abs(self.commands[:, 2] - est_ang_vel)
            reward = torch.exp(-ang_vel_error/self.cfg.rewards.tracking_ang_sigma)
        forward = quat_apply(self.base_quat, self.forward_vec)
        upright_vec = quat_apply_yaw(self.base_quat, self.upright_vec)
        is_stand = (torch.sum(forward * upright_vec, dim=-1) / torch.norm(upright_vec, dim=-1)) > 0.9
        scale_factor_low = self.cfg.rewards.scale_factor_low
        scale_factor_high = self.cfg.rewards.scale_factor_high
        scaling_factor = (torch.clip(
            self.root_states[:, 2] - torch.mean(self._get_heights_at_points(self.foot_positions[:, -2:, :2]), dim = 1), min=scale_factor_low, max=scale_factor_high
        ) - scale_factor_low) / (scale_factor_high - scale_factor_low)
        reward = reward * is_stand.float() * scaling_factor
        return reward
    
    def _reward_feet_clearance_cmd_linear(self):
        phases = 1 - torch.abs(1.0 - torch.clip((self.foot_indices[:, -2:] * 2.0) - 1.0, 0.0, 1.0) * 2.0)
        foot_height = (self.foot_positions[:, -2:, 2]).view(self.num_envs, -1)# - reference_heights
        terrain_at_foot_height = self._get_heights_at_points(self.foot_positions[:, -2:, :2])
        target_height = self.cfg.rewards.foot_target * phases + terrain_at_foot_height + 0.02 
        rew_foot_clearance = torch.square(target_height - foot_height) * (1 - self.desired_contact_states[:, -2:])
        condition = self.episode_length_buf > self.cfg.rewards.allow_contact_steps
        rew_foot_clearance = rew_foot_clearance * condition.unsqueeze(dim=-1).float()
        return torch.sum(rew_foot_clearance, dim=1)

    def _reward_rear_air(self):
        contact = self.contact_forces[:, self.feet_indices[-2:], 2] < 1.
        calf_contact = self.contact_forces[:, self.calf_indices[-2:], 2] < 1.
        unhealthy_condition = torch.logical_and(~calf_contact, contact)
        reward = torch.all(contact, dim=1).float() + unhealthy_condition.sum(dim=-1).float()
        return reward
    
    def _reward_stand_air(self):
        stand_air_condition = torch.logical_and(
            torch.logical_and(
                self.episode_length_buf < self.cfg.rewards.allow_contact_steps,
                quat_apply(self.base_quat, self.forward_vec)[:, 2] < 0.9
            ), torch.any((self.foot_positions[:, -2:, 2] - self._get_heights_at_points(self.foot_positions[:, -2:, :2])) > 0.03, dim=1)
        )
        return stand_air_condition.float()
    
    def _reward_foot_twist(self):
        vxy = torch.norm(self.foot_velocities[:, :, :2], dim=-1)
        vang = torch.norm(self.foot_velocities_ang, dim=-1)
        condition = (self.foot_positions[:, :, 2] - self._get_heights_at_points(self.foot_positions[:, :, :2])) < 0.025
        reward = torch.mean((vxy + 0.1 * vang) * condition.float(), dim=1)
        return reward
    
    def _reward_feet_slip(self):
        condition = (self.foot_positions[:, :, 2] - self._get_heights_at_points(self.foot_positions[:, :, :2])) < 0.03
        # xy lin vel
        foot_velocities = torch.square(torch.norm(self.foot_velocities[:, :, 0:2], dim=2).view(self.num_envs, -1))
        # yaw ang vel
        foot_ang_velocities = torch.square(torch.norm(self.foot_velocities_ang[:, :, 2:] / np.pi, dim=2).view(self.num_envs, -1))
        rew_slip = torch.sum(condition.float() * (foot_velocities + foot_ang_velocities), dim=1)
        return rew_slip

    def _reward_foot_shift(self):
        desired_foot_positions = torch.clone(self.init_feet_positions[:, 2:])
        desired_foot_positions[:, :, 2] = 0.02
        desired_foot_positions[:, :, 2] += self._get_heights_at_points(self.foot_positions[:, -2:, :2])
        rear_foot_shift = torch.norm(self.foot_positions[:, 2:] - desired_foot_positions, dim=-1).mean(dim=1)
        init_ffoot_positions = torch.clone(self.init_feet_positions[:, :2])
        front_foot_shift = torch.norm( torch.stack([
            (init_ffoot_positions[:, :, 0] - self.foot_positions[:, :2, 0]).clamp(min=0), 
            torch.abs(init_ffoot_positions[:, :, 1] - self.foot_positions[:, :2, 1])
        ], dim=-1), dim=-1).mean(dim=1)
        condition = self.episode_length_buf < self.cfg.rewards.allow_contact_steps
        reward = (front_foot_shift + rear_foot_shift) * condition.float()
        return reward
    
    def _reward_front_contact_force(self):
        force = torch.norm(self.contact_forces[:, self.termination_contact_indices[5: 7]], dim=-1).mean(dim=1)
        reward = force
        return reward
    
    def _reward_hip_still(self):
        movement = torch.abs(self.dof_pos.view(self.num_envs, 4, 3)[:, :, 0] - 0.).mean(dim=1)
        condition = self.episode_length_buf < self.cfg.rewards.allow_contact_steps
        reward = movement * condition.float()
        return reward

    # def _reward_not_too_upright(self):
    #     forward = quat_apply(self.base_quat, self.forward_vec)
    #     vertical = to_torch([0, 0, 1.0], device=self.device).repeat((self.num_envs, 1))
    #     cosine_dist = torch.sum(forward * vertical, dim=-1)
    #     cond = torch.acos(cosine_dist) < self.cfg.rewards.too_upright_threshold
    #     condition = self.episode_length_buf < self.cfg.rewards.before_tracking_command_steps
    #     reward = cond.float() * 1 * condition.float()
    #     return reward

    def _reward_evaluate_metrics(self):
        # evaluate heading tracking and cost of transport
        metrics = {}
        # heading tracking
        mask = self.episode_length_buf > self.cfg.rewards.allow_contact_steps

        heading = self._get_cur_heading()
        heading_error = torch.abs(wrap_to_pi(self.commands[:, 3] - heading))
        # heading_error = torch.abs(self.commands[:, 2] - self.root_states[:,12])
        # print(heading, heading_error)
        heading_error[~mask] = 0.
        metrics["tracking_ang_vel"] = heading_error
        # power consumption
        power = self.power_sum.clone()
        metrics["energy"] = power
        self.power_sum[:] = 0.
        power[~mask] = 0.

        metrics["energy"] = power

        # base ang vel in yaw
        base_ang_vel = torch.abs(self.base_ang_vel[:,2])
        base_ang_vel[~mask] = 0.
        metrics["base_ang_vel"] = base_ang_vel
        # record time
        metrics["time"] = torch.zeros((self.num_envs, ), dtype=torch.float, device=self.device) 
        metrics["time"][mask] += self.dt

        self.metrics = metrics
        


        return torch.zeros((self.num_envs, ), dtype=torch.float, device=self.device)
