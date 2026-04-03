import torch
import numpy as np
from torch.utils.data import Dataset, DataLoader


class BackwaterCoupledRiver1D(Dataset):
    """
    1D Coupled River Dataset with Dynamic Backwater Effects.
    Features: [eta, h, u, x_norm, Q, cover]
    Implements an iterative GVF solver: dH/dx = (S0 - Sf) / (1 - Fr^2)
    """

    def __init__(self, min_nodes=64, max_nodes=256, n_steps=50, num_samples=1000, dt=0.01):
        self.min_nodes = min_nodes
        self.max_nodes = max_nodes
        self.n_steps = n_steps
        self.num_samples = num_samples
        self.dt = dt
        self.g = 9.81
        self.n_manning = 0.035  # Manning's n

    def solve_backwater(self, x, eta, Q, h_downstream):
        """
        Standard step-style iterative solver for GVF.
        Calculates water surface elevation H = eta + h.
        """
        n = len(x)
        h = np.zeros(n)
        h[-1] = h_downstream
        dx = x[1] - x[0]

        # Solving from downstream to upstream (Subcritical flow)
        for i in range(n - 2, -1, -1):
            # Iterative solution for h[i] at node i
            h_guess = h[i + 1]
            for _ in range(5):
                u = Q / h_guess
                Sf = (self.n_manning**2 * u**2) / (h_guess ** (4 / 3))
                # Energy balance: H_i = H_i+1 + Sf*dx
                H_next = eta[i + 1] + h[i + 1]
                h_guess = (H_next + Sf * dx) - eta[i]
                h_guess = max(h_guess, 0.1)
            h[i] = h_guess

        u = Q / h
        return h, u

    def generate_sample(self):
        n_nodes = np.random.randint(self.min_nodes, self.max_nodes + 1)
        length = 100.0
        x = np.linspace(0, length, n_nodes)
        dx = length / n_nodes

        # Bedrock slope + some dunes
        beta = 5.0 - 0.02 * x
        M = np.zeros(n_nodes)
        # Create a large 'downstream' dune to trigger backwater
        dune_center = np.random.uniform(60, 80)
        M += 2.0 * np.exp(-((x - dune_center) ** 2) / (2 * 5**2))
        eta = beta + M

        Q_series = np.full(self.n_steps, 15.0)  # Start steady
        # Pulse in the middle
        Q_series[20:40] = 30.0

        k_transport = 0.01
        history = []
        qs_history = []

        for t_idx in range(self.n_steps):
            Q = Q_series[t_idx]
            # Downstream boundary: normal depth for Q
            h_down = ((Q * self.n_manning) / np.sqrt(0.02)) ** 0.6
            h, u = self.solve_backwater(x, eta, Q, h_down)

            qs = k_transport * (u**3)
            dq_dx = np.zeros_like(qs)
            dq_dx[1:] = (qs[1:] - qs[:-1]) / dx
            dq_dx[0] = dq_dx[1]

            # Simple Exner update
            eta_next = eta - self.dt * dq_dx

            # Normalization
            Q_node = np.full((n_nodes,), Q)
            cover = np.clip(M / 0.5, 0, 1)
            state = np.stack(
                [eta / 10.0, h / 10.0, u / 10.0, x / length, Q_node / 40.0, cover], axis=-1
            )

            history.append(state.copy())
            qs_history.append(qs / 20.0)

            eta = eta_next
            M = np.maximum(eta - beta, 0)

        return np.array(history), np.array(qs_history)

    def __len__(self):
        return self.num_samples

    def __getitem__(self, idx):
        data, qs = self.generate_sample()
        inp = torch.tensor(data[:10], dtype=torch.float32)
        next_state = data[1:11]
        qs_next = qs[:10, :, np.newaxis]
        # Target: [eta, h, u, x, Q, cover, qs]
        tgt = torch.tensor(np.concatenate([next_state, qs_next], axis=-1), dtype=torch.float32)
        return inp, tgt


def collate_backwater(batch):
    max_res = max([item[0].shape[1] for item in batch])
    batch_size = len(batch)
    T = batch[0][0].shape[0]
    padded_inp = torch.zeros(batch_size, T, max_res, 6)
    padded_tgt = torch.zeros(batch_size, T, max_res, 7)
    mask = torch.zeros(batch_size, T, max_res)
    for i, (inp, tgt) in enumerate(batch):
        res = inp.shape[1]
        padded_inp[i, :, :res, :] = inp
        padded_tgt[i, :, :res, :] = tgt
        mask[i, :, :res] = 1.0
    return padded_inp, padded_tgt, mask


def get_backwater_dataloader(batch_size=8):
    ds = BackwaterCoupledRiver1D()
    return DataLoader(ds, batch_size=batch_size, shuffle=True, collate_fn=collate_backwater)
