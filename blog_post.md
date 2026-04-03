# Confluence in Context: When KV Cache Interpolation Preserves, Dilutes, or Collapses Branching Semantics

Branching is easy. Merging is hard.

Forking a large language model (LLM) conversation requires only copying context. One branch explores cats, another dogs; one pursues optimism, another pessimism. But rejoining divergent branches has no obvious mechanism. The standard options are to pick a winner, summarize both, or concatenate them and hope. All operate in text space. None performs actual reconciliation.

This post tests a more direct approach: merge branches in latent space.

For autoregressive transformers, the key-value (KV) cache is the natural latent state. Run a shared prefix, save the cache at the fork, continue with two branch prompts, and treat each branch's continuation as a delta from the fork. Interpolate those deltas, rebuild a merged cache, decode.

This should fail. The branches traverse different semantic trajectories. Their keys and values occupy identical positional slots but represent different tokens. Averaging them could produce gibberish.

The merge does not collapse into gibberish.

The merged cache decodes into fluent text. Cache interpolation lands in a valid region of activation space. But fluency is not the finding. The finding is the semantic state that the merge creates.

Three outcomes emerged. Some merges preserve both branches. Some dilute them into generic continuation. Some collapse toward one branch while suppressing the other.

That is the core result: KV-cache merging is mechanically viable but semantically underdetermined. Geometric interpolation can produce confluence, washout, or collapse depending on branch structure.

## Method

Run a shared prefix through the model. Save the KV cache at that point: the fork. Continue from the fork with two branch prompts, A and B. Save each branch's final cache.

Branch caches cannot be subtracted from the fork as full tensors because the sequence length grows. The useful object is the suffix: the new keys and values added after the fork.

```text
delta_A = branch_cache_A[fork_position:]
delta_B = branch_cache_B[fork_position:]
```

The merge operator interpolates these suffix deltas and reattaches them to the shared fork:

```text
merged_suffix = 0.5 * (delta_A + delta_B)
merged_cache = concat(fork_cache, merged_suffix)
```

One refinement matters. In the sweep, merging only the upper half of transformer layers performs as well as merging all layers. Lower layers contribute little to branch-specific behavior; late layers carry the lexical and semantic divergence.

Decoding from the merged cache requires a probe token. HuggingFace causal models expect at least one input token to produce logits from a cache. The experiments use a fixed probe token (`" The"`) appended after the merged state, with additional probes in follow-up checks.

### Figure 1: Method Pipeline

![method](figures/method_diagram.png)

A shared prefix anchors both branches in one scaffold. The merge replaces only the post-fork suffix, primarily in late layers, so the experiment isolates what branch-specific cache deltas carry.

This procedure defines branch merging in latent space. The next question is what semantic state it produces. The merged cache remains fluent, but fluency is not the finding. The finding is that merged states fall into three regimes.

## Three Regimes

The merged cache did not produce one kind of outcome. It produced three.

To separate them, I tracked three quantities at the probe step.

**Branch balance** measures which branch dominates the merged distribution:

```text
balance = log(mass_A / mass_B)
```

**Branch mass** measures how much probability remains on branch-specific vocabulary:

```text
branch_mass = mass_A + mass_B
```

**Entropy change** measures whether the merge sharpens or diffuses the distribution relative to the unmerged branches:

```text
dH_mid = H(merged) - 0.5 * (H(branch_A) + H(branch_B))
```

These three quantities define the taxonomy.

**Confluence** occurs when balance stays near zero, branch mass stays high, and entropy remains stable. Both branches remain active in the merged distribution.

**Washout** occurs when branch mass drops and entropy increases. The merge stays fluent but loses branch-specific content. Output drifts toward generic continuation.

**Collapse** occurs when branch mass stays high, balance shifts strongly toward one branch, and entropy decreases. The merge does not preserve both branches. It sharpens into one.

### Figure 2: Regime Map

![regime_map](figures/regime_map.png)

KV-cache merge outcomes in `(|balance|, branch_mass)` space, colored by entropy change `dH_mid`. Each pair appears across all four merge strategies; labels mark the best-performing strategy. Three regimes emerge: confluence at low `|balance|` and high branch mass with near-zero entropy change, washout at low branch mass with positive entropy change, and collapse at high `|balance|` with negative entropy change.

The map shows that confluence is real. Some pairs remain centered and retain branch mass after merging.

