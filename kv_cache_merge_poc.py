# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "torch>=2.2",
#   "transformers>=4.39",
# ]
# ///

from __future__ import annotations

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
from transformers.cache_utils import DynamicCache

MODEL_NAME = "distilgpt2"
PREFIX = "At the pet shop, the child kept thinking about"
BRANCH_CANDIDATES = [
    (" cats and kittens.", " dogs and puppies."),
    (" about cats.", " about dogs."),
    (" with cats.", " with dogs."),
]
MAX_NEW_TOKENS = 24
TEMPERATURE = 0.8
TOP_K = 40
REPORT_TOP_K = 8
SEED = 7
TARGET_TOKENS = [" cat", " cats", " dog", " dogs", " pet", " child", " puppy", " kitten"]
PROBE_TEXTS = [".", " about", " The", " It"]
STRATEGY_PROBES = [" about", " The"]


def cache_layers(past_key_values):
    if isinstance(past_key_values, DynamicCache):
        return tuple(
            (layer.keys.clone(), layer.values.clone())
            for layer in past_key_values.layers
        )
    return tuple((key.clone(), value.clone()) for key, value in past_key_values)


def as_dynamic_cache(cache):
    if cache is None or isinstance(cache, DynamicCache):
        return cache
    return DynamicCache(list(cache))


def cache_seq_len(cache) -> int:
    return cache[0][0].shape[2]


def slice_cache(cache, end_pos: int):
    return tuple(
        (
            key[:, :, :end_pos, :].clone(),
            value[:, :, :end_pos, :].clone(),
        )
        for key, value in cache
    )


def compute_delta_cache(fork_cache, branch_cache):
    fork_len = cache_seq_len(fork_cache)
    branch_len = cache_seq_len(branch_cache)
    if branch_len <= fork_len:
        raise ValueError("Branch cache must be longer than the fork cache.")

    # Sequence length grows, so the "delta" is the suffix added after the fork.
    return tuple(
        (
            key[:, :, fork_len:, :].clone(),
            value[:, :, fork_len:, :].clone(),
        )
        for key, value in branch_cache
    )


def blend_delta_caches(delta_a, delta_b, layer_weights):
    blended = []
    for layer_idx, ((key_a, value_a), (key_b, value_b)) in enumerate(
        zip(delta_a, delta_b, strict=True)
    ):
        if key_a.shape != key_b.shape or value_a.shape != value_b.shape:
            raise ValueError(
                "Branch deltas must have identical shapes. "
                "Use branch prompts with the same token length."
            )
        weight = layer_weights[layer_idx]
        blended.append(
            (
                (1.0 - weight) * key_a + weight * key_b,
                (1.0 - weight) * value_a + weight * value_b,
            )
        )
    return tuple(blended)


def average_delta_caches(delta_a, delta_b):
    return blend_delta_caches(delta_a, delta_b, [0.5] * len(delta_a))


def slerp_tensor(tensor_a, tensor_b, weight, dot_threshold: float = 0.9995, eps: float = 1e-8):
    flat_a = tensor_a.reshape(-1)
    flat_b = tensor_b.reshape(-1)
    norm_a = torch.linalg.vector_norm(flat_a)
    norm_b = torch.linalg.vector_norm(flat_b)

    if norm_a <= eps or norm_b <= eps:
        return (1.0 - weight) * tensor_a + weight * tensor_b

    dir_a = flat_a / norm_a
    dir_b = flat_b / norm_b
    dot = torch.clamp(torch.dot(dir_a, dir_b), -1.0, 1.0)
    if torch.abs(dot) > dot_threshold:
        return (1.0 - weight) * tensor_a + weight * tensor_b

    theta = torch.acos(dot)
    sin_theta = torch.sin(theta)
    interp_dir = (
        torch.sin((1.0 - weight) * theta) / sin_theta * dir_a
        + torch.sin(weight * theta) / sin_theta * dir_b
    )
    interp_norm = (1.0 - weight) * norm_a + weight * norm_b
    return (interp_dir * interp_norm).reshape_as(tensor_a)


