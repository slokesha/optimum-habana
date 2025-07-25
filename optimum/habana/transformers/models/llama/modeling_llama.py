import copy
from functools import partial
from typing import List, Optional, Tuple, Union

import torch
from torch.distributed.distributed_c10d import ProcessGroup
from transformers.activations import ACT2FN
from transformers.cache_utils import Cache, DynamicCache, StaticCache
from transformers.modeling_outputs import BaseModelOutputWithPast, CausalLMOutputWithPast
from transformers.modeling_rope_utils import ROPE_INIT_FUNCTIONS
from transformers.models.llama.modeling_llama import (
    KwargsForCausalLM,
    LlamaAttention,
    LlamaDecoderLayer,
    LlamaForCausalLM,
    LlamaMLP,
    LlamaModel,
    LlamaRMSNorm,
    apply_rotary_pos_emb,
    logger,
)
from transformers.processing_utils import Unpack

from .... import distributed
from ....distributed import parallel_state
from ....distributed.strategy import DistributedStrategy, NoOpStrategy
from ....distributed.tensorparallel import (
    reduce_from_tensor_model_parallel_region,
)
from ....distributed.tp import TPModule
from ....utils.features import import_usable_component
from ...modeling_attn_mask_utils import (
    _gaudi_prepare_4d_causal_attention_mask,
)
from ..modeling_all_models import Matmul, apply_customized_rope_module
from .configuration_llama import LlamaConfig


try:
    from habana_frameworks.torch.hpex.kernels import RotaryPosEmbeddingHelperV2 as FusedRoPE  # noqa

    has_fused_rope = True
except ImportError:
    has_fused_rope = False
    print("Not using HPU fused kernel for apply_rotary_pos_emb")

try:
    from habana_frameworks.torch.hpex.kernels import FusedSDPA
except ImportError:
    print("Not using HPU fused scaled dot-product attention kernel.")
    FusedSDPA = None

import habana_frameworks.torch.core as htcore


FusedRMSNorm, has_fused_rms_norm = import_usable_component(
    "habana_frameworks.torch.hpex.normalization", "FusedRMSNorm"
)


def gaudi_llama_rmsnorm_forward(self, hidden_states):
    """
    Copied from LlamaRMSNorm.forward: https://github.com/huggingface/transformers/blob/main/src/transformers/models/llama/modeling_llama.py
    The only differences are:
        - override RMSNorm with Habana fused RMSNorm
    """
    if hidden_states.device.type == "hpu" and has_fused_rms_norm:
        # mixed dtypes are not good for FusedRMSNorm, both inputs need to have same dtype
        if hidden_states.dtype != self.weight.dtype:
            orig_dtype = hidden_states.dtype
            hidden_states = FusedRMSNorm.apply(hidden_states.to(self.weight.dtype), self.weight, self.variance_epsilon)
            return hidden_states.to(orig_dtype)
        else:
            hidden_states = FusedRMSNorm.apply(hidden_states, self.weight, self.variance_epsilon)
            return hidden_states
    else:
        input_dtype = hidden_states.dtype
        hidden_states = hidden_states.to(torch.float32)
        variance = hidden_states.pow(2).mean(-1, keepdim=True)
        hidden_states = hidden_states * torch.rsqrt(variance + self.variance_epsilon)
        return self.weight * hidden_states.to(input_dtype)