The map also shows that confluence is not the default. Different-frame pairs lose mass and wash out. Homograph and adversarial pairs shift rightward into collapse: the merged state stays sharp, but one branch wins.

This distinction matters because entropy alone cannot separate the failure modes. Washout and collapse both fail to preserve both branches, but they fail in opposite directions. Washout diffuses. Collapse resolves.

The next question is what predicts which regime appears.

## Late-Layer Alignment Predicts Washout

The regime map shows what happened. The next question is what predicts it.

The strongest signal comes from late transformer layers, specifically the value cache. For each branch pair, I measured cosine similarity between branch deltas layer by layer, then compared alignment to entropy change after merging.

The pattern is simple. High late-layer value-cache (V) similarity keeps entropy stable. Low late-layer V similarity increases washout risk.

### Figure 3: Late V Alignment vs Entropy

![late_v_vs_entropy](figures/late_v_vs_entropy.png)

Late-layer value-cache cosine similarity versus entropy change `dH_mid` for all pair-strategy combinations. Higher alignment is associated with stable or reduced entropy, while lower alignment increases washout risk. Homograph and adversarial cases fall below that trend because they collapse toward one branch rather than diffusing.

The trend is clear but incomplete. Some low-alignment pairs wash out. Others collapse. Late V similarity predicts when merges lose specificity. It does not predict which branch wins when specificity sharpens rather than diffuses.

That asymmetry is the key finding. If entropy were sufficient, collapse would look like success. Several homograph and adversarial pairs produced low entropy, yet achieved it by sharpening toward one branch while suppressing the other. The balance metric exposed this failure mode.

Late-layer alignment therefore predicts one axis of the problem. It separates stable merges from diffuse ones. It does not distinguish confluence from winner-take-all resolution.

## Homograph Collapse

The clearest examples came from lexical ambiguity.

Homograph pairs did not preserve both senses. They collapsed. A merged *bank* cache sharpened toward the financial reading. A merged *bow* cache sharpened toward the nautical reading. A merged *pitcher* cache resolved toward baseball. Interpolation did not maintain ambiguity. It forced a decision.

This result sets a hard limit on the method. Cache interpolation can blend compatible frames. It can wash out incompatible ones. But when branches encode unresolved lexical ambiguity, interpolation collapses into one basin of the output distribution. The merge selects; it does not reconcile.

## Discussion

These results define a narrow but useful claim.

KV-cache interpolation can merge divergent branches in latent space. The merged cache remains fluent. But fluency is not the criterion. The semantic outcome depends on branch structure.

Compatible branches produce confluence. Incompatible frames wash out. Ambiguous branches collapse. Cache merging therefore operates inside a restricted mergeability regime.

That regime has two practical implications.

First, branch merging should be a diagnostic decision, not a default. Some branches should merge; some should remain separate. A useful system decides between those options from measurable signals, not from hope.

Second, the signals suggest a workable heuristic. Early-layer key-cache (K) structure reflects frame compatibility. Late-layer value-cache alignment reflects whether specificity survives the merge. Branch balance and branch mass reveal which regime the merged state entered.

That combination yields a simple policy:

1. Check structural compatibility via early-layer K alignment.
2. Estimate washout risk via late-layer V alignment.
3. Merge only if the expected regime is acceptable.
4. After merging, measure balance and branch mass to confirm the outcome.

This policy matters for systems that explore parallel branches. A branching framework like Distributary does not need universal reconciliation. It needs a way to distinguish reconcilable branches from incompatible ones, and to act accordingly.

That distinction is the practical contribution. These experiments do not provide general semantic reconciliation. They provide a map: when reconciliation holds, when it dilutes, when it silently selects a winner.

The main limit is equally clear. Cache interpolation does not preserve unresolved ambiguity. When two branches encode different senses of the same word, the merge selects one. Geometric averaging is not semantic synthesis.

## Conclusion

KV-cache interpolation makes branch merging mechanically possible. The merged state is fluent, and sometimes genuinely preserves both branches. But the outcome is not uniform. It falls into three regimes: confluence, washout, and collapse.

That taxonomy reframes the question. The question is not whether cache merging works. The question is when it preserves meaning, when it dilutes meaning, and when it resolves meaning by force.

Larger models may shift the regime boundaries. Better operators may widen the confluence zone. Stronger diagnostics may improve the gate. But any future method faces the same constraint: branch merging is valuable only if it preserves the semantic structure that made branching worth doing.