def slerp_delta_caches(delta_a, delta_b, layer_weights):
    blended = []
    for layer_idx, ((key_a, value_a), (key_b, value_b)) in enumerate(
        zip(delta_a, delta_b, strict=True)
    ):
        if key_a.shape != key_b.shape or value_a.shape != value_b.shape:
            raise ValueError(
                "Branch deltas must have identical shapes. "
                "Use branch prompts with the same token length."
            )
        weight = layer_weights[layer_idx]
        blended.append(
            (
                slerp_tensor(key_a, key_b, weight),
                slerp_tensor(value_a, value_b, weight),
            )
        )
    return tuple(blended)


def apply_delta_cache(fork_cache, delta_cache):
    merged = []
    for (fork_key, fork_value), (delta_key, delta_value) in zip(
        fork_cache, delta_cache, strict=True
    ):
        merged.append(
            (
                torch.cat([fork_key.clone(), delta_key], dim=2),
                torch.cat([fork_value.clone(), delta_value], dim=2),
            )
        )
    return tuple(merged)


def apply_layerwise_delta_cache(fork_cache, base_delta, override_delta, override_layers):
    layer_set = set(override_layers)
    mixed = []
    for layer_idx, (
        (fork_key, fork_value),
        (base_key, base_value),
        (override_key, override_value),
    ) in enumerate(zip(fork_cache, base_delta, override_delta, strict=True)):
        delta_key = override_key if layer_idx in layer_set else base_key
        delta_value = override_value if layer_idx in layer_set else base_value
        mixed.append(
            (
                torch.cat([fork_key.clone(), delta_key], dim=2),
                torch.cat([fork_value.clone(), delta_value], dim=2),
            )
        )
    return tuple(mixed)


@torch.no_grad()
def run_model(model, input_ids, past_key_values=None):
    outputs = model(
        input_ids=input_ids,
        past_key_values=as_dynamic_cache(past_key_values),
        use_cache=True,
        return_dict=True,
    )
    return outputs.logits, cache_layers(outputs.past_key_values)


def sample_next_token(logits, generator):
    scaled = logits / TEMPERATURE
    top_k = min(TOP_K, scaled.shape[-1])
    top_logits, top_indices = torch.topk(scaled, k=top_k, dim=-1)
    probs = torch.softmax(top_logits, dim=-1)
    sample_index = torch.multinomial(probs, num_samples=1, generator=generator)
    return top_indices.gather(-1, sample_index)


@torch.no_grad()
def sample_decode_from_cache(
    model,
    tokenizer,
    past_key_values,
    last_token_id,
    steps: int,
    seed: int,
):
    generator = torch.Generator(device=last_token_id.device).manual_seed(seed)
    logits, cache = run_model(model, last_token_id, past_key_values=past_key_values)
    next_token = sample_next_token(logits[:, -1, :], generator)
    generated_ids = [next_token.item()]

    for _ in range(steps - 1):
        logits, cache = run_model(model, next_token, past_key_values=cache)
        next_token = sample_next_token(logits[:, -1, :], generator)
        generated_ids.append(next_token.item())

    return tokenizer.decode(generated_ids, skip_special_tokens=True)


def top_tokens(tokenizer, logits, k: int = REPORT_TOP_K):
    probs = torch.softmax(logits, dim=-1)
    top_probs, top_ids = torch.topk(probs, k=min(k, probs.shape[-1]), dim=-1)
    rows = []
    for token_id, prob in zip(top_ids[0].tolist(), top_probs[0].tolist(), strict=True):
        rows.append((token_id, tokenizer.decode([token_id]), prob))
    return rows


def token_prob_rows(tokenizer, logits, token_texts):
    probs = torch.softmax(logits, dim=-1)[0]
    rows = []
    for token_text in token_texts:
        token_ids = tokenizer(token_text, add_special_tokens=False).input_ids
        if len(token_ids) != 1:
            raise ValueError(f"Expected a single token for {token_text!r}, got {token_ids}.")
        token_id = token_ids[0]
        rows.append((token_id, token_text, probs[token_id].item()))
    return rows


