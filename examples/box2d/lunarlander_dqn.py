import os
import gym
import torch
import pprint
import argparse
import numpy as np
from torch.utils.tensorboard import SummaryWriter

from tianshou.policy import DQNPolicy
from tianshou.utils import TensorboardLogger
from tianshou.utils.net.common import Net
from tianshou.trainer import offpolicy_trainer
from tianshou.data import Collector, VectorReplayBuffer
from tianshou.env import DummyVectorEnv, SubprocVectorEnv


def get_args():
    parser = argparse.ArgumentParser()
    # the parameters are found by Optuna
    parser.add_argument('--task', type=str, default='LunarLander-v2')
    parser.add_argument('--seed', type=int, default=0)
    parser.add_argument('--eps-test', type=float, default=0.01)
    parser.add_argument('--eps-train', type=float, default=0.73)
    parser.add_argument('--buffer-size', type=int, default=100000)
    parser.add_argument('--lr', type=float, default=0.013)
    parser.add_argument('--gamma', type=float, default=0.99)
    parser.add_argument('--n-step', type=int, default=4)
    parser.add_argument('--target-update-freq', type=int, default=500)
    parser.add_argument('--epoch', type=int, default=10)
    parser.add_argument('--step-per-epoch', type=int, default=80000)
    parser.add_argument('--step-per-collect', type=int, default=16)
    parser.add_argument('--update-per-step', type=float, default=0.0625)
    parser.add_argument('--batch-size', type=int, default=128)
    parser.add_argument('--hidden-sizes', type=int,
                        nargs='*', default=[128, 128])
    parser.add_argument('--dueling-q-hidden-sizes', type=int,
                        nargs='*', default=[128, 128])
    parser.add_argument('--dueling-v-hidden-sizes', type=int,
                        nargs='*', default=[128, 128])
    parser.add_argument('--training-num', type=int, default=16)
    parser.add_argument('--test-num', type=int, default=100)
    parser.add_argument('--logdir', type=str, default='log')
    parser.add_argument('--render', type=float, default=0.)
    parser.add_argument(
        '--device', type=str,
        default='cuda' if torch.cuda.is_available() else 'cpu')
    return parser.parse_args()


def test_dqn(args=get_args()):
    env = gym.make(args.task)
    args.state_shape = env.observation_space.shape or env.observation_space.n
    args.action_shape = env.action_space.shape or env.action_space.n
    # train_envs = gym.make(args.task)
    # you can also use tianshou.env.SubprocVectorEnv
    train_envs = DummyVectorEnv(
        [lambda: gym.make(args.task) for _ in range(args.training_num)])
    # test_envs = gym.make(args.task)
    test_envs = SubprocVectorEnv(
        [lambda: gym.make(args.task) for _ in range(args.test_num)])
    # seed
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    train_envs.seed(args.seed)
    test_envs.seed(args.seed)
    # model
    Q_param = {"hidden_sizes": args.dueling_q_hidden_sizes}
    V_param = {"hidden_sizes": args.dueling_v_hidden_sizes}
    net = Net(args.state_shape, args.action_shape,
              hidden_sizes=args.hidden_sizes, device=args.device,
              dueling_param=(Q_param, V_param)).to(args.device)
    optim = torch.optim.Adam(net.parameters(), lr=args.lr)
    policy = DQNPolicy(
        net, optim, args.gamma, args.n_step,
        target_update_freq=args.target_update_freq)
    # collector
    train_collector = Collector(
        policy, train_envs,
        VectorReplayBuffer(args.buffer_size, len(train_envs)),
        exploration_noise=True)
    test_collector = Collector(policy, test_envs, exploration_noise=True)
    # policy.set_eps(1)
    train_collector.collect(n_step=args.batch_size * args.training_num)
    # log
    log_path = os.path.join(args.logdir, args.task, 'dqn')
    writer = SummaryWriter(log_path)
    logger = TensorboardLogger(writer)

    def save_fn(policy):
        torch.save(policy.state_dict(), os.path.join(log_path, 'policy.pth'))

    def stop_fn(mean_rewards):
        return mean_rewards >= env.spec.reward_threshold

    def train_fn(epoch, env_step):  # exp decay
        eps = max(args.eps_train * (1 - 5e-6) ** env_step, args.eps_test)
        policy.set_eps(eps)

    def test_fn(epoch, env_step):
        policy.set_eps(args.eps_test)

    # trainer
    result = offpolicy_trainer(
        policy, train_collector, test_collector, args.epoch,
        args.step_per_epoch, args.step_per_collect, args.test_num, args.batch_size,
        update_per_step=args.update_per_step, stop_fn=stop_fn, train_fn=train_fn,
        test_fn=test_fn, save_fn=save_fn, logger=logger)

    assert stop_fn(result['best_reward'])
    if __name__ == '__main__':
        pprint.pprint(result)
        # Let's watch its performance!
        policy.eval()
        policy.set_eps(args.eps_test)
        test_envs.seed(args.seed)
        test_collector.reset()
        result = test_collector.collect(n_episode=args.test_num, render=args.render)
        rews, lens = result["rews"], result["lens"]
        print(f"Final reward: {rews.mean()}, length: {lens.mean()}")


if __name__ == '__main__':
    test_dqn(get_args())
