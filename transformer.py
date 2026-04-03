import torch
import torch.nn as nn


class TinyTransformer(nn.Module):
    def __init__(
        self,
        vocab_size: int,
        n_embd: int = 128,
        n_head: int = 4,
        n_layer: int = 2,
        block_size: int = 128,
        dropout: float = 0.0,
        tie_weights: bool = True,
        norm_type: str = "layer",  # "layer", "rms"
    ):
        super().__init__()
        self.config = {
            "vocab_size": vocab_size,
            "n_embd": n_embd,
            "n_head": n_head,
            "n_layer": n_layer,
            "block_size": block_size,
            "dropout": dropout,
            "tie_weights": tie_weights,
            "norm_type": norm_type,
        }

        self.transformer = nn.ModuleDict(
            dict(
                wte=nn.Embedding(vocab_size, n_embd),
                wpe=nn.Embedding(block_size, n_embd),
                drop=nn.Dropout(dropout),
                h=nn.ModuleList(
                    [
                        Block(n_embd, n_head, block_size, dropout, norm_type)
                        for _ in range(n_layer)
                    ]
                ),
                ln_f=nn.LayerNorm(n_embd) if norm_type == "layer" else RMSNorm(n_embd),
            )
        )
        self.lm_head = nn.Linear(n_embd, vocab_size, bias=False)

        if tie_weights:
            self.transformer.wte.weight = self.lm_head.weight

        # init all weights
        self.apply(self._init_weights)

    def _init_weights(self, module):
        if isinstance(module, nn.Linear):
            torch.nn.init.normal_(module.weight, mean=0.0, std=0.02)
            if module.bias is not None:
                torch.nn.init.zeros_(module.bias)
        elif isinstance(module, nn.Embedding):
            torch.nn.init.normal_(module.weight, mean=0.0, std=0.02)

    def forward(self, idx, targets=None):
        device = idx.device
        b, t = idx.size()
        pos = torch.arange(0, t, dtype=torch.long, device=device).unsqueeze(0)

        tok_emb = self.transformer.wte(idx)
        pos_emb = self.transformer.wpe(pos)
        x = self.transformer.drop(tok_emb + pos_emb)

        for block in self.transformer.h:
            x = block(x)
        x = self.transformer.ln_f(x)
        logits = self.lm_head(x)

        loss = None
        if targets is not None:
            loss = nn.functional.cross_entropy(
                logits.view(-1, logits.size(-1)), targets.view(-1), ignore_index=-1
            )

        return logits, loss


class Block(nn.Module):
    def __init__(self, n_embd, n_head, block_size, dropout, norm_type, causal=True):
        super().__init__()
        self.ln_1 = nn.LayerNorm(n_embd) if norm_type == "layer" else RMSNorm(n_embd)
        self.attn = CausalSelfAttention(n_embd, n_head, block_size, dropout, causal=causal)
        self.ln_2 = nn.LayerNorm(n_embd) if norm_type == "layer" else RMSNorm(n_embd)
        self.mlp = nn.ModuleDict(
            dict(
                c_fc=nn.Linear(n_embd, 4 * n_embd),
                c_proj=nn.Linear(4 * n_embd, n_embd),
                act=nn.GELU(),
                drop=nn.Dropout(dropout),
            )
        )

    def forward(self, x):
        x = x + self.attn(self.ln_1(x))
        m = self.mlp
        x = x + m.drop(m.c_proj(m.act(m.c_fc(self.ln_2(x)))))
        return x


class CausalSelfAttention(nn.Module):
    def __init__(self, n_embd, n_head, block_size, dropout, causal=True):
        super().__init__()
        assert n_embd % n_head == 0
        self.c_attn = nn.Linear(n_embd, 3 * n_embd)
        self.c_proj = nn.Linear(n_embd, n_embd)
        self.attn_drop = nn.Dropout(dropout)
        self.resid_drop = nn.Dropout(dropout)
        self.n_head = n_head
        self.n_embd = n_embd
        self.causal = causal
        self.register_buffer(
            "bias",
            torch.tril(torch.ones(block_size, block_size)).view(
                1, 1, block_size, block_size
            ),
        )

    def forward(self, x):
        B, T, C = x.size()
        q, k, v = self.c_attn(x).split(self.n_embd, dim=2)
        k = k.view(B, T, self.n_head, C // self.n_head).transpose(1, 2)
        q = q.view(B, T, self.n_head, C // self.n_head).transpose(1, 2)
        v = v.view(B, T, self.n_head, C // self.n_head).transpose(1, 2)

        att = (q @ k.transpose(-2, -1)) * (1.0 / (k.size(-1) ** 0.5))
        if self.causal:
            att = att.masked_fill(self.bias[:, :, :T, :T] == 0, float("-inf"))
        att = nn.functional.softmax(att, dim=-1)
        att = self.attn_drop(att)
        y = att @ v
        y = y.transpose(1, 2).contiguous().view(B, T, C)
        y = self.resid_drop(self.c_proj(y))
        return y


class RMSNorm(nn.Module):
    def __init__(self, dim, eps=1e-6):
        super().__init__()
        self.eps = eps
        self.weight = nn.Parameter(torch.ones(dim))

    def _norm(self, x):
        return x * torch.rsqrt(x.pow(2).mean(-1, keepdim=True) + self.eps)

    def forward(self, x):
        output = self._norm(x.float()).type_as(x)
        return output * self.weight