def pick_compatible_branches(tokenizer):
    for branch_a, branch_b in BRANCH_CANDIDATES:
        ids_a = tokenizer(branch_a, add_special_tokens=False, return_tensors="pt").input_ids
        ids_b = tokenizer(branch_b, add_special_tokens=False, return_tensors="pt").input_ids
        if ids_a.shape[1] == ids_b.shape[1] and ids_a[0, -1].item() == ids_b[0, -1].item():
            return branch_a, branch_b, ids_a, ids_b
    raise RuntimeError(
        "No compatible branch prompts found. Need equal token length and shared last token."
    )


def continuation_from_full_cache(model, tokenizer, full_cache, prompt_ids):
    pre_last_cache = slice_cache(full_cache, cache_seq_len(full_cache) - 1)
    last_token_id = prompt_ids[:, -1:]
    return sample_decode_from_cache(
        model=model,
        tokenizer=tokenizer,
        past_key_values=pre_last_cache,
        last_token_id=last_token_id,
        steps=MAX_NEW_TOKENS,
        seed=SEED,
    )


def next_token_rows_from_replayed_last_token(model, tokenizer, full_cache, prompt_ids):
    pre_last_cache = slice_cache(full_cache, cache_seq_len(full_cache) - 1)
    last_token_id = prompt_ids[:, -1:]
    logits, _ = run_model(model, last_token_id, past_key_values=pre_last_cache)
    return top_tokens(tokenizer, logits[:, -1, :])


def next_token_rows_after_probe(model, tokenizer, full_cache, probe_ids):
    logits, _ = run_model(model, probe_ids, past_key_values=full_cache)
    return top_tokens(tokenizer, logits[:, -1, :])


def token_prob_rows_after_probe(model, tokenizer, full_cache, probe_ids, token_texts):
    logits, _ = run_model(model, probe_ids, past_key_values=full_cache)
    return token_prob_rows(tokenizer, logits[:, -1, :], token_texts)


def sample_continuation_after_probe(model, tokenizer, full_cache, probe_ids):
    generator = torch.Generator(device=probe_ids.device).manual_seed(SEED)
    logits, cache = run_model(model, probe_ids, past_key_values=full_cache)
    next_token = sample_next_token(logits[:, -1, :], generator)
    generated_ids = [next_token.item()]

    for _ in range(MAX_NEW_TOKENS - 1):
        logits, cache = run_model(model, next_token, past_key_values=cache)
        next_token = sample_next_token(logits[:, -1, :], generator)
        generated_ids.append(next_token.item())

    return tokenizer.decode(generated_ids, skip_special_tokens=True)


def print_top_rows(title, rows):
    print(title)
    for token_id, token_text, prob in rows:
        print(f"  id={token_id:>5} text={token_text!r:<18} p={prob:.4f}")


def print_token_prob_rows(title, rows):
    print(title)
    for token_id, token_text, prob in rows:
        print(f"  id={token_id:>5} text={token_text!r:<18} p={prob:.4f}")


def rows_to_prob_map(rows):
    return {token_text: prob for _, token_text, prob in rows}


def print_probe_blend_summary(probe_text, rows_a, rows_b, rows_merged):
    probs_a = rows_to_prob_map(rows_a)
    probs_b = rows_to_prob_map(rows_b)
    probs_merged = rows_to_prob_map(rows_merged)
    between_count = 0

    print(f"Probe summary for {probe_text!r}:")
    for token_text in TARGET_TOKENS:
        prob_a = probs_a[token_text]
        prob_b = probs_b[token_text]
        prob_merged = probs_merged[token_text]
        lower = min(prob_a, prob_b)
        upper = max(prob_a, prob_b)
        is_between = lower <= prob_merged <= upper
        midpoint = 0.5 * (prob_a + prob_b)
        if is_between:
            between_count += 1
        print(
            "  "
            f"{token_text!r:<10} "
            f"A={prob_a:.4f} "
            f"B={prob_b:.4f} "
            f"M={prob_merged:.4f} "
            f"mid={midpoint:.4f} "
            f"between={is_between}"
        )
    print(f"  merged between endpoints for {between_count}/{len(TARGET_TOKENS)} target tokens")