class GaudiLlamaRotaryEmbedding(torch.nn.Module):
    def __init__(self, config: LlamaConfig, device=None):
        super().__init__()

        # BC: "rope_type" was originally "type"
        if hasattr(config, "rope_scaling") and config.rope_scaling is not None:
            self.rope_type = config.rope_scaling.get("rope_type", config.rope_scaling.get("type"))
        else:
            self.rope_type = "default"
        self.max_seq_len_cached = config.max_position_embeddings
        self.original_max_seq_len = config.max_position_embeddings

        if self.rope_type == "linear":
            self.scaling_factor = config.rope_scaling["factor"]
        elif self.rope_type == "dynamic":
            self.scaling_factor = config.rope_scaling["factor"]
            self.base = config.rope_theta
            partial_rotary_factor = config.partial_rotary_factor if hasattr(config, "partial_rotary_factor") else 1.0
            head_dim = getattr(config, "head_dim", config.hidden_size // config.num_attention_heads)
            self.dim = int(head_dim * partial_rotary_factor)

        self.config = config
        self.rope_init_fn = ROPE_INIT_FUNCTIONS[self.rope_type]

        inv_freq, self.attention_scaling = self.rope_init_fn(self.config, device)
        self.register_buffer("inv_freq", inv_freq, persistent=False)
        self.original_inv_freq = self.inv_freq

        # Build here to make `torch.jit.trace` work.
        self._set_cos_sin_cache(
            seq_len=self.max_seq_len_cached, device=self.inv_freq.device, dtype=torch.get_default_dtype()
        )

    def _set_cos_sin_cache(self, seq_len, device, dtype):
        self.max_seq_len_cached = seq_len

        if self.rope_type == "dynamic" and seq_len > self.config.max_position_embeddings:
            base = self.base * (
                (self.scaling_factor * seq_len / self.config.max_position_embeddings) - (self.scaling_factor - 1)
            ) ** (self.dim / (self.dim - 2))
            inv_freq = 1.0 / (base ** (torch.arange(0, self.dim, 2).float().to(device) / self.dim))
            self.register_buffer("inv_freq", inv_freq, persistent=False)

        # Use torch.int32 to avoid loss due to low precision with BF16 (refer to SW-215204)
        t = torch.arange(self.max_seq_len_cached, device=device, dtype=torch.int32)

        if self.rope_type == "linear":
            t = t / self.scaling_factor

        freqs = torch.outer(t, self.inv_freq)
        # Different from paper, but it uses a different permutation in order to obtain the same calculation
        emb = torch.cat((freqs, freqs), dim=-1)
        self.register_buffer("_cos_cached", emb.cos().to(dtype), persistent=False)
        self.register_buffer("_sin_cached", emb.sin().to(dtype), persistent=False)

    def _dynamic_frequency_update(self, seq_len, device):
        """
        dynamic RoPE layers should recompute `inv_freq` in the following situations:
        1 - growing beyond the cached sequence length (allow scaling)
        2 - the current sequence length is in the original scale (avoid losing precision with small sequences)
        """
        # seq_len = torch.max(position_ids) + 1
        if seq_len > self.max_seq_len_cached:  # growth
            inv_freq, self.attention_scaling = self.rope_init_fn(self.config, device, seq_len=seq_len)
            self.register_buffer("inv_freq", inv_freq, persistent=False)  # TODO joao: may break with compilation
            self.max_seq_len_cached = seq_len

        if seq_len < self.original_max_seq_len and self.max_seq_len_cached > self.original_max_seq_len:  # reset
            # This .to() is needed if the model has been moved to a device after being initialized (because
            # the buffer is automatically moved, but not the original copy)
            self.original_inv_freq = self.original_inv_freq.to(device)
            self.register_buffer("inv_freq", self.original_inv_freq, persistent=False)
            self.max_seq_len_cached = self.original_max_seq_len

    @torch.no_grad()
    def forward(self, x, seq_len=None):
        # x: [bs, num_attention_heads, seq_len, head_size]

        if "dynamic" in self.rope_type:
            self._dynamic_frequency_update(seq_len, device=x.device)

        if seq_len > self.max_seq_len_cached:
            self._set_cos_sin_cache(seq_len=seq_len, device=x.device, dtype=x.dtype)

        if self.attention_scaling == 1.0:
            return (
                self._cos_cached[:seq_len].to(dtype=x.dtype),
                self._sin_cached[:seq_len].to(dtype=x.dtype),
            )
        else:
            return (
                self._cos_cached[:seq_len].to(dtype=x.dtype) * self.attention_scaling,
                self._sin_cached[:seq_len].to(dtype=x.dtype) * self.attention_scaling,
            )


class GaudiLlamaMLP(LlamaMLP):
    def __init__(self, config):
        super(LlamaMLP, self).__init__()
        self.config = config
        self.hidden_size = config.hidden_size
        self.intermediate_size = config.intermediate_size
        self.gate_proj = torch.nn.Linear(self.hidden_size, self.intermediate_size, bias=config.mlp_bias)
        self.up_proj = torch.nn.Linear(self.hidden_size, self.intermediate_size, bias=config.mlp_bias)
        self.down_proj = torch.nn.Linear(self.intermediate_size, self.hidden_size, bias=config.mlp_bias)
        self.act_fn = ACT2FN[config.hidden_act]

    def pre_mlp_forward(self, x):
        input = self.act_fn(self.gate_proj(x)) * self.up_proj(x)
        output = self.down_proj(input)
        return output

    def mlp_all_reduce(self, x):
        if hasattr(self.down_proj, "all_reduce"):
            self.down_proj.all_reduce(x)

    def post_mlp_forward(self, x):
        if hasattr(self.down_proj, "post_all_reduce"):
            return self.down_proj.post_all_reduce(x)
        return x


class TPGaudiLlamaMLP(GaudiLlamaMLP, TPModule):
    def __init__(
        self,
        config,
        group: Optional[ProcessGroup] = None,
    ):
        assert torch.distributed.is_initialized()
        rank, world_size = distributed.rank_and_world(group)
        hidden_dim = int(config.hidden_grow_factor * config.hidden_size)
        assert hidden_dim % world_size == 0, "Hidden dim must be divisible by world size"

        self.config = copy.deepcopy(config)
        self.config.intermediate_size = int((config.hidden_grow_factor / world_size) * config.hidden_size)
        GaudiLlamaMLP.__init__(self, self.config)
        self.setup_tp(rank, world_size)

    def colwise_param_names(self) -> List[str]:
        return ["up_proj", "gate_proj"]

    def rowwise_param_names(self) -> List[str]:
        return ["down_proj"]

    @staticmethod
    def import_module(glu: GaudiLlamaMLP, group: ProcessGroup) -> "TPGaudiLlamaMLP":
        config = copy.deepcopy(glu.config)
        config.hidden_grow_factor = glu.config.intermediate_size / glu.config.hidden_size
        tp_glu = TPGaudiLlamaMLP(config=config, group=group)
        return tp_glu

    def pre_mlp_forward(self, x):
        out_par = GaudiLlamaMLP.pre_mlp_forward(self, x)
        return reduce_from_tensor_model_parallel_region(out_par)


def gaudi_llama_repeat_kv(
    query_states: torch.Tensor,
    key_states: torch.Tensor,
    value_states: torch.Tensor,
    attention_mask: torch.Tensor,
    n_rep: int,
):
    """
    Copied from repeat_kv: https://github.com/huggingface/transformers/blob/main/src/transformers/models/llama/modeling_llama.py
    The only differences are:
        - Append num_key_value_heads == 1 check as kv states can be broadcasted during matmuls so need to expand and reshape them.
        - Add new args query_states, key_states, value_states and attention_mask and update the logic for expansion.
    The query states go from (batch, num_heads, seqlen, head_dim) to (batch, num_key_value_heads, n_rep, seqlen, head_dim)
    The key/value states go from (batch, num_key_value_heads, seqlen, head_dim) to (batch, num_key_value_heads, 1, seqlen, head_dim)
    """
    batch, num_key_value_heads, kv_len, head_dim = key_states.shape
    if n_rep == 1 or num_key_value_heads == 1:
        return query_states, key_states, value_states, attention_mask

    new_kv_shape = (batch, num_key_value_heads, 1, kv_len, head_dim)
    key_states = key_states.reshape(new_kv_shape)
    value_states = value_states.reshape(new_kv_shape)

    batch, _, q_len, head_dim = query_states.shape
    new_q_shape = (batch, num_key_value_heads, n_rep, q_len, head_dim)
    query_states = query_states.reshape(new_q_shape)

    if attention_mask is not None:
        # Add groups dim and set to 1
        attention_mask = attention_mask.unsqueeze(1)

    return query_states, key_states, value_states, attention_mask


# FusedScaledDotProductAttention
class ModuleFusedSDPA(torch.nn.Module):
    def __init__(self, fusedSDPA, scale, attention_dropout, enable_recompute, flash_attention_fp8):
        super().__init__()
        self._hpu_kernel_fsdpa = fusedSDPA
        self.scale = scale
        self.attention_dropout = attention_dropout
        self.enable_recompute = enable_recompute
        self.flash_attention_fp8 = flash_attention_fp8

    def forward(
        self,
        query,
        key,
        value,
        attn_mask,
        dropout_p,
        is_causal,
        scale,
        softmax_mode,
        recompute_mode,
        valid_sequence_lengths,
        padding_side="left",
    ):
        return self._hpu_kernel_fsdpa.apply(
            query,
            key,
            value,
            attn_mask,
            dropout_p,
            is_causal,
            scale,
            softmax_mode,
            recompute_mode,
            valid_sequence_lengths,
            padding_side,
        )


class KVCache(torch.nn.Module):
    def __init__(self):
        super(KVCache, self).__init__()
        self.cache = None
        self.inp_seq_len = -1

    def allocate(self, inp_seq_len, dtype, device, shape):
        if self.cache is None or self.cache.shape != shape:
            self.inp_seq_len = inp_seq_len
            self.cache = torch.zeros(shape, dtype=dtype, device=device)
        else:
            assert self.inp_seq_len == inp_seq_len, (
                f"inp_seq_len must be the same. self.inp_seq_len:{self.inp_seq_len} inp_seq_len:{inp_seq_len}"
            )
            self.cache.fill_(0)

    @staticmethod
    def update(prev, cur, dim, idx, inp_seq_len):
        if inp_seq_len != -1:
            # reuse cache logic
            orig_cur = cur
            if prev.shape == cur.shape:
                prev.copy_(cur)
                return orig_cur
            if cur.shape[2] > 1 and cur.shape[2] <= prev.shape[2]:
                # Initialize
                prev[:, :, :inp_seq_len, :].copy_(cur)
                return orig_cur
        if idx is not None:
            # 2+ tokenizer logic if model is static shape optimized
            prev.index_copy_(dim, idx - 1, cur)
            return prev
        else:
            return torch.cat((prev, cur), dim=dim)

    def get_shape(self):
        if self.cache is None:
            return None
        return self.cache.shape

    def forward(self, cur, dim, idx):
        return self.update(self.cache, cur, dim, idx, self.inp_seq_len)


class GaudiDistributedAttention(torch.nn.Module):
    def __init__(
        self, hpu_module_fsdpa: ModuleFusedSDPA, scale, attention_dropout, enable_recompute, flash_attention_fp8
    ):
        super().__init__()
        self._hpu_module_fsdpa = hpu_module_fsdpa
        if parallel_state.sequence_parallel_is_initialized() and parallel_state.get_sequence_parallel_world_size() > 1:
            from deepspeed.sequence.layer import DistributedAttention

            self._hpu_module_fsdpa_distributed = DistributedAttention(
                self._hpu_module_fsdpa, parallel_state.get_sequence_parallel_group(), 1, 2
            )

    def forward(
        self,
        query: torch.Tensor,
        key: torch.Tensor,
        value: torch.Tensor,
        attn_mask: torch.Tensor,
        dropout_p: float,
        is_casual,
        scale,
        softmax_mode,
        recompute_mode,
        valid_sequence_lengths,
        padding_side="left",
    ):
        if parallel_state.sequence_parallel_is_initialized() and parallel_state.get_sequence_parallel_world_size() > 1:
            return self._hpu_module_fsdpa_distributed(
                query,
                key,
                value,
                0,  # As the shape for inputs is [B, N, S, H]
                None,
                attn_mask,
                dropout_p,
                is_casual,
                scale,
                softmax_mode,
                recompute_mode,
                valid_sequence_lengths,
                padding_side,
            )
        else:
            return self._hpu_module_fsdpa(
                query,
                key,
                value,
                attn_mask,
                dropout_p,
                is_casual,
                scale,
                softmax_mode,
                recompute_mode,
                valid_sequence_lengths,
                padding_side,
            )


def get_gaudi_distributed_attention(
    fused_scaled_dot_product_attention, fused_scaled_dot_product_attention_distributed
):
    if parallel_state.sequence_parallel_is_initialized() and parallel_state.get_sequence_parallel_world_size() > 1:
        return fused_scaled_dot_product_attention_distributed
    else:
        return fused_scaled_dot_product_attention


def gaudi_eager_attention_forward(
    module: torch.nn.Module,
    query: torch.Tensor,
    key: torch.Tensor,
    value: torch.Tensor,
    attention_mask: Optional[torch.Tensor],
    scaling: float,
    dropout: float = 0.0,
    attn_softmax_bf16: bool = False,
    **kwargs,
):
    bsz, q_len = kwargs["input_shape"]
    query_states, key_states, value_states, attention_mask = gaudi_llama_repeat_kv(
        query, key, value, attention_mask, module.num_key_value_groups
    )

    attn_weights = module.matmul_qk(query_states, key_states.transpose(-2, -1)) * scaling
    if attention_mask is not None:
        causal_mask = attention_mask[:, :, :, : key_states.shape[-2]]
        attn_weights = attn_weights + causal_mask

    if attn_softmax_bf16:
        attn_weights = torch.nn.functional.softmax(attn_weights, dim=-1, dtype=query_states.dtype)
    else:
        # upcast attention to fp32
        attn_weights = torch.nn.functional.softmax(attn_weights, dim=-1, dtype=torch.float32).to(query_states.dtype)
    attn_weights = torch.nn.functional.dropout(attn_weights, p=dropout, training=module.training)
    attn_output = module.matmul_av(attn_weights, value_states)
    attn_output = attn_output.reshape(bsz, -1, q_len, module.head_dim)

    return attn_output, attn_weights


class GaudiLlamaAttention(LlamaAttention):
    def __init__(self, config: LlamaConfig, layer_idx: Optional[int] = None):
        super().__init__(config, layer_idx)

        self.matmul_qk = Matmul()
        self.matmul_av = Matmul()
        self.k_cache = KVCache()
        self.v_cache = KVCache()

        self.rotary_emb = GaudiLlamaRotaryEmbedding(config=config)
        self.num_key_value_heads = config.num_key_value_heads
        self.hidden_size = config.hidden_size
        self.num_heads = config.num_attention_heads
        self.head_dim = getattr(config, "head_dim", self.hidden_size // self.num_heads)

        if hasattr(config, "fused_qkv") and config.fused_qkv:
            self.num_heads = config.num_attention_heads
            self.head_dim = config.hidden_size // self.num_heads
            self.dim1 = self.num_heads * self.head_dim
            self.dim2 = config.num_key_value_heads * self.head_dim
            self.qkv_proj = torch.nn.Linear(
                self.hidden_size,
                self.dim1 + 2 * self.dim2,
                bias=config.attention_bias,
            )
            self.q_proj = None
            self.k_proj = None
            self.v_proj = None
        self.inp_seq_len = -1
        self.fused_scaled_dot_product_attention = (
            ModuleFusedSDPA(
                FusedSDPA,
                scale=self.scaling,
                attention_dropout=self.attention_dropout,
                enable_recompute=False,
                flash_attention_fp8=getattr(config, "flash_attention_fp8", False),
            )
            if FusedSDPA
            else None
        )
        # for all2all comm, Distributed Attention cares about sequence (s) and number of heads (h) dimensions. In HPU, they are at 1 and 2 indices
        self.fused_scaled_dot_product_attention_distributed = None
        if parallel_state.sequence_parallel_is_initialized() and parallel_state.get_sequence_parallel_world_size() > 1:
            self.fused_scaled_dot_product_attention_distributed = (
                GaudiDistributedAttention(
                    self.fused_scaled_dot_product_attention,
                    scale=self.scaling,
                    attention_dropout=self.attention_dropout,
                    enable_recompute=False,
                    flash_attention_fp8=getattr(config, "flash_attention_fp8", False),
                )
                if FusedSDPA
                else None
            )

    def get_k_proj_weight(self):
        """4bit quantization in GPTQ replaces the k_proj.weight with qweight."""
        if hasattr(self.k_proj, "qweight"):
            return self.k_proj.qweight
        return self.k_proj.weight

    def get_k_proj_weight_dtype(self):
        """4bit quantization in GPTQ replaces the k_proj.weight with qweight.
        Scales tensor gets the weight dtype."""
        if hasattr(self.k_proj, "qweight"):
            return self.k_proj.scales.dtype
        elif hasattr(self.k_proj, "use_qdq") and self.k_proj.use_qdq:
            return self.k_proj.dequant_weights.hp_dtype
        elif isinstance(self.k_cache, KVCache) and "float8" in str(self.k_proj.weight.dtype):
            return self.k_proj.hp_dtype
        return self.k_proj.weight.dtype

    def allocate_kv_cache(self, batch_size, max_seq_len, inp_seq_len):
        cache_shape = (batch_size, self.num_key_value_heads, max_seq_len, self.head_dim)
        device = self.get_k_proj_weight().device
        dtype = self.config.torch_dtype
        self.k_cache.allocate(inp_seq_len, dtype, device, cache_shape)
        self.v_cache.allocate(inp_seq_len, dtype, device, cache_shape)

    def update_sincos_cache(self, seq_len):
        # Call rotary emb forward() to update cos/sin cache when infering more than self.rotary_emb.original_max_seq_len
        # This helps in avoiding creation of these caches during actual model forward pass and
        # reduce memory consumption and improve performance.
        if seq_len > self.rotary_emb.original_max_seq_len:
            self.rotary_emb.original_max_seq_len = seq_len
            _, _ = self.rotary_emb(self.get_k_proj_weight(), seq_len=seq_len)

    def reorder(self, tensor, beam_idx, dim_a, dim_b):
        updated = tensor.index_select(0, beam_idx)
        tensor.copy_(updated)

    def reorder_kv_cache(self, beam_idx: torch.LongTensor):
        if self.k_cache.cache is None:
            return (None, None)

        head_dim = self.k_cache.cache.size(-1)
        seq_length = self.k_cache.cache.size(-2)
        self.reorder(self.k_cache.cache, beam_idx, seq_length, head_dim)
        self.reorder(self.v_cache.cache, beam_idx, seq_length, head_dim)
        return (self.k_cache.cache.shape, self.v_cache.cache.shape)

    def pre_attn_forward(
        self,
        hidden_states: torch.Tensor,
        position_embeddings: Tuple[torch.Tensor, torch.Tensor],
        attention_mask: Optional[torch.Tensor],
        past_key_value: Optional[Cache] = None,
        output_attentions: bool = False,
        use_cache: bool = False,
        cache_position: Optional[torch.LongTensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        token_idx: Optional[torch.Tensor] = None,
        attn_softmax_bf16: Optional[bool] = False,
        reuse_cache: Optional[bool] = False,
        use_flash_attention: Optional[bool] = False,
        flash_attention_recompute: Optional[bool] = False,
        flash_attention_causal_mask: Optional[bool] = False,
        flash_attention_fast_softmax: Optional[bool] = False,
        valid_sequence_lengths: Optional[torch.Tensor] = None,
        cache_idx: int = None,
        num_virtual_tokens: int = None,
        **kwargs,
    ) -> Tuple[torch.Tensor, Optional[torch.Tensor], Optional[Tuple[torch.Tensor]]]:
        """
        Copied from LlamaAttention.forward: https://github.com/huggingface/transformers/blob/main/src/transformers/models/llama/modeling_llama.py
        The only differences are:
        - add new args token_idx
        - optimize KV cache
        - add new args attn_softmax_bf16
        - add new args reuse_cache
        - add new args use_flash_attention
        - add new arg flash_attention_recompute
        - add new arg flash_attention_causal_mask
        - add new arg flash_attention_fast_softmax
        - add new arg num_virtual_tokens
        """
        input_shape = hidden_states.shape[:-1]
        q_len = input_shape[1]
        hidden_shape = (*input_shape, -1, self.head_dim)

        if hasattr(self.config, "fused_qkv") and self.config.fused_qkv:
            qkv_states = self.qkv_proj(hidden_states)
            query_states, key_states, value_states = torch.split(qkv_states, [self.dim1, self.dim2, self.dim2], dim=-1)
        else:
            query_states = self.q_proj(hidden_states)
            key_states = self.k_proj(hidden_states)
            value_states = self.v_proj(hidden_states)

        # use -1 to infer num_heads and num_key_value_heads as they may vary if tensor parallel is used
        query_states = query_states.view(hidden_shape).transpose(1, 2)
        # TODO: update when auto mp params is enabled in DeepSpeed (cf. https://github.com/HabanaAI/DeepSpeed/blob/94309c7b5dfc1a69858f5c9f25737b2f81a332a5/deepspeed/module_inject/replace_module.py#L440)
        key_states = key_states.view(hidden_shape).transpose(1, 2)
        value_states = value_states.view(hidden_shape).transpose(1, 2)

        kv_seq_len = key_states.shape[-2]
        if past_key_value is not None:
            if token_idx is None:
                if hasattr(past_key_value, "get_usable_length"):
                    kv_seq_len += past_key_value.get_usable_length(kv_seq_len, self.layer_idx)
                else:
                    kv_seq_len += past_key_value[0].shape[-2]
            else:
                if reuse_cache and not isinstance(past_key_value[0], torch.Tensor):
                    kv_seq_len = past_key_value[0][-2]
                else:
                    if num_virtual_tokens is not None and num_virtual_tokens == past_key_value[0].shape[-2]:
                        kv_seq_len = past_key_value[0].shape[-2] + kv_seq_len
                    else:
                        kv_seq_len = past_key_value[0].shape[-2]

        # TODO: the following section cause torch.compile performance issue with graph recompilation
        # as we are not using position_embeddings, disable it for now
        # if position_embeddings is None:
        # logger.warning_once(
        # "The attention layers in this model are transitioning from computing the RoPE embeddings internally "
        # "through `position_ids` (2D tensor with the indexes of the tokens), to using externally computed "
        # "`position_embeddings` (Tuple of tensors, containing cos and sin). In v4.46 `position_ids` will be "
        # "removed and `position_embeddings` will be mandatory."
        # )
        # cos, sin = self.rotary_emb(value_states, seq_len=kv_seq_len)
        # else:
        # cos, sin = position_embeddings
        seq_len = kv_seq_len
        if parallel_state.sequence_parallel_is_initialized():
            seq_len = kv_seq_len * parallel_state.get_sequence_parallel_world_size()

        cos, sin = self.rotary_emb(value_states, seq_len=seq_len)
        # If sequence parallel in enabled, position_ids should be based on which part of the sequence is present in the rank
        # As we divide the inputs based on ranks, position_ids are generated to suit that part of the sequence
        if parallel_state.sequence_parallel_is_initialized() and parallel_state.get_sequence_parallel_rank() > 0:
            position_ids = torch.arange(
                kv_seq_len * parallel_state.get_sequence_parallel_rank(),
                kv_seq_len * (parallel_state.get_sequence_parallel_rank() + 1),
                dtype=torch.long,
                device=query_states.device,
            )
            position_ids = position_ids.unsqueeze(0)

        query_states, key_states = apply_customized_rope(
            query_states, key_states, cos, sin, position_ids, self.training
        )

        if use_cache:
            # reuse k, v, self_attention
            if reuse_cache:
                if past_key_value is not None and isinstance(past_key_value[0], torch.Tensor):
                    # prefix tuning case. attach past_key_value to generate first token.
                    key_states = torch.cat((past_key_value[0], key_states), -2)
                    value_states = torch.cat((past_key_value[1], value_states), -2)
                key_states = self.k_cache(key_states, 2, token_idx)
                value_states = self.v_cache(value_states, 2, token_idx)
                past_key_value = (self.k_cache.get_shape(), self.v_cache.get_shape())
            else:
                if past_key_value is None:
                    past_key = torch.zeros(
                        key_states.shape,
                        dtype=self.get_k_proj_weight_dtype()
                        if self.get_k_proj_weight_dtype() != torch.uint8
                        else key_states.dtype,
                        device=key_states.device,
                    )
                    past_value = torch.zeros(
                        key_states.shape,
                        dtype=self.get_k_proj_weight_dtype()
                        if self.get_k_proj_weight_dtype() != torch.uint8
                        else key_states.dtype,
                        device=key_states.device,
                    )
                    # Return list instead of tuple
                    past_key_value = [past_key, past_value]
                    key_states = self.k_cache.update(past_key_value[0], key_states, 2, token_idx, key_states.shape[-2])
                    value_states = self.v_cache.update(
                        past_key_value[1], value_states, 2, token_idx, value_states.shape[-2]
                    )

                elif (
                    token_idx is not None
                    and num_virtual_tokens is not None
                    and num_virtual_tokens == past_key_value[0].shape[-2]
                ):
                    # prefix tuning case. attach past_key_value to generate first token.
                    key_states = self.k_cache.update(past_key_value[0], key_states, 2, None, -1)
                    value_states = self.v_cache.update(past_key_value[1], value_states, 2, None, -1)
                    past_key_value = (key_states, value_states)
                else:
                    key_states = self.k_cache.update(past_key_value[0], key_states, 2, token_idx, self.inp_seq_len)
                    value_states = self.v_cache.update(past_key_value[1], value_states, 2, token_idx, self.inp_seq_len)

                if token_idx is None:
                    past_key_value = (key_states, value_states)

            if cache_idx is not None and q_len == 1:
                key_states = key_states[:, :, :cache_idx, :]
                value_states = value_states[:, :, :cache_idx, :]
                if attention_mask is not None:
                    attention_mask = attention_mask[:, :, :, :cache_idx]
                kv_seq_len = key_states.shape[-2]
        else:
            past_key_value = None
        fused_scaled_dot_product_attention = get_gaudi_distributed_attention(
            self.fused_scaled_dot_product_attention, self.fused_scaled_dot_product_attention_distributed
        )
        if use_flash_attention and FusedSDPA is not None:
            attn_weights = None
            if q_len == 1:
                # next token
                attn_output = fused_scaled_dot_product_attention(
                    query_states,
                    key_states,
                    value_states,
                    attention_mask,
                    0.0,
                    False,
                    None,
                    "None",
                    False,
                    None,
                    "None",
                )
            else:
                # first token
                softmax_mode = "fast" if flash_attention_fast_softmax else "None"
                if flash_attention_causal_mask:
                    # causal masking on first token requires inputs to be of the same length
                    attn_output = fused_scaled_dot_product_attention(
                        query_states,
                        key_states,
                        value_states,
                        None,
                        0.0,
                        True,
                        None,
                        softmax_mode,
                        flash_attention_recompute,
                        valid_sequence_lengths,
                        "left",
                    )
                else:
                    attn_output = fused_scaled_dot_product_attention(
                        query_states,
                        key_states,
                        value_states,
                        attention_mask,
                        0.0,
                        False,
                        None,
                        softmax_mode,
                        flash_attention_recompute,
                        None,
                        "None",
                    )

        else:
            attn_output, attn_weights = gaudi_eager_attention_forward(
                self,
                query_states,
                key_states,
                value_states,
                attention_mask,
                dropout=0.0 if not self.training else self.attention_dropout,
                scaling=self.scaling,
                attn_softmax_bf16=attn_softmax_bf16,
                input_shape=input_shape,
            )

        attn_output = attn_output.transpose(1, 2).contiguous()
        attn_output = attn_output.reshape(*input_shape, -1).contiguous()
        attn_output = self.o_proj(attn_output)
        if not output_attentions:
            attn_weights = None

        if not reuse_cache and token_idx is not None and cache_idx is not None and q_len == 1:
            # Return only past key value shapes and not the tensors during decode phase (q len is 1)
            # to avoid making past key values as persistent output tensors of HPU graphs.
            past_key_value = (past_key_value[0].shape, past_key_value[1].shape)

        return attn_output, attn_weights, past_key_value

    def attention_all_reduce(self, attn_output):
        if hasattr(self.o_proj, "all_reduce"):
            self.o_proj.all_reduce(attn_output)

    def post_attn_forward(self, attn_output):
        if hasattr(self.o_proj, "post_all_reduce"):
            return self.o_proj.post_all_reduce(attn_output)
        return attn_output


class TPGaudiLlamaAttention(GaudiLlamaAttention, TPModule):
    def __init__(
        self,
        config: LlamaConfig,
        layer_idx: Optional[int] = None,
        group: Optional[ProcessGroup] = None,
    ):
        super().__init__(config, layer_idx)

        assert torch.distributed.is_initialized()
        rank, world_size = distributed.rank_and_world(group)
        assert config.num_attention_heads % world_size == 0, "The number of heads must be divisible by world size"
        self.config = copy.deepcopy(config)

        self.pre_tp_kvheads = config.num_key_value_heads
        GaudiLlamaAttention.__init__(self, self.config, layer_idx)
        self.config.num_attention_heads = self.config.num_attention_heads // world_size
        self.config.num_key_value_heads = (
            (self.config.num_key_value_heads // world_size)
            if self.config.num_key_value_heads > 1
            else self.config.num_key_value_heads
        )
        self.head_dim = config.hidden_size // config.num_attention_heads
        self.hidden_size = self.config.hidden_size // world_size
        self.num_heads = self.config.num_attention_heads

        self.q_proj = torch.nn.Linear(
            config.hidden_size, self.config.num_attention_heads * self.head_dim, bias=config.attention_bias
        )
        self.k_proj = torch.nn.Linear(
            config.hidden_size, self.config.num_key_value_heads * self.head_dim, bias=config.attention_bias
        )
        self.v_proj = torch.nn.Linear(
            config.hidden_size, self.config.num_key_value_heads * self.head_dim, bias=config.attention_bias
        )
        self.o_proj = torch.nn.Linear(
            self.config.num_attention_heads * self.head_dim, config.hidden_size, bias=config.attention_bias
        )
        self.setup_tp(rank, world_size)

    def colwise_param_names(self) -> List[str]:
        colwise_weights = ["q_proj"]
        if self.pre_tp_kvheads != 1:
            colwise_weights.append("k_proj")
            colwise_weights.append("v_proj")
        return colwise_weights

    def rowwise_param_names(self) -> List[str]:
        return ["o_proj"]

    @staticmethod
    def import_module(mha: GaudiLlamaAttention, layer_idx, group: ProcessGroup) -> "TPGaudiLlamaAttention":
        tp_mha = TPGaudiLlamaAttention(config=mha.config, layer_idx=layer_idx, group=group)
        return tp_mha

    def pre_attn_forward(
        self,
        hidden_states: torch.Tensor,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_value: Optional[Cache] = None,
        output_attentions: bool = False,
        use_cache: bool = False,
        cache_position: Optional[torch.LongTensor] = None,
        position_embeddings: Optional[Tuple[torch.Tensor, torch.Tensor]] = None,  # will become mandatory in v4.45
        token_idx: Optional[torch.Tensor] = None,
        attn_softmax_bf16: Optional[bool] = False,
        reuse_cache: Optional[bool] = False,
        use_flash_attention: Optional[bool] = False,
        flash_attention_recompute: Optional[bool] = False,
        flash_attention_causal_mask: Optional[bool] = False,
        flash_attention_fast_softmax: Optional[bool] = False,
        cache_idx: int = None,
        **kwargs,
    ) -> Tuple[torch.Tensor, Optional[torch.Tensor], Optional[Tuple[torch.Tensor]]]:
        hidden_states, attn_weights, present_key_value = GaudiLlamaAttention.pre_attn_forward(
            self,
            hidden_states=hidden_states,
            attention_mask=attention_mask,
            position_ids=position_ids,
            past_key_value=past_key_value,
            output_attentions=output_attentions,
            use_cache=use_cache,
            cache_position=cache_position,
            position_embeddings=position_embeddings,
            token_idx=token_idx,
            attn_softmax_bf16=attn_softmax_bf16,
            reuse_cache=reuse_cache,
            use_flash_attention=use_flash_attention,
            flash_attention_recompute=flash_attention_recompute,
            flash_attention_causal_mask=flash_attention_causal_mask,
            flash_attention_fast_softmax=flash_attention_fast_softmax,
            cache_idx=cache_idx,
            **kwargs,
        )

        hidden_states = reduce_from_tensor_model_parallel_region(hidden_states)
        return hidden_states, attn_weights, present_key_value


class GaudiLlamaDecoderLayer(LlamaDecoderLayer):
    def __init__(self, config: LlamaConfig, layer_idx: int):
        super(LlamaDecoderLayer, self).__init__()
        self.hidden_size = config.hidden_size
        self.self_attn = GaudiLlamaAttention(config=config, layer_idx=layer_idx)

        self.mlp = GaudiLlamaMLP(config)
        self.input_layernorm = LlamaRMSNorm(config.hidden_size, eps=config.rms_norm_eps)
        self.post_attention_layernorm = LlamaRMSNorm(config.hidden_size, eps=config.rms_norm_eps)

    def allocate_kv_cache(self, batch_size, max_seq_len, inp_seq_len):
        self.self_attn.allocate_kv_cache(batch_size, max_seq_len, inp_seq_len)

    def reorder_kv_cache(self, beam_idx: torch.LongTensor):
        return self.self_attn.reorder_kv_cache(beam_idx)

    def update_sincos_cache(self, seq_len):
        self.self_attn.update_sincos_cache(seq_len)

    def forward(
        self,
        hidden_states: torch.Tensor,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_value: Optional[Cache] = None,
        output_attentions: Optional[bool] = False,
        use_cache: Optional[bool] = False,
        cache_position: Optional[torch.LongTensor] = None,
        position_embeddings: Optional[Tuple[torch.Tensor, torch.Tensor]] = None,  # necessary, but kept here for BC
        token_idx: Optional[torch.Tensor] = None,
        attn_softmax_bf16: Optional[bool] = False,
        reuse_cache: Optional[bool] = False,
        use_flash_attention: Optional[bool] = False,
        flash_attention_recompute: Optional[bool] = False,
        flash_attention_causal_mask: Optional[bool] = False,
        flash_attention_fast_softmax: Optional[bool] = False,
        valid_sequence_lengths: Optional[torch.Tensor] = None,
        cache_idx: int = None,
        num_virtual_tokens: int = None,
        attn_batch_split: int = 1,
        prev_layer_residual: Optional[torch.Tensor] = None,
        **kwargs,
    ) -> Tuple[torch.FloatTensor, Optional[Tuple[torch.FloatTensor, torch.FloatTensor]]]:
        """
        Copied from LlamaDecoderLayer.forward: https://github.com/huggingface/transformers/blob/main/src/transformers/models/llama/modeling_llama.py
        The only differences are:
        - add new args token_idx
        - add new args attn_softmax_bf16
        - add new args reuse_cache
        - add new args use_flash_attention
        - add new arg flash_attention_recompute
        - add new arg flash_attention_causal_mask
        - add new arg flash_attention_fast_softmax
        """
        if attn_batch_split > 1 and past_key_value is None:
            # Calculate split sizes to handle cases where batch size is not divisible by attn_batch_split
            batch_size = attention_mask.size(0)
            base_split_size = batch_size // attn_batch_split
            remainder = batch_size % attn_batch_split

            split_sizes = [base_split_size + 1 if i < remainder else base_split_size for i in range(attn_batch_split)]

            # Split tensors using the calculated sizes
            sub_attention_mask = torch.split(attention_mask, split_sizes, dim=0)
            sub_position_ids = torch.split(position_ids, split_sizes, dim=0)
            sub_valid_sequence_lengths = torch.split(valid_sequence_lengths, split_sizes, dim=0)
            split_attn_weights = []
            split_present_key_values = []
            split_hidden_states = [None] * attn_batch_split
            residual = [None] * attn_batch_split

            for i in range(attn_batch_split):
                split_hidden_states[i] = hidden_states[i]
                if self.self_attn.layer_idx != 0:
                    # Add the residual from the previous layer
                    split_hidden_states[i] = self.post_mlp(hidden_states[i], prev_layer_residual[i])

                residual[i] = split_hidden_states[i]
                split_hidden_states[i], self_attn_weights, present_key_value = self.pre_attn(
                    hidden_states=split_hidden_states[i],
                    attention_mask=sub_attention_mask[i],
                    position_ids=sub_position_ids[i],
                    past_key_value=past_key_value,
                    output_attentions=output_attentions,
                    use_cache=use_cache,
                    cache_position=cache_position,
                    position_embeddings=position_embeddings,
                    token_idx=token_idx,
                    attn_softmax_bf16=attn_softmax_bf16,
                    reuse_cache=reuse_cache,
                    use_flash_attention=use_flash_attention,
                    flash_attention_recompute=flash_attention_recompute,
                    flash_attention_causal_mask=flash_attention_causal_mask,
                    flash_attention_fast_softmax=flash_attention_fast_softmax,
                    valid_sequence_lengths=sub_valid_sequence_lengths[i],
                    cache_idx=cache_idx,
                    num_virtual_tokens=num_virtual_tokens,
                    **kwargs,
                )
                self.self_attn.attention_all_reduce(split_hidden_states[i])
                if output_attentions:
                    split_attn_weights.append(self_attn_weights)
                if use_cache:
                    split_present_key_values.append(present_key_value)

            self_attn_weights = torch.cat(split_attn_weights, dim=0) if split_attn_weights else None
            present_key_value = [torch.cat(tensors, dim=0) for tensors in zip(*split_present_key_values)]

            int_residual_splits = []
            for i in range(attn_batch_split):
                split_hidden_states[i], int_residual = self.post_attn_pre_mlp(split_hidden_states[i], residual[i])
                self.mlp.mlp_all_reduce(split_hidden_states[i])
                int_residual_splits.append(int_residual)

            if self.self_attn.layer_idx == (self.self_attn.config.num_hidden_layers - 1):
                for i in range(attn_batch_split):
                    split_hidden_states[i] = self.post_mlp(split_hidden_states[i], int_residual_splits[i])

            hidden_states = split_hidden_states

        else:
            residual = hidden_states
            hidden_states, self_attn_weights, present_key_value = self.pre_attn(
                hidden_states=hidden_states,
                attention_mask=attention_mask,
                position_ids=position_ids,
                past_key_value=past_key_value,
                output_attentions=output_attentions,
                use_cache=use_cache,
                cache_position=cache_position,
                position_embeddings=position_embeddings,
                token_idx=token_idx,
                attn_softmax_bf16=attn_softmax_bf16,
                reuse_cache=reuse_cache,
                use_flash_attention=use_flash_attention,
                flash_attention_recompute=flash_attention_recompute,
                flash_attention_causal_mask=flash_attention_causal_mask,
                flash_attention_fast_softmax=flash_attention_fast_softmax,
                valid_sequence_lengths=valid_sequence_lengths,
                cache_idx=cache_idx,
                num_virtual_tokens=num_virtual_tokens,
                **kwargs,
            )
            self.self_attn.attention_all_reduce(hidden_states)
            hidden_states, residual = self.post_attn_pre_mlp(hidden_states, residual)
            self.mlp.mlp_all_reduce(hidden_states)
            hidden_states = self.post_mlp(hidden_states, residual)

        outputs = (hidden_states,)
        if output_attentions:
            outputs += (self_attn_weights,)
        if use_cache:
            outputs += (present_key_value,)
        # Store the residual splits to add them in the beginning of the next layer
        if attn_batch_split > 1 and past_key_value is None:
            outputs += (int_residual_splits,)

        return outputs

    def pre_attn(
        self,
        hidden_states: torch.Tensor,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_value: Optional[Tuple[torch.Tensor]] = None,
        output_attentions: Optional[bool] = False,
        use_cache: Optional[bool] = False,
        cache_position: Optional[torch.LongTensor] = None,
        position_embeddings: Optional[Tuple[torch.Tensor, torch.Tensor]] = None,  # will become mandatory in v4.45
        token_idx: Optional[torch.Tensor] = None,
        attn_softmax_bf16: Optional[bool] = False,
        reuse_cache: Optional[bool] = False,
        use_flash_attention: Optional[bool] = False,
        flash_attention_recompute: Optional[bool] = False,
        flash_attention_causal_mask: Optional[bool] = False,
        flash_attention_fast_softmax: Optional[bool] = False,
        valid_sequence_lengths: Optional[torch.Tensor] = None,
        cache_idx: int = None,
        num_virtual_tokens: int = None,
    ) -> Tuple[torch.FloatTensor, Optional[Tuple[torch.FloatTensor, torch.FloatTensor]]]:
        hidden_states = self.input_layernorm(hidden_states)
        hidden_states, attn_weights, present_key_value = self.self_attn.pre_attn_forward(
            hidden_states=hidden_states,
            attention_mask=attention_mask,
            position_ids=position_ids,
            past_key_value=past_key_value,
            output_attentions=output_attentions,
            use_cache=use_cache,
            cache_position=cache_position,
            position_embeddings=position_embeddings,
            token_idx=token_idx,
            attn_softmax_bf16=attn_softmax_bf16,
            reuse_cache=reuse_cache,
            use_flash_attention=use_flash_attention,
            flash_attention_recompute=flash_attention_recompute,
            flash_attention_causal_mask=flash_attention_causal_mask,
            flash_attention_fast_softmax=flash_attention_fast_softmax,
            valid_sequence_lengths=valid_sequence_lengths,
            cache_idx=cache_idx,
            num_virtual_tokens=num_virtual_tokens,
        )

        return hidden_states, attn_weights, present_key_value

    def post_attn_pre_mlp(self, hidden_states, residual):
        hidden_states = self.self_attn.post_attn_forward(hidden_states)

        if self.training:
            hidden_states = hidden_states + residual
            residual = hidden_states
        else:
            residual.add_(hidden_states)
            hidden_states = residual

        hidden_states = self.post_attention_layernorm(hidden_states)

        hidden_states = self.mlp.pre_mlp_forward(hidden_states)
        return hidden_states, residual

    def post_mlp(self, hidden_states, residual):
        hidden_states = self.mlp.post_mlp_forward(hidden_states)

        if self.training:
            hidden_states = hidden_states + residual
        else:
            residual.add_(hidden_states)
            hidden_states = residual

        return hidden_states


class GaudiLlamaModel(LlamaModel):
    """
    Copied from https://github.com/huggingface/transformers/blob/v4.38.2/src/transformers/models/llama/modeling_llama.py#L909
    """

    def __init__(self, config: LlamaConfig):
        """
        Copied from https://github.com/huggingface/transformers/blob/v4.38.2/src/transformers/models/llama/modeling_llama.py#L917
        1. set fill_value to 1 instead of True
        2. add device=self.device
        """
        super(LlamaModel, self).__init__(config)
        self.padding_idx = config.pad_token_id
        self.vocab_size = config.vocab_size
        self.embed_tokens = torch.nn.Embedding(config.vocab_size, config.hidden_size, self.padding_idx)
        layers = []
        for layer_idx in range(config.num_hidden_layers):
            layer = GaudiLlamaDecoderLayer(config, layer_idx)
            if hasattr(config, "parallel_strategy") and config.parallel_strategy is not None:
                layer = config.parallel_strategy.distribute_layer(layer, layer_idx)
            layers.append(layer)
        self.layers = torch.nn.ModuleList(layers)
        # parallel_strategy is not JSON serializable
        config.parallel_strategy = None

        self.norm = LlamaRMSNorm(config.hidden_size, eps=config.rms_norm_eps)
        self.gradient_checkpointing = False

        # Initialize weights and apply final processing
        self.post_init()

    def allocate_kv_cache(self, batch_size, max_seq_len, inp_seq_len):
        for layer in self.layers:
            layer.allocate_kv_cache(batch_size, max_seq_len, inp_seq_len)

    def reorder_kv_cache(self, beam_idx: torch.LongTensor):
        return tuple(layer.reorder_kv_cache(beam_idx) for layer in self.layers)

    def update_sincos_cache(self, seq_len):
        for layer in self.layers:
            layer.update_sincos_cache(seq_len)

    def forward(
        self,
        input_ids: Optional[torch.LongTensor] = None,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_values: Optional[Union[Cache, List[torch.FloatTensor]]] = None,
        inputs_embeds: Optional[torch.FloatTensor] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        cache_position: Optional[torch.LongTensor] = None,
        token_idx: Optional[torch.Tensor] = None,
        attn_softmax_bf16: Optional[bool] = False,
        reuse_cache: Optional[bool] = False,
        use_flash_attention: Optional[bool] = False,
        flash_attention_recompute: Optional[bool] = False,
        flash_attention_causal_mask: Optional[bool] = False,
        flash_attention_fast_softmax: Optional[bool] = False,
        valid_sequence_lengths: torch.Tensor = None,
        cache_idx: int = None,
        lazy_mode: Optional[bool] = True,
        num_virtual_tokens: int = None,
        attn_batch_split: int = 1,
        **kwargs,
    ) -> BaseModelOutputWithPast:
        """
        Copied from LlamaModel.forward: https://github.com/huggingface/transformers/blob/main/src/transformers/models/llama/modeling_llama.py
        The only differences are:
        - add new args token_idx
        - add new args attn_softmax_bf16
        - add new args reuse_cache
        - add new args use_flash_attention
        - add new arg flash_attention_recompute
        - add new arg flash_attention_causal_mask
        - add new arg flash_attention_fast_softmax
        - add new arg lazy_mode
        """
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        use_cache = use_cache if use_cache is not None else self.config.use_cache

        if (input_ids is None) ^ (inputs_embeds is not None):
            raise ValueError("You must specify exactly one of input_ids or inputs_embeds")
        elif input_ids is not None:
            batch_size, seq_length = input_ids.shape[:2]
        elif inputs_embeds is not None:
            batch_size, seq_length = inputs_embeds.shape[:2]
        else:
            raise ValueError("You have to specify either input_ids or inputs_embeds")

        if hasattr(self.config, "use_fused_rope") and self.config.use_fused_rope is False:
            global has_fused_rope
            has_fused_rope = False
        if hasattr(self.config, "use_fused_rms_norm") and self.config.use_fused_rms_norm is False:
            global has_fused_rms_norm
            has_fused_rms_norm = False

        if self.gradient_checkpointing and self.training and use_cache:
            logger.warning_once(
                "`use_cache=True` is incompatible with gradient checkpointing. Setting `use_cache=False`."
            )
            use_cache = False

        if inputs_embeds is None:
            inputs_embeds = self.embed_tokens(input_ids)

        ignore_cache_position = True  # Ignoring cache position for HPU
        use_new_cache = False  # Ignoring new Cache path for HPU

        past_seen_tokens = 0

        if past_key_values is not None and use_cache:  # kept for BC (cache positions)
            if reuse_cache:
                if isinstance(past_key_values[0][0], torch.Tensor):
                    past_seen_tokens = past_key_values[0][0].shape[2]
                else:
                    past_seen_tokens = past_key_values[0][0][2]
            else:
                if use_new_cache:
                    if not isinstance(past_key_values, StaticCache):
                        past_key_values = DynamicCache.from_legacy_cache(past_key_values)
                    past_seen_tokens = past_key_values.get_seq_length()
                else:
                    past_seen_tokens = past_key_values[0][0].shape[2]

        if ignore_cache_position is False:
            if cache_position is None:
                past_seen_tokens = past_key_values.get_seq_length() if past_key_values is not None else 0
                cache_position = torch.arange(
                    past_seen_tokens, past_seen_tokens + inputs_embeds.shape[1], device=inputs_embeds.device
                )
            if position_ids is None and cache_position:
                position_ids = cache_position.unsqueeze(0)
        else:
            if position_ids is None:
                position_ids = torch.arange(
                    past_seen_tokens, seq_length + past_seen_tokens, dtype=torch.long, device=inputs_embeds.device
                )
                position_ids = position_ids.unsqueeze(0)
            cache_position = None

        # HPU specific mask generation
        if ignore_cache_position:
            causal_mask = _gaudi_prepare_4d_causal_attention_mask(
                attention_mask,
                input_ids.shape if input_ids is not None else (batch_size, seq_length),
                inputs_embeds,
                past_seen_tokens,
            )
        else:
            causal_mask = self._update_causal_mask(attention_mask, inputs_embeds, cache_position, past_seen_tokens)

        # embed positions
        hidden_states = inputs_embeds

        # create position embeddings to be shared across the decoder layers
        position_embeddings = None  # self.rotary_emb(hidden_states, position_ids)

        # decoder layers
        all_hidden_states = () if output_hidden_states else None
        all_self_attns = () if output_attentions else None
        next_decoder_cache = () if not use_new_cache else None

        if lazy_mode:
            htcore.mark_step()

        split_prompt = False
        prev_layer_residual = None
        if attn_batch_split > 1 and past_key_values is None:
            # Calculate split sizes to handle cases where batch size is not divisible by attn_batch_split
            batch_size = hidden_states.size(0)
            base_split_size = batch_size // attn_batch_split
            remainder = batch_size % attn_batch_split
            split_sizes = [base_split_size + 1 if i < remainder else base_split_size for i in range(attn_batch_split)]
            # Split tensors using the calculated sizes
            hidden_states_split = torch.split(hidden_states, split_sizes, dim=0)
            split_prompt = True

        for layer_idx, decoder_layer in enumerate(self.layers[: self.config.num_hidden_layers]):
            if (
                lazy_mode
                and not self.training
                and (torch.distributed.is_initialized() is False or torch.distributed.get_world_size() == 1)
            ):
                htcore.mark_step()

            if output_hidden_states:
                all_hidden_states += (hidden_states,)

            if self.gradient_checkpointing and self.training:
                layer_outputs = self._gradient_checkpointing_func(
                    partial(decoder_layer.__call__, **kwargs),
                    hidden_states,
                    causal_mask,
                    position_ids,
                    past_key_values,
                    output_attentions,
                    use_cache,
                    cache_position,
                    position_embeddings,
                    None,
                    attn_softmax_bf16,
                    False,
                    use_flash_attention,
                    flash_attention_recompute,
                    flash_attention_causal_mask,
                    flash_attention_fast_softmax,
                    valid_sequence_lengths,
                    None,
                )
                hidden_states = layer_outputs[0]
            else:
                # Calling the layer with positional arguments
                # This is a workaround for an issue with DeepSpeed where
                # it cannot handle keyword arguments and throws a RuntimError
                use_prev_layer_residual = attn_batch_split > 1 and past_key_values is None
                layer_prev_layer_residual = prev_layer_residual if use_prev_layer_residual else None
                layer_hidden_states = hidden_states_split if split_prompt else hidden_states
                past_key_value = None if past_key_values is None else past_key_values[layer_idx]
                layer_outputs = decoder_layer(
                    layer_hidden_states,
                    causal_mask,
                    position_ids,
                    past_key_value,
                    output_attentions,
                    use_cache,
                    cache_position,
                    position_embeddings,
                    token_idx,
                    attn_softmax_bf16,
                    reuse_cache,
                    use_flash_attention,
                    flash_attention_recompute,
                    flash_attention_causal_mask,
                    flash_attention_fast_softmax,
                    valid_sequence_lengths,
                    cache_idx,
                    num_virtual_tokens,
                    attn_batch_split,
                    layer_prev_layer_residual,
                )
                if use_prev_layer_residual:
                    index = 1 + int(use_cache) + int(output_attentions)
                    prev_layer_residual = layer_outputs[index]
                if split_prompt:
                    hidden_states_split = layer_outputs[0]
                else:
                    hidden_states = layer_outputs[0]

            if use_cache:
                next_decoder_cache += (layer_outputs[2 if output_attentions else 1],)

            if output_attentions:
                all_self_attns += (layer_outputs[1],)

        hidden_states = self.norm(hidden_states)

        # add hidden states from the last decoder layer
        if output_hidden_states:
            all_hidden_states += (hidden_states,)

        next_cache = next_decoder_cache if use_cache else None
        if not use_new_cache and isinstance(next_cache, Cache):
            next_cache = next_cache.to_legacy_cache()

        return BaseModelOutputWithPast(
            last_hidden_state=hidden_states,
            past_key_values=next_cache,
            hidden_states=all_hidden_states,
            attentions=all_self_attns,
        )


class GaudiLlamaForCausalLM(LlamaForCausalLM):
    """
    Inherits from LlamaForCausalLM: https://github.com/huggingface/transformers/blob/main/src/transformers/models/llama/modeling_llama.py
    The only differences are:
    - add new args token_idx
    - add token_idx into model_inputs
    - from step2 when enable KV cache, slice next_input_ids from input_ids base on the token_idx
    - from step2 when enable KV cache, slice next_position_ids from position_ids base on the token_idx
    - add new args attn_softmax_bf16
    - add new args reuse_cache
    """

    def __init__(self, config, parallel_strategy: DistributedStrategy = NoOpStrategy):
        config.parallel_strategy = parallel_strategy
        super().__init__(config)
        if parallel_state.sequence_parallel_is_initialized() and parallel_state.get_sequence_parallel_world_size() > 1:
            from ....distributed.contextparallel import ForCausalLMContextParallelLoss

            self._loss_function = ForCausalLMContextParallelLoss

    def allocate_kv_cache(self, batch_size, max_seq_len, inp_seq_len):
        self.model.allocate_kv_cache(batch_size, max_seq_len, inp_seq_len)

    def reorder_kv_cache(self, beam_idx: torch.LongTensor):
        return self.model.reorder_kv_cache(beam_idx)

    def update_sincos_cache(self, seq_len):
        self.model.update_sincos_cache(seq_len)

    def forward(
        self,
        input_ids: Optional[torch.LongTensor] = None,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_values: Optional[Union[Cache, List[torch.FloatTensor]]] = None,
        inputs_embeds: Optional[torch.FloatTensor] = None,
        labels: Optional[torch.LongTensor] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        cache_position: Optional[torch.LongTensor] = None,
        logits_to_keep: Union[int, torch.Tensor] = 0,
        token_idx: Optional[torch.Tensor] = None,
        trim_logits: Optional[bool] = False,
        attn_softmax_bf16: Optional[bool] = False,
        reuse_cache: Optional[bool] = False,
        use_flash_attention: Optional[bool] = False,
        flash_attention_recompute: Optional[bool] = False,
        flash_attention_causal_mask: Optional[bool] = False,
        flash_attention_fast_softmax: Optional[bool] = False,
        valid_sequence_lengths: torch.Tensor = None,
        cache_idx: int = None,
        lazy_mode: Optional[bool] = True,
        num_virtual_tokens: int = None,
        attn_batch_split: int = 1,
        **kwargs: Unpack[KwargsForCausalLM],
    ) -> CausalLMOutputWithPast:
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        if self.generation_config.use_fused_rope is False:
            global has_fused_rope
            has_fused_rope = False

        # decoder outputs consists of (dec_features, layer_state, dec_hidden, dec_attn)
        outputs: BaseModelOutputWithPast = self.model(
            input_ids=input_ids,
            attention_mask=attention_mask,
            position_ids=position_ids,
            past_key_values=past_key_values,
            inputs_embeds=inputs_embeds,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            cache_position=cache_position,
            token_idx=token_idx,
            attn_softmax_bf16=attn_softmax_bf16,
            reuse_cache=reuse_cache,
            use_flash_attention=use_flash_attention,
            flash_attention_recompute=flash_attention_recompute,
            flash_attention_causal_mask=flash_attention_causal_mask,
            flash_attention_fast_softmax=flash_attention_fast_softmax,
            valid_sequence_lengths=valid_sequence_lengths,
            cache_idx=cache_idx,
            lazy_mode=lazy_mode,
            num_virtual_tokens=num_virtual_tokens,
            attn_batch_split=attn_batch_split,
            **kwargs,
        )

        hidden_states = outputs.last_hidden_state
        _, seq_len, _ = hidden_states.shape
        if seq_len > 1 and trim_logits and not self.training:
            if token_idx is not None:
                hidden_states = hidden_states.index_select(1, token_idx - 1)
            else:
                hidden_states = hidden_states[:, -1, :]

        # Only compute necessary logits, and do not upcast them to float if we are not computing the loss
        slice_indices = slice(-logits_to_keep, None) if isinstance(logits_to_keep, int) else logits_to_keep
        logits = self.lm_head(hidden_states[:, slice_indices, :]).float()

        loss = None
        if labels is not None:
            loss = self.loss_function(logits=logits, labels=labels, vocab_size=self.config.vocab_size, **kwargs)

        return CausalLMOutputWithPast(
            loss=loss,
            logits=logits,
            past_key_values=outputs.past_key_values,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
        )

    @staticmethod
    def _reorder_cache(
        past: Tuple[Tuple[torch.Tensor, torch.Tensor], ...], beam_idx: torch.LongTensor
    ) -> Tuple[Tuple[torch.Tensor, torch.Tensor], ...]:
        """
        This function is used to re-order the `past_key_values` cache if [`~PreTrainedModel.beam_search`] or
        [`~PreTrainedModel.beam_sample`] is called. This is required to match `past_key_values` with the correct
        beam_idx at every generation step.

        Output shares the same memory storage as `past`.
        """
        return tuple(
            (
                layer_past[0].index_select(0, beam_idx.to(layer_past[0].device)),
                layer_past[1].index_select(0, beam_idx.to(layer_past[1].device)),
            )
            for layer_past in past
        )

    def prepare_inputs_for_generation(
        self,
        input_ids,
        past_key_values=None,
        attention_mask=None,
        inputs_embeds=None,
        cache_position=None,
        position_ids=None,
        use_cache=True,
        num_logits_to_keep=None,
        token_idx=None,
        **kwargs,
    ):
        reuse_cache = kwargs.get("reuse_cache")
        bucket_internal = kwargs.get("bucket_internal")
        if past_key_values is not None:
            if token_idx is not None:
                idx = token_idx + kwargs.get("inputs_embeds_offset", 0) - 1
                input_ids = torch.index_select(input_ids, 1, idx)
            else:
                if inputs_embeds is not None:  # Exception 1
                    input_ids = input_ids[:, -cache_position.shape[0] :]
                elif (
                    input_ids.shape[1] != cache_position.shape[0]
                ):  # Default case (the "else", a no op, is Exception 2)
                    input_ids = input_ids[:, cache_position]
        elif (reuse_cache or bucket_internal) and token_idx is not None:
            # KV cache is pre allocated with reuse cache or will be padded with bucket internal
            # hence for the 1st token we can slice the inputs till token idx for the fwd pass.
            input_ids = input_ids[:, :token_idx]
            attention_mask = attention_mask[:, :token_idx]

        if attention_mask is not None and position_ids is None:
            # create position_ids on the fly for batch generation
            position_ids = attention_mask.long().cumsum(-1) - 1
            position_ids.masked_fill_(attention_mask == 0, 1)
            if past_key_values:
                if token_idx is not None:
                    position_ids = torch.index_select(position_ids, 1, token_idx - 1)
                else:
                    position_ids = position_ids[:, -input_ids.shape[1] :]
                # This `clone` call is needed to avoid recapturing cuda graphs with `torch.compile`'s  `mode="reduce-overhead`, as otherwise the input `position_ids` would have various stride during the decoding. Here, simply using `.contiguous()` is not sufficient as in the batch size = 1 case, `position_ids` is already contiguous but with varying stride which retriggers a capture.
                position_ids = position_ids.clone(memory_format=torch.contiguous_format)

        # keep cache_position implementation as None for HPU
        cache_position = None

        # if `inputs_embeds` are passed, we only want to use them in the 1st generation step
        if inputs_embeds is not None and past_key_values is None:
            model_inputs = {"inputs_embeds": inputs_embeds}
        else:
            model_inputs = {"input_ids": input_ids.clone(memory_format=torch.contiguous_format)}

        if num_logits_to_keep is not None:
            model_inputs["num_logits_to_keep"] = num_logits_to_keep

        if bucket_internal and reuse_cache is not True:
            # update input with kv cache len to capture padding changes during internal bucketing without cache reuse
            model_inputs["kv_cache_len"] = kwargs.get("kv_cache_len")

        model_inputs.update(
            {
                "position_ids": position_ids,
                "cache_position": cache_position,
                "past_key_values": past_key_values,
                "use_cache": use_cache,
                "attention_mask": attention_mask,
                "token_idx": token_idx,
                "trim_logits": kwargs.get("trim_logits"),
                "attn_softmax_bf16": kwargs.get("attn_softmax_bf16"),
                "reuse_cache": reuse_cache,
                "use_flash_attention": kwargs.get("use_flash_attention"),
                "flash_attention_recompute": kwargs.get("flash_attention_recompute"),
                "flash_attention_causal_mask": kwargs.get("flash_attention_causal_mask"),
                "flash_attention_fast_softmax": kwargs.get("flash_attention_fast_softmax"),
                "valid_sequence_lengths": kwargs.get("valid_sequence_lengths"),
                "cache_idx": kwargs.get("cache_idx"),
                "lazy_mode": kwargs.get("lazy_mode"),
                "num_virtual_tokens": kwargs.get("num_virtual_tokens"),
                "attn_batch_split": kwargs.get("attn_batch_split"),
            }
        )
        return model_inputs

    # Transformer4.43 use new Cache mechanism while Gaudi is not.
    # Adding _reorder_cache back to support HPU.
    @staticmethod
    def _reorder_cache(past_key_values, beam_idx):
        reordered_past = ()
        for layer_past in past_key_values:
            reordered_past += (
                tuple(past_state.index_select(0, beam_idx.to(past_state.device)) for past_state in layer_past),
            )
        return reordered_past


def apply_customized_rope(q, k, cos, sin, position_ids, training=True):
    if q.device.type == "hpu" and has_fused_rope:
        return apply_customized_rope_module(q, k, cos, sin, position_ids, training)
    else:
        return apply_rotary_pos_emb(q, k, cos[position_ids], sin[position_ids])
