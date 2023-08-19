from pdb import set_trace as T
import numpy as np

from abc import ABC, abstractmethod

import torch
import torch.nn as nn

import pufferlib.pytorch


class Policy(torch.nn.Module, ABC):
    '''Pure PyTorch base policy
    
    This spec allows PufferLib to repackage your policy
    for compatibility with RL frameworks

    encode_observations -> decode_actions is PufferLib's equivalent of PyTorch forward
    This structure provides additional flexibility for us to include an LSTM
    between the encoder and decoder.

    To port a policy to PufferLib, simply put everything from forward() before the
    recurrent cell (or, if no recurrent cell, everything before the action head)
    into encode_observations and put everything after into decode_actions.

    You can delete the recurrent cell from forward(). PufferLib handles this for you
    with its framework-specific wrappers. Since each frameworks treats temporal data a bit
    differently, this approach lets you write a single PyTorch network for multiple frameworks.

    Specify the value function in critic(). This is a single value for each batch element.
    It is called on the output of the recurrent cell (or, if no recurrent cell,
    the output of encode_observations)
    '''

    @abstractmethod
    def encode_observations(self, flat_observations):
        '''Encodes a batch of observations into hidden states

        Call pufferlib.emulation.unpack_batched_obs at the start of this
        function to unflatten observations to their original structured form:

        observations = pufferlib.emulation.unpack_batched_obs(
            self.envs.structured_observation_space, env_outputs)
 
        Args:
            flat_observations: A tensor of shape (batch, ..., obs_size)

        Returns:
            hidden: Tensor of (batch, ..., hidden_size)
            lookup: Tensor of (batch, ...) that can be used to return additional embeddings
        '''
        raise NotImplementedError

    @abstractmethod
    def decode_actions(self, flat_hidden, lookup):
        '''Decodes a batch of hidden states into multidiscrete actions

        Args:
            flat_hidden: Tensor of (batch, ..., hidden_size)
            lookup: Tensor of (batch, ...), if returned by encode_observations

        Returns:
            actions: Tensor of (batch, ..., action_size)
            value: Tensor of (batch, ...)

        actions is a concatenated tensor of logits for each action space dimension.
        It should be of shape (batch, ..., sum(action_space.nvec))'''
        raise NotImplementedError

    def forward(self, env_outputs):
        '''Forward pass for PufferLib compatibility'''
        hidden, lookup = self.encode_observations(env_outputs)
        actions, value = self.decode_actions(hidden, lookup)
        return actions, value


class RecurrentWrapper(torch.nn.Module):
    def __init__(self, envs, policy, input_size=128, hidden_size=128, num_layers=1):
        super().__init__()

        if not isinstance(policy, Policy):
            raise ValueError('Subclass pufferlib.Policy to use RecurrentWrapper')

        self.single_observation_shape = envs.single_observation_space.shape
        self.policy = policy
        self.input_size = input_size
        self.hidden_size = hidden_size

        self.recurrent = torch.nn.LSTM(
            input_size, hidden_size, num_layers=num_layers)

    def forward(self, x, state):
        shape, tensor_shape = x.shape, self.single_observation_shape
        x_dims, tensor_dims = len(shape), len(tensor_shape)
        if x_dims == tensor_dims + 1:
            TT = 1
        elif x_dims == tensor_dims + 2:
            TT = shape[1]
        else:
            raise ValueError('Invalid tensor shape', shape)

        B = shape[0]
        x = x.reshape(B*TT, *tensor_shape)
        hidden, lookup = self.policy.encode_observations(x)
        assert hidden.shape == (B*TT, self.input_size)

        hidden = hidden.view(TT, B, self.input_size)
        hidden, state = self.recurrent(hidden, state)
        hidden = hidden.reshape(B*TT, self.hidden_size)

        hidden, critic = self.policy.decode_actions(hidden, lookup)
        return hidden, critic, state


class Default(Policy):
    def __init__(self, envs, input_size=128, hidden_size=128):
        '''Default PyTorch policy, meant for debugging.
        This should run with any environment but is unlikely to learn anything.
        
        Uses a single linear layer to encode observations and a list of
        linear layers to decode actions. The value function is a single linear layer.
        '''
        super().__init__()
        self.encoder = nn.Linear(np.prod(envs.single_observation_space.shape), hidden_size)
        self.decoders = nn.ModuleList([nn.Linear(hidden_size, n)
                for n in envs.single_action_space.nvec])
        self.value_head = nn.Linear(hidden_size, 1)

    def forward(self, env_outputs):
        '''Forward pass for PufferLib compatibility'''
        hidden, lookup = self.encode_observations(env_outputs)
        actions, value = self.decode_actions(hidden, lookup)
        return actions, value

    def encode_observations(self, env_outputs):
        '''Linear encoder function'''
        env_outputs = env_outputs.reshape(env_outputs.shape[0], -1)
        return self.encoder(env_outputs), None

    def decode_actions(self, hidden, lookup, concat=True):
        '''Concatenated linear decoder function'''
        actions = [dec(hidden) for dec in self.decoders]
        value = self.value_head(hidden)
        return actions, value

class Convolutional(Policy):
    def __init__(self, envs, *args, framestack, flat_size,
            input_size=512, hidden_size=512, output_size=512,
            channels_last=False, downsample=1, **kwargs):
        '''The CleanRL default Atari policy: a stack of three convolutions followed by a linear layer
        
        Takes framestack as a mandatory keyword arguments. Suggested default is 1 frame
        with LSTM or 4 frames without.'''
        super().__init__()
        self.observation_space = envs.single_observation_space
        self.num_actions = envs.single_action_space.nvec[0]
        self.channels_last = channels_last
        self.downsample = downsample

        self.network = nn.Sequential(
            pufferlib.pytorch.layer_init(nn.Conv2d(framestack, 32, 8, stride=4)),
            nn.ReLU(),
            pufferlib.pytorch.layer_init(nn.Conv2d(32, 64, 4, stride=2)),
            nn.ReLU(),
            pufferlib.pytorch.layer_init(nn.Conv2d(64, 64, 3, stride=1)),
            nn.ReLU(),
            nn.Flatten(),
            pufferlib.pytorch.layer_init(nn.Linear(flat_size, hidden_size)),
            nn.ReLU(),
        )

        self.actor = pufferlib.pytorch.layer_init(nn.Linear(output_size, self.num_actions), std=0.01)
        self.value_function = pufferlib.pytorch.layer_init(nn.Linear(output_size, 1), std=1)

    def encode_observations(self, observations):
        if self.channels_last:
            observations = observations.permute(0, 3, 1, 2)
        if self.downsample > 1:
            observations = observations[:, :, ::self.downsample, ::self.downsample]
        return self.network(observations / 255.0), None

    def decode_actions(self, flat_hidden, lookup, concat=None):
        action = self.actor(flat_hidden)
        value = self.value_function(flat_hidden)
        return action, value