def blend_summary_rows(rows_a, rows_b, rows_merged):
    probs_a = rows_to_prob_map(rows_a)
    probs_b = rows_to_prob_map(rows_b)
    probs_merged = rows_to_prob_map(rows_merged)
    rows = []
    between_count = 0
    midpoint_abs_error_sum = 0.0

    for token_text in TARGET_TOKENS:
        prob_a = probs_a[token_text]
        prob_b = probs_b[token_text]
        prob_merged = probs_merged[token_text]
        midpoint = 0.5 * (prob_a + prob_b)
        lower = min(prob_a, prob_b)
        upper = max(prob_a, prob_b)
        is_between = lower <= prob_merged <= upper
        if is_between:
            between_count += 1
        midpoint_abs_error_sum += abs(prob_merged - midpoint)
        rows.append((token_text, prob_a, prob_b, prob_merged, midpoint, is_between))

    return (
        rows,
        between_count,
        midpoint_abs_error_sum / len(TARGET_TOKENS),
    )


def print_strategy_probe_table(title, summary_rows):
    print(title)
    for strategy_name, between_count, mean_midpoint_error in summary_rows:
        print(
            f"  {strategy_name:<22} "
            f"between={between_count}/{len(TARGET_TOKENS)} "
            f"mean_abs_mid_err={mean_midpoint_error:.4f}"
        )


