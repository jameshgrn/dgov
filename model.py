"""Minimal transformer implementation with configurable architecture."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import torch
import torch.nn as nn
import torch.nn.functional as F


class NormalizationStrategy(str, Enum):
    """Normalization strategy choices."""
    LAYER_NORM = "layer_norm"
    RMS_NORM = "rms_norm"


class PositionalEncodingType(str, Enum):
    """Positional encoding type choices."""
    ABSOLUTE = "absolute"
    ROTARY = "rotary"
    ALIBI = "alibi"
    NONE = "none"


@dataclass
class TransformerConfig:
    """Configuration for TinyTransformer architecture."""

    vocab_size: int = 1024
    hidden_dim: int = 256
    num_layers: int = 4
    num_heads: int = 4
    ffn_hidden_ratio: float = 4.0
    tie_embeddings: bool = True
    normalization: NormalizationStrategy = NormalizationStrategy.LAYER_NORM
    positional_encoding: PositionalEncodingType = PositionalEncodingType.ABSOLUTE
    residual_scale: float = 1.0
    dropout: float = 0.0
    max_seq_len: int = 512

    def __post_init__(self):
        if self.num_heads <= 0:
            raise ValueError("num_heads must be > 0")
        if self.ffn_hidden_ratio <= 0:
            raise ValueError("ffn_hidden_ratio must be > 0")


class RMSNorm(nn.Module):
    """Root Mean Square Layer Normalization."""

    def __init__(self, dim: int, eps: float = 1e-6):
        super().__init__()
        self.eps = eps
        self.weight = nn.Parameter(torch.ones(dim))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return F.rms_norm(x, (self.weight.shape[0],), self.weight, self.eps)


class RotaryEmbedding(nn.Module):
    """Rotary positional embeddings."""

    def __init__(self, dim: int, max_seq_len: int = 512):
        super().__init__()
        self.dim = dim
        inv_freq = 1.0 / (10000 ** (torch.arange(0, dim, 2).float() / dim))
        t = torch.arange(max_seq_len, dtype=torch.float32)
        freqs = torch.einsum("i,j->ij", t, inv_freq)
        self.register_buffer("freqs_cos", freqs.cos())
        self.register_buffer("freqs_sin", freqs.sin())

    def forward(self, x: torch.Tensor, seq_pos=None):
        """x: [batch, seq_len, num_heads, head_dim]"""
        if seq_pos is None:
            seq_pos = torch.arange(x.shape[1], device=x.device)
        cos = self.freqs_cos[seq_pos].unsqueeze(0).unsqueeze(-2)
        sin = self.freqs_sin[seq_pos].unsqueeze(0).unsqueeze(-2)

        x1 = x[..., : self.dim // 2]
        x2 = x[..., self.dim // 2 :]
        return torch.cat([x1 * cos - x2 * sin, x2 * cos + x1 * sin], dim=-1)


class AlibiPositionalEncoding(nn.Module):
    """ALiBi positional bias."""

    def __init__(self, num_heads: int, max_seq_len: int = 512):
        super().__init__()
        self.num_heads = num_heads
        slopes = torch.tensor([2 ** (-2 ** -j) for j in range(num_heads)])
        self.register_buffer("slopes", slopes.unsqueeze(0).unsqueeze(-1))

    def forward(self, attn_weights: torch.Tensor, seq_pos=None):
        """Apply ALiBi bias to attention weights.
        
        Args:
            attn_weights: [batch, num_heads, seq_len, seq_len]
            seq_pos: Optional position indices (defaults to 0..seq_len-1)
        """
        if seq_pos is None:
            seq_pos = torch.arange(attn_weights.shape[-1], device=attn_weights.device)

        rel_pos = seq_pos.unsqueeze(0).unsqueeze(-2) - seq_pos.unsqueeze(-1).unsqueeze(0)
        bias = self.slopes * rel_pos.float()
        return attn_weights + bias


class Attention(nn.Module):
    """Multi-head attention with configurable positional encoding."""

    def __init__(self, config: TransformerConfig):
        super().__init__()
        assert config.hidden_dim % config.num_heads == 0, "hidden_dim must be divisible by num_heads"
        self.head_dim = config.hidden_dim // config.num_heads

        self.qkv_proj = nn.Linear(config.hidden_dim, 3 * config.hidden_dim)
        self.out_proj = nn.Linear(config.hidden_dim, config.hidden_dim)

        self.config = config
        if config.positional_encoding == PositionalEncodingType.ROTARY:
            self.rotary = RotaryEmbedding(self.head_dim, config.max_seq_len)
        elif config.positional_encoding == PositionalEncodingType.ALIBI:
            self.alibi = AlibiPositionalEncoding(config.num_heads, config.max_seq_len)

    def forward(self, x: torch.Tensor, seq_pos=None):
        batch, seq_len, _ = x.shape

        qkv = self.qkv_proj(x)
        qkv = qkv.view(batch, seq_len, self.config.num_heads, 3 * self.head_dim).transpose(1, 2)
        q, k, v = qkv.chunk(3, dim=-1)

        if self.config.positional_encoding == PositionalEncodingType.ROTARY:
            q = self.rotary(q, seq_pos)
            k = self.rotary(k, seq_pos)

        scores = torch.matmul(q, k.transpose(-2, -1)) / (self.head_dim ** 0.5)

        if self.config.positional_encoding == PositionalEncodingType.ALIBI:
            scores = self.alibi(scores, seq_pos)

        attn = F.softmax(scores.float(), dim=-1).type_as(scores)
        out = torch.matmul(attn, v)
        out = out.transpose(1, 2).contiguous().view(batch, seq_len, -1)

        return self.out_proj(out)


class MLP(nn.Module):
    """MLP block with configurable hidden size."""

    def __init__(self, config: TransformerConfig):
        super().__init__()
        hidden = int(config.hidden_dim * config.ffn_hidden_ratio)
        self.fc1 = nn.Linear(config.hidden_dim, hidden)
        self.fc2 = nn.Linear(hidden, config.hidden_dim)
        self.activation = F.gelu

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.fc2(self.activation(self.fc1(x)))


class TransformerBlock(nn.Module):
    """Single transformer block with configurable norm and residual scaling."""

    def __init__(self, config: TransformerConfig, layer_idx: int = 0):
        super().__init__()

        if config.normalization == NormalizationStrategy.LAYER_NORM:
            self.norm1 = nn.LayerNorm(config.hidden_dim, eps=1e-5)
            self.norm2 = nn.LayerNorm(config.hidden_dim, eps=1e-5)
        elif config.normalization == NormalizationStrategy.RMS_NORM:
            self.norm1 = RMSNorm(config.hidden_dim)
            self.norm2 = RMSNorm(config.hidden_dim)

        self.attention = Attention(config)
        self.mlp = MLP(config)

        self.residual_scale = nn.Parameter(torch.tensor([config.residual_scale] * config.num_layers)[layer_idx:layer_idx+1])

    def forward(self, x: torch.Tensor, seq_pos=None):
        residual = x
        x = self.norm1(x)
        x = self.attention(x, seq_pos)
        x = residual + x * self.residual_scale

        residual = x
        x = self.norm2(x)
        x = self.mlp(x)
        x = residual + x * self.residual_scale

        return x


class TinyTransformer(nn.Module):
    """Minimal transformer with full architectural control."""

    def __init__(self, config: TransformerConfig):
        super().__init__()
        self.config = config

        if config.tie_embeddings:
            self.token_embed = nn.Embedding(config.vocab_size, config.hidden_dim)
            self.out_proj = nn.Linear(config.hidden_dim, config.vocab_size, bias=False)
            self.out_proj.weight = self.token_embed.weight
        else:
            self.token_embed = nn.Embedding(config.vocab_size, config.hidden_dim)
            self.out_proj = nn.Linear(config.hidden_dim, config.vocab_size)

        if config.positional_encoding == PositionalEncodingType.ABSOLUTE:
            self.pos_embed = nn.Embedding(config.max_seq_len, config.hidden_dim)
        elif config.positional_encoding == PositionalEncodingType.NONE:
            self.pos_embed = None

        self.blocks = nn.ModuleList([
            TransformerBlock(config, layer_idx=i) for i in range(config.num_layers)
        ])

        if config.normalization == NormalizationStrategy.LAYER_NORM:
            self.norm_out = nn.LayerNorm(config.hidden_dim, eps=1e-5)
        elif config.normalization == NormalizationStrategy.RMS_NORM:
            self.norm_out = RMSNorm(config.hidden_dim)

    def forward(self, tokens: torch.Tensor, positions=None):
        """Forward pass.
        
        Args:
            tokens: [batch, seq_len] of token indices
            positions: Optional [seq_len] position indices (defaults to 0..seq_len-1)
            
        Returns:
            [batch, seq_len, vocab_size] logits
        """
        batch, seq_len = tokens.shape

        if positions is None:
            positions = torch.arange(seq_len, device=tokens.device)

        x = self.token_embed(tokens)

        if self.pos_embed is not None:
            pos_emb = self.pos_embed(positions).unsqueeze(0)
            x = x + pos_emb

        for block in self.blocks:
            x = block(x, positions)

        x = self.norm_out(x)
        return self.out_proj(x)

    def num_parameters(self) -> int:
        """Count total parameters."""
        return sum(p.numel() for p in self.parameters())