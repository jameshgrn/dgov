import torch
import torch.nn as nn
from .transformer import Block


class FlowAwareExchangeOperator(nn.Module):
    def __init__(self, hidden_dim, causal=True):
        super().__init__()
        self.hidden_dim = hidden_dim
        self.causal = causal
        self.transport_kernel = nn.Sequential(
            nn.Linear(hidden_dim * 2 + 1, hidden_dim), nn.GELU(), nn.Linear(hidden_dim, hidden_dim)
        )
        self.info_kernel = nn.Sequential(
            nn.Linear(hidden_dim * 2 + 1, hidden_dim // 4),
            nn.GELU(),
            nn.Linear(hidden_dim // 4, hidden_dim),
        )
        self.alpha = nn.Parameter(torch.tensor(0.1))

    def forward(self, h: torch.Tensor, u: torch.Tensor) -> torch.Tensor:
        B, N, D = h.shape
        h_prev = torch.roll(h, shifts=1, dims=1)
        h_next = torch.roll(h, shifts=-1, dims=1)
        u_node = u.view(B * h.shape[0] // B, N, 1)  # Support flattened time
        m_upwind = self.transport_kernel(torch.cat([h_prev, h, u_node], dim=-1))
        m_info = self.info_kernel(torch.cat([h_next, h, u_node], dim=-1))
        m_info = torch.clamp(self.alpha, 0.05, 0.3) * m_info
        if self.causal:
            return h + m_upwind
        else:
            return h + m_upwind + m_info


class NMO_V3(nn.Module):
    """
    Neural Morphodynamic Operator (V3).
    Supports Mixed-Regime Dynamics: Incision + Transport + Cover.
    """

    def __init__(self, n_embd=128, n_head=4, n_layer=4, dropout=0.0, dt=0.01, causal=True):
        super().__init__()
        self.dt = dt
        # Input features: [eta, h, u, x, Q, cover] (6)
        self.node_proj = nn.Linear(6, n_embd)
        self.time_emb = nn.Parameter(torch.zeros(1, 100, 1, n_embd))

        self.exchange_layers = nn.ModuleList(
            [FlowAwareExchangeOperator(n_embd, causal=causal) for _ in range(n_layer)]
        )
        self.blocks = nn.ModuleList(
            [Block(n_embd, n_head, 2048, dropout, "layer", causal=causal) for _ in range(n_layer)]
        )

        self.ln_f = nn.LayerNorm(n_embd)

        # Multi-Head Outputs:
        # 1. Flux (qs)
        # 2. Incision (E)
        # 3. Hydro (h_next, u_next)
        # 4. Storage (cover_next)
        self.output_proj = nn.Linear(n_embd, 5)

        # Diagnostic Regime Head
        self.regime_head = nn.Linear(n_embd, 3)  # [Transport, Incision, Storage]

    def forward(self, x, targets=None, mask=None):
        # x: [B, T, N, 6]
        B, T, N, _ = x.size()
        eta_now = x[:, :, :, 0:1]
        u_now = x[:, :, :, 2:3]
        x_norm = x[:, :, :, 3:4]
        Q_now = x[:, :, :, 4:5]

        feat = self.node_proj(x) + self.time_emb[:, :T, :, :]
        feat = feat.view(B * T, N, -1)
        u_flat = u_now.view(B * T, N, 1)

        for exch, block in zip(self.exchange_layers, self.blocks):
            feat = exch(feat, u_flat)
            feat = block(feat)

        feat = self.ln_f(feat)
        out = self.output_proj(feat).view(B, T, N, 5)
        regime_logits = self.regime_head(feat).view(B, T, N, 3)

        qs_pred = out[:, :, :, 0:1]
        inc_pred = out[:, :, :, 1:2]
        h_next = out[:, :, :, 2:3]
        u_next = out[:, :, :, 3:4]
        cover_next = out[:, :, :, 4:5]

        # Structural Exner Update with Incision
        # Dimensionless scale back: qs_scale=20, eta_scale=5, inc_scale=0.1
        # eta_next = eta_now - dt * div(qs) - dt * incision
        dx = (x_norm[:, :, 1:2, :] - x_norm[:, :, 0:1, :]) * 100.0
        dqs = torch.zeros_like(qs_pred)
        dqs[:, :, 1:, :] = (qs_pred[:, :, 1:, :] - qs_pred[:, :, :-1, :]) / dx
        dqs[:, :, 0, :] = dqs[:, :, 1, :]

        # Update rule using normalized scales:
        # qs_pred is norm (scale 20), inc_pred is norm (scale 0.1)
        # eta_pred is norm (scale 5)
        # d_eta_norm = - (dt*0.1 * 20/5) * div(qs_norm) - (dt*0.1 * 0.1/5) * incision_norm
        eta_next = eta_now - (self.dt * 0.4) * dqs - (self.dt * 0.002) * inc_pred

        logits = torch.cat([eta_next, h_next, u_next, x_norm, Q_now, cover_next], dim=-1)

        loss = None
        if targets is not None:
            # targets: [B, T, N, 8] (state(6) + qs + inc)
            eta_tgt, h_tgt, u_tgt, cover_tgt = (
                targets[:, :, :, 0:1],
                targets[:, :, :, 1:2],
                targets[:, :, :, 2:3],
                targets[:, :, :, 5:6],
            )
            qs_tgt, inc_tgt = targets[:, :, :, 6:7], targets[:, :, :, 7:8]

            loss_eta = nn.functional.mse_loss(eta_next, eta_tgt)
            loss_qs = nn.functional.mse_loss(qs_pred, qs_tgt)
            loss_inc = nn.functional.mse_loss(inc_pred, inc_tgt)
            loss_hydro = nn.functional.mse_loss(h_next, h_tgt) + nn.functional.mse_loss(
                u_next, u_tgt
            )
            loss_cover = nn.functional.mse_loss(cover_next, cover_tgt)

            loss = loss_eta + 1.0 * loss_qs + 1.0 * loss_inc + 0.1 * loss_hydro + 0.1 * loss_cover

        return logits, loss, qs_pred, regime_logits