def main():
    torch.set_grad_enabled(False)
    tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
    model = AutoModelForCausalLM.from_pretrained(MODEL_NAME)
    model.eval()

    prefix_ids = tokenizer(PREFIX, add_special_tokens=False, return_tensors="pt").input_ids
    branch_a, branch_b, branch_a_ids, branch_b_ids = pick_compatible_branches(tokenizer)

    _, fork_cache = run_model(model, prefix_ids)
    _, branch_a_cache = run_model(model, branch_a_ids, past_key_values=fork_cache)
    _, branch_b_cache = run_model(model, branch_b_ids, past_key_values=fork_cache)

    delta_a = compute_delta_cache(fork_cache, branch_a_cache)
    delta_b = compute_delta_cache(fork_cache, branch_b_cache)
    num_layers = len(delta_a)
    split = num_layers // 2
    merged_delta = average_delta_caches(delta_a, delta_b)
    uniform_slerp_delta = slerp_delta_caches(delta_a, delta_b, [0.5] * num_layers)
    late_only_weights = [0.0 if layer_idx < split else 0.5 for layer_idx in range(num_layers)]
    late_only_delta = blend_delta_caches(delta_a, delta_b, late_only_weights)
    late_only_slerp_delta = slerp_delta_caches(delta_a, delta_b, late_only_weights)
    merged_cache = apply_delta_cache(fork_cache, merged_delta)
    uniform_slerp_cache = apply_delta_cache(fork_cache, uniform_slerp_delta)
    late_only_cache = apply_delta_cache(fork_cache, late_only_delta)
    late_only_slerp_cache = apply_delta_cache(fork_cache, late_only_slerp_delta)
    strategy_caches = {
        "uniform avg": merged_cache,
        "uniform slerp": uniform_slerp_cache,
        "late-only avg": late_only_cache,
        "late-only slerp": late_only_slerp_cache,
    }

    prompt_a_ids = torch.cat([prefix_ids, branch_a_ids], dim=1)
    prompt_b_ids = torch.cat([prefix_ids, branch_b_ids], dim=1)
    merged_prompt_ids = torch.cat([prefix_ids, branch_a_ids], dim=1)

    continuation_a = continuation_from_full_cache(model, tokenizer, branch_a_cache, prompt_a_ids)
    continuation_b = continuation_from_full_cache(model, tokenizer, branch_b_cache, prompt_b_ids)
    merged_continuation = continuation_from_full_cache(
        model,
        tokenizer,
        merged_cache,
        merged_prompt_ids,
    )
    merged_anchor = f"{PREFIX} <avg({branch_a.strip()} | {branch_b.strip()})>"
    rows_a = next_token_rows_from_replayed_last_token(model, tokenizer, branch_a_cache, prompt_a_ids)
    rows_b = next_token_rows_from_replayed_last_token(model, tokenizer, branch_b_cache, prompt_b_ids)
    rows_merged = next_token_rows_from_replayed_last_token(
        model,
        tokenizer,
        merged_cache,
        merged_prompt_ids,
    )

    merged_replay_token_ids = merged_prompt_ids[:, -1:]
    merged_prefix_probe_ids = prefix_ids[:, -1:]
    merged_the_probe_ids = tokenizer(" The", add_special_tokens=False, return_tensors="pt").input_ids
    the_rows_a = next_token_rows_after_probe(model, tokenizer, branch_a_cache, merged_the_probe_ids)
    the_rows_b = next_token_rows_after_probe(model, tokenizer, branch_b_cache, merged_the_probe_ids)
    the_rows_merged = next_token_rows_after_probe(model, tokenizer, merged_cache, merged_the_probe_ids)
    the_target_rows_a = token_prob_rows_after_probe(
        model,
        tokenizer,
        branch_a_cache,
        merged_the_probe_ids,
        TARGET_TOKENS,
    )
    the_target_rows_b = token_prob_rows_after_probe(
        model,
        tokenizer,
        branch_b_cache,
        merged_the_probe_ids,
        TARGET_TOKENS,
    )
    the_target_rows_merged = token_prob_rows_after_probe(
        model,
        tokenizer,
        merged_cache,
        merged_the_probe_ids,
        TARGET_TOKENS,
    )
    probe_sweep = []
    for probe_text in PROBE_TEXTS:
        probe_ids = tokenizer(probe_text, add_special_tokens=False, return_tensors="pt").input_ids
        probe_sweep.append(
            (
                probe_text,
                token_prob_rows_after_probe(
                    model,
                    tokenizer,
                    branch_a_cache,
                    probe_ids,
                    TARGET_TOKENS,
                ),
                token_prob_rows_after_probe(
                    model,
                    tokenizer,
                    branch_b_cache,
                    probe_ids,
                    TARGET_TOKENS,
                ),
                token_prob_rows_after_probe(
                    model,
                    tokenizer,
                    merged_cache,
                    probe_ids,
                    TARGET_TOKENS,
                ),
            )
        )
    early_merged_on_a_cache = apply_layerwise_delta_cache(
        fork_cache,
        delta_a,
        merged_delta,
        range(split),
    )
    late_merged_on_a_cache = apply_layerwise_delta_cache(
        fork_cache,
        delta_a,
        merged_delta,
        range(split, num_layers),
    )
    layer_probe_rows = [
        (
            "branch A baseline",
            token_prob_rows_after_probe(
                model,
                tokenizer,
                branch_a_cache,
                merged_the_probe_ids,
                TARGET_TOKENS,
            ),
        ),
        (
            "A with early layers merged",
            token_prob_rows_after_probe(
                model,
                tokenizer,
                early_merged_on_a_cache,
                merged_the_probe_ids,
                TARGET_TOKENS,
            ),
        ),
        (
            "A with late layers merged",
            token_prob_rows_after_probe(
                model,
                tokenizer,
                late_merged_on_a_cache,
                merged_the_probe_ids,
                TARGET_TOKENS,
            ),
        ),
        (
            "all layers merged",
            token_prob_rows_after_probe(
                model,
                tokenizer,
                merged_cache,
                merged_the_probe_ids,
                TARGET_TOKENS,
            ),
        ),
    ]
    strategy_probe_summaries = []
    strategy_the_rows = []
    strategy_the_continuations = []
    for probe_text in STRATEGY_PROBES:
        probe_ids = tokenizer(probe_text, add_special_tokens=False, return_tensors="pt").input_ids
        branch_a_rows = token_prob_rows_after_probe(
            model,
            tokenizer,
            branch_a_cache,
            probe_ids,
            TARGET_TOKENS,
        )
        branch_b_rows = token_prob_rows_after_probe(
            model,
            tokenizer,
            branch_b_cache,
            probe_ids,
            TARGET_TOKENS,
        )
        probe_summary_rows = []
        for strategy_name, strategy_cache in strategy_caches.items():
            strategy_rows = token_prob_rows_after_probe(
                model,
                tokenizer,
                strategy_cache,
                probe_ids,
                TARGET_TOKENS,
            )
            _, between_count, mean_midpoint_error = blend_summary_rows(
                branch_a_rows,
                branch_b_rows,
                strategy_rows,
            )
            probe_summary_rows.append(
                (strategy_name, between_count, mean_midpoint_error)
            )
            if probe_text == " The":
                strategy_the_rows.append((strategy_name, strategy_rows))
                strategy_the_continuations.append(
                    (
                        strategy_name,
                        sample_continuation_after_probe(
                            model,
                            tokenizer,
                            strategy_cache,
                            probe_ids,
                        ),
                    )
                )
        strategy_probe_summaries.append((probe_text, probe_summary_rows))
    merged_probe_specs = [
        ("replay branch-final token", merged_replay_token_ids),
        ("append prefix-final token", merged_prefix_probe_ids),
        ("append ' The'", merged_the_probe_ids),
    ]

    print(f"Model: {MODEL_NAME}")
    print(f"Prefix: {PREFIX!r}")
    print(f"Branch A: {branch_a!r}")
    print(f"Branch B: {branch_b!r}")
    print()
    print("Branch A full text:")
    print(PREFIX + branch_a + continuation_a)
    print()
    print("Branch B full text:")
    print(PREFIX + branch_b + continuation_b)
    print()
    print("Merged synthetic context + continuation:")
    print(merged_anchor + merged_continuation)
    print()
    print("Continuation only:")
    print(f"A      : {continuation_a!r}")
    print(f"B      : {continuation_b!r}")
    print(f"Merged : {merged_continuation!r}")
    print()
    print_top_rows("Top next tokens after reconstructing branch A state:", rows_a)
    print()
    print_top_rows("Top next tokens after reconstructing branch B state:", rows_b)
    print()
    print_top_rows("Top next tokens after reconstructing merged state:", rows_merged)
    print()
    print_top_rows("Top next tokens after appending ' The' to branch A:", the_rows_a)
    print()
    print_top_rows("Top next tokens after appending ' The' to branch B:", the_rows_b)
    print()
    print_top_rows("Top next tokens after appending ' The' to merged:", the_rows_merged)
    print()
    print_token_prob_rows("Target token probs after appending ' The' to branch A:", the_target_rows_a)
    print()
    print_token_prob_rows("Target token probs after appending ' The' to branch B:", the_target_rows_b)
    print()
    print_token_prob_rows("Target token probs after appending ' The' to merged:", the_target_rows_merged)
    print()
    print("Fixed probe sweep:")
    for probe_text, probe_rows_a, probe_rows_b, probe_rows_merged in probe_sweep:
        print_probe_blend_summary(probe_text, probe_rows_a, probe_rows_b, probe_rows_merged)
        print()
    print("Merge strategy comparison:")
    for probe_text, summary_rows in strategy_probe_summaries:
        print_strategy_probe_table(f"Probe {probe_text!r}:", summary_rows)
        print()
    print("Strategy token probs after appending ' The':")
    for strategy_name, rows in strategy_the_rows:
        print_token_prob_rows(strategy_name + ":", rows)
        print()
    print("Strategy sampled continuations after appending ' The':")
    for strategy_name, continuation in strategy_the_continuations:
        print(f"  {strategy_name:<22} {' The' + continuation!r}")
    print()
    print("Layer-slice ablation on branch-A baseline with probe ' The':")
    for label, rows in layer_probe_rows:
        print_token_prob_rows(label + ":", rows)
        print()
    print("Merged replay-token sensitivity:")
    for label, probe_ids in merged_probe_specs:
        probe_text = tokenizer.decode(probe_ids[0].tolist())
        probe_rows = next_token_rows_after_probe(model, tokenizer, merged_cache, probe_ids)
        probe_continuation = sample_continuation_after_probe(
            model,
            tokenizer,
            merged_cache,
            probe_ids,
        )
        print(f"  Probe: {label} ({probe_text!r})")
        for token_id, token_text, prob in probe_rows[:5]:
            print(f"    id={token_id:>5} text={token_text!r:<18} p={prob:.4f}")
        print(f"    sampled continuation: {probe_text + probe_continuation!r}")


if __name__ == "__main__":
    main()
