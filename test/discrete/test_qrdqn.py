import os
import gym
import torch
import pickle
import pprint
import argparse
import numpy as np
from torch.utils.tensorboard import SummaryWriter

from tianshou.utils import TensorboardLogger
from tianshou.policy import QRDQNPolicy
from tianshou.env import DummyVectorEnv
from tianshou.utils.net.common import Net
from tianshou.trainer import offpolicy_trainer
from tianshou.data import Collector, VectorReplayBuffer, PrioritizedVectorReplayBuffer


def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--task', type=str, default='CartPole-v0')
    parser.add_argument('--seed', type=int, default=1)
    parser.add_argument('--eps-test', type=float, default=0.05)
    parser.add_argument('--eps-train', type=float, default=0.1)
    parser.add_argument('--buffer-size', type=int, default=20000)
    parser.add_argument('--lr', type=float, default=1e-3)
    parser.add_argument('--gamma', type=float, default=0.9)
    parser.add_argument('--num-quantiles', type=int, default=200)
    parser.add_argument('--n-step', type=int, default=3)
    parser.add_argument('--target-update-freq', type=int, default=320)
    parser.add_argument('--epoch', type=int, default=10)
    parser.add_argument('--step-per-epoch', type=int, default=10000)
    parser.add_argument('--step-per-collect', type=int, default=10)
    parser.add_argument('--update-per-step', type=float, default=0.1)
    parser.add_argument('--batch-size', type=int, default=64)
    parser.add_argument('--hidden-sizes', type=int,
                        nargs='*', default=[128, 128, 128, 128])
    parser.add_argument('--training-num', type=int, default=10)
    parser.add_argument('--test-num', type=int, default=100)
    parser.add_argument('--logdir', type=str, default='log')
    parser.add_argument('--render', type=float, default=0.)
    parser.add_argument('--prioritized-replay',
                        action="store_true", default=False)
    parser.add_argument('--alpha', type=float, default=0.6)
    parser.add_argument('--beta', type=float, default=0.4)
    parser.add_argument(
        '--save-buffer-name', type=str,
        default="./expert_QRDQN_CartPole-v0.pkl")
    parser.add_argument(
        '--device', type=str,
        default='cuda' if torch.cuda.is_available() else 'cpu')
    args = parser.parse_known_args()[0]
    return args


def test_qrdqn(args=get_args()):
    env = gym.make(args.task)
    if args.task == 'CartPole-v0':
        env.spec.reward_threshold = 190  # lower the goal
    args.state_shape = env.observation_space.shape or env.observation_space.n
    args.action_shape = env.action_space.shape or env.action_space.n
    # train_envs = gym.make(args.task)
    # you can also use tianshou.env.SubprocVectorEnv
    train_envs = DummyVectorEnv(
        [lambda: gym.make(args.task) for _ in range(args.training_num)])
    # test_envs = gym.make(args.task)
    test_envs = DummyVectorEnv(
        [lambda: gym.make(args.task) for _ in range(args.test_num)])
    # seed
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    train_envs.seed(args.seed)
    test_envs.seed(args.seed)
    # model
    net = Net(args.state_shape, args.action_shape,
              hidden_sizes=args.hidden_sizes, device=args.device,
              softmax=False, num_atoms=args.num_quantiles)
    optim = torch.optim.Adam(net.parameters(), lr=args.lr)
    policy = QRDQNPolicy(
        net, optim, args.gamma, args.num_quantiles,
        args.n_step, target_update_freq=args.target_update_freq
    ).to(args.device)
    # buffer
    if args.prioritized_replay:
        buf = PrioritizedVectorReplayBuffer(
            args.buffer_size, buffer_num=len(train_envs),
            alpha=args.alpha, beta=args.beta)
    else:
        buf = VectorReplayBuffer(args.buffer_size, buffer_num=len(train_envs))
    # collector
    train_collector = Collector(policy, train_envs, buf, exploration_noise=True)
    test_collector = Collector(policy, test_envs, exploration_noise=True)
    # policy.set_eps(1)
    train_collector.collect(n_step=args.batch_size * args.training_num)
    # log
    log_path = os.path.join(args.logdir, args.task, 'qrdqn')
    writer = SummaryWriter(log_path)
    logger = TensorboardLogger(writer)

    def save_fn(policy):
        torch.save(policy.state_dict(), os.path.join(log_path, 'policy.pth'))

    def stop_fn(mean_rewards):
        return mean_rewards >= env.spec.reward_threshold

    def train_fn(epoch, env_step):
        # eps annnealing, just a demo
        if env_step <= 10000:
            policy.set_eps(args.eps_train)
        elif env_step <= 50000:
            eps = args.eps_train - (env_step - 10000) / \
                40000 * (0.9 * args.eps_train)
            policy.set_eps(eps)
        else:
            policy.set_eps(0.1 * args.eps_train)

    def test_fn(epoch, env_step):
        policy.set_eps(args.eps_test)

    # trainer
    result = offpolicy_trainer(
        policy, train_collector, test_collector, args.epoch,
        args.step_per_epoch, args.step_per_collect, args.test_num,
        args.batch_size, train_fn=train_fn, test_fn=test_fn,
        stop_fn=stop_fn, save_fn=save_fn, logger=logger,
        update_per_step=args.update_per_step)
    assert stop_fn(result['best_reward'])

    if __name__ == '__main__':
        pprint.pprint(result)
        # Let's watch its performance!
        env = gym.make(args.task)
        policy.eval()
        policy.set_eps(args.eps_test)
        collector = Collector(policy, env)
        result = collector.collect(n_episode=1, render=args.render)
        rews, lens = result["rews"], result["lens"]
        print(f"Final reward: {rews.mean()}, length: {lens.mean()}")

    # save buffer in pickle format, for imitation learning unittest
    buf = VectorReplayBuffer(args.buffer_size, buffer_num=len(test_envs))
    policy.set_eps(0.9)  # 10% of expert data as demonstrated in the original paper
    collector = Collector(policy, test_envs, buf, exploration_noise=True)
    result = collector.collect(n_step=args.buffer_size)
    pickle.dump(buf, open(args.save_buffer_name, "wb"))
    print(result["rews"].mean())


def test_pqrdqn(args=get_args()):
    args.prioritized_replay = True
    args.gamma = .95
    test_qrdqn(args)


if __name__ == '__main__':
    test_pqrdqn(get_args())
