import dataclasses
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Type, Union

import equinox as eqx
import jax
import jax.numpy as jnp
import jax.random as jrandom
from transformers import PretrainedConfig
from transformers import PretrainedConfig as HfConfig

import haliax as hax
import haliax.jax_utils
import haliax.nn as hnn
from haliax import Axis, AxisSpec, NamedArray
from haliax.jax_utils import named_call

from levanter.compat.hf_checkpoints import HFCheckpointConverter, LmWithHfSerializationMixin
from levanter.compat.torch_serialization import (
    StateDict,
    StateDictSerializationMixin,
    apply_prefix,
    flatten_linear_layers,
    unflatten_linear_layers,
)
from levanter.models.attention import AttnMask, materialize_mask
from levanter.models.gpt2 import ACT2FN, Gpt2Config, Gpt2Transformer
from levanter.models.lm_model import LmConfig
from levanter.utils.py_utils import cached_classproperty


@LmConfig.register_subclass("backpack")
@dataclass(frozen=True)
class BackpackConfig(Gpt2Config):
    # Backpack-specific terms
    num_senses: int = 16
    sense_intermediate_scale: int = 4

    @property
    def model_type(self) -> Type["BackpackLMHeadModel"]:
        return BackpackLMHeadModel

    @cached_classproperty
    def default_hf_checkpoint_converter(cls) -> HFCheckpointConverter["BackpackConfig"]:  # type: ignore
        # We trust this code because it's in our hub repo
        return HFCheckpointConverter(
            cls, "stanford-crfm/levanter-backpack-1b@9face7bd6182155fe3f1a6a5a14ca1c4810bb079", trust_remote_code=True
        )

    # Axes
    SenseHeadDim = property(lambda self: Axis(name="head_dim", size=self.hidden_dim // self.num_senses))
    Senses = property(lambda self: Axis(name="senses", size=self.num_senses))
    SenseIntermediate = property(
        lambda self: Axis(name="concat_senses", size=self.sense_intermediate_scale * self.hidden_dim)
    )

    def to_hf_config(self, vocab_size, config_overrides=None):
        if config_overrides is None:
            config_overrides = {}

        return PretrainedConfig(
            vocab_size=vocab_size,
            n_positions=self.seq_len,
            n_layer=self.num_layers,
            n_head=self.num_heads,
            n_embd=self.hidden_dim,
            initializer_range=self.initializer_range,
            attn_pdrop=self.attn_pdrop,
            embd_pdrop=self.embed_pdrop,
            resid_pdrop=self.resid_pdrop,
            layer_norm_epsilon=self.layer_norm_epsilon,
            activation_function=self.activation_function,
            scale_attn_by_inverse_layer_idx=self.scale_attn_by_inverse_layer_idx,
            reorder_and_upcast_attn=self.upcast_attn,
            num_senses=self.num_senses,
            sense_intermediate_scale=self.sense_intermediate_scale,
            **config_overrides,
        )

    @classmethod
    def from_hf_config(cls, hf_config: HfConfig):
        return cls(
            seq_len=hf_config.n_positions,
            num_layers=hf_config.n_layer,
            num_heads=hf_config.n_head,
            hidden_dim=hf_config.n_embd,
            initializer_range=hf_config.initializer_range,
            attn_pdrop=hf_config.attn_pdrop,
            embed_pdrop=hf_config.embd_pdrop,
            resid_pdrop=hf_config.resid_pdrop,
            layer_norm_epsilon=hf_config.layer_norm_epsilon,
            activation_function=hf_config.activation_function,
            scale_attn_by_inverse_layer_idx=hf_config.scale_attn_by_inverse_layer_idx,
            upcast_attn=hf_config.reorder_and_upcast_attn,
            num_senses=hf_config.num_senses,
            sense_intermediate_scale=hf_config.sense_intermediate_scale,
        )


class BackpackMlp(eqx.Module, StateDictSerializationMixin):
    c_fc: hnn.Linear  # projection from Embed to Intermediate (typically 4x Embed)
    c_proj: hnn.Linear  # projection from Intermediate to Embed
    act: Callable = eqx.static_field()

    @staticmethod
    def init(
        Embed: Axis,
        Mlp: Axis,
        Out: AxisSpec,
        activation_fn: Union[str, Callable],
        *,
        key,
        use_bias: bool = True,
    ) -> "BackpackMlp":
        k_fc, k_proj = jrandom.split(key, 2)
        c_fc = hnn.Linear.init(Out=Mlp, In=Embed, key=k_fc, use_bias=use_bias)
        c_proj = hnn.Linear.init(Out=Out, In=Mlp, key=k_proj, use_bias=use_bias)
        if isinstance(activation_fn, str):
            activation_fn = ACT2FN[activation_fn]
        act = activation_fn  # type: ignore

        return BackpackMlp(c_fc=c_fc, c_proj=c_proj, act=act)

    @named_call
    def __call__(self, x: NamedArray):
        x = self.c_fc(x)
        x = self.act(x)
        x = self.c_proj(x)
        return x

    def from_state_dict(self, state_dict: StateDict, prefix: Optional[str] = None) -> "BackpackMlp":
        d = {}
        d.update(
            unflatten_linear_layers(
                apply_prefix(prefix, "c_proj"), state_dict, self.c_proj, out_dims_first_in_dict=False
            )
        )
        d.update(
            unflatten_linear_layers(apply_prefix(prefix, "c_fc"), state_dict, self.c_fc, out_dims_first_in_dict=False)
        )
        return super().from_state_dict(d, prefix)

    def update_state_dict(self, state_dict: StateDict, prefix: Optional[str] = None) -> StateDict:
        my_dict: StateDict = {}
        super().update_state_dict(my_dict, prefix)

        my_dict.update(
            flatten_linear_layers(apply_prefix(prefix, "c_proj"), self.c_proj, out_dims_first_in_dict=False)
        )
        my_dict.update(flatten_linear_layers(apply_prefix(prefix, "c_fc"), self.c_fc, out_dims_first_in_dict=False))

        state_dict.update(my_dict)
        return state_dict


class WeightsOnlyAttention(StateDictSerializationMixin, eqx.Module):
    """
    Changes from Gpt2Attention:
    1. No projection; it returns the attention weights
    2. Use SenseHeadDim instead of HeadDim, use Senses instead of Heads
    """

    # No projection
    config: Gpt2Config = eqx.static_field()

    c_attn: hnn.Linear  # input projection from [embed] -> [(q, k, v), heads, head_dim]
    dropout: hnn.Dropout

    @staticmethod
    def init(config: Gpt2Config, *, key) -> "WeightsOnlyAttention":
        Qk = Axis("qk", size=2)
        use_bias = config.use_bias
        Embed = config.Embed

        k_c, _ = jrandom.split(key, 2)
        c_attn = hnn.Linear.init(In=Embed, Out=(Qk, config.Senses, config.SenseHeadDim), key=k_c, use_bias=use_bias)
        dropout = hnn.Dropout(config.attn_pdrop)

        return WeightsOnlyAttention(config, c_attn, dropout)

    @named_call
    def __call__(self, x: NamedArray, mask: Optional[AttnMask], layer_idx, *, key):
        qk_out = self.c_attn(x)
        q, k = qk_out.unbind("qk")

        # Rename k's Pos as haliax doesn't support unnamed axes or duplicate axes
        k = k.rename({"position": "key_position"})

        # mistral tweak: scale norms by 1/sqrt(layer_idx) to prevent blowup
        scale = jax.lax.rsqrt(float(self.config.SenseHeadDim.size))

        # do this first to help keep FP values small
        q = q * scale

        # mistral tweak: attention scores can overflow FP16, or just be too imprecise, so upcast to FP32
        if self.config.upcast_attn:
            q = q.astype(jnp.float32)
            k = k.astype(jnp.float32)

        attn_scores = hax.dot("head_dim", q, k)

        if mask is not None:
            mask = materialize_mask(mask)
            attn_scores = attn_scores + (1.0 - mask) * -1e15

        attn_weights = hnn.softmax(attn_scores, axis="key_position").astype(x.dtype)
        attn_weights = self.dropout(attn_weights, key=key)
        return attn_weights

    def from_state_dict(self, state_dict: StateDict, prefix: Optional[str] = None) -> "WeightsOnlyAttention":
        d = unflatten_linear_layers(
            apply_prefix(prefix, "c_attn"), state_dict, self.c_attn, out_dims_first_in_dict=True
        )
        return super().from_state_dict(d, prefix)

    def update_state_dict(self, state_dict: StateDict, prefix: Optional[str] = None) -> StateDict:
        # need to undo the reshape we did in from_state_dict
        # reminder that everything is vectorized
        my_dict: StateDict = {}
        super().update_state_dict(my_dict, prefix)

        my_dict.update(flatten_linear_layers(apply_prefix(prefix, "c_attn"), self.c_attn, out_dims_first_in_dict=True))

        state_dict.update(my_dict)
        return state_dict


class NoMixBlock(StateDictSerializationMixin, eqx.Module):
    ln_1: hnn.LayerNorm
    ln_2: hnn.LayerNorm
    mlp: BackpackMlp
    resid_dropout1: hnn.Dropout
    resid_dropout2: hnn.Dropout

    @staticmethod
    def init(config: BackpackConfig, *, key) -> "NoMixBlock":
        k_mlp = jrandom.split(key, 1)[0]

        ln_1 = hnn.LayerNorm.init(config.Embed, eps=config.layer_norm_epsilon)
        resid_dropout1 = hnn.Dropout(pdrop=config.resid_pdrop)
        resid_dropout2 = hnn.Dropout(pdrop=config.resid_pdrop)
        ln_2 = hnn.LayerNorm.init(config.Embed, eps=config.layer_norm_epsilon)

        mlp = BackpackMlp.init(
            Embed=config.Embed,
            Mlp=config.Mlp,
            Out=config.Embed,
            activation_fn=config.activation_function,
            key=k_mlp,
            use_bias=config.use_bias,
        )

        return NoMixBlock(ln_1=ln_1, ln_2=ln_2, mlp=mlp, resid_dropout1=resid_dropout1, resid_dropout2=resid_dropout2)

    @named_call
    def __call__(self, hidden_states: NamedArray, residual: NamedArray, *, key):
        k1, k2 = haliax.jax_utils.maybe_rng_split(key, 2)

        residual = self.resid_dropout1(hidden_states, key=k1) + residual
        hidden_states = self.ln_1(residual)
        mlp_out = self.mlp(hidden_states)
        residual = self.resid_dropout2(mlp_out, key=k2) + residual
        hidden_states = self.ln_2(residual)

        return hidden_states


class BackpackSenses(StateDictSerializationMixin, eqx.Module):
    dropout: hnn.Dropout
    block: NoMixBlock
    ln: hnn.LayerNorm
    final_mlp: BackpackMlp

    Pos: Axis = eqx.static_field()

    @staticmethod
    def init(
        config,
        dropout_prob: float,
        *,
        key,
    ):
        k_block, k_mlp = jrandom.split(key, 2)

        dropout = hnn.Dropout(pdrop=dropout_prob)
        block = NoMixBlock.init(config, key=k_block)
        ln = hnn.LayerNorm.init(config.Embed, eps=config.layer_norm_epsilon)
        final_mlp = BackpackMlp.init(
            Embed=config.Embed,
            Mlp=config.SenseIntermediate,
            Out=(config.Senses, config.Embed),
            activation_fn=config.activation_function,
            key=k_mlp,
            use_bias=config.use_bias,
        )

        return BackpackSenses(
            dropout=dropout,
            block=block,
            ln=ln,
            final_mlp=final_mlp,
            Pos=config.Pos,
        )

    @named_call
    def sense_embed(self, input_embeds, *, key):
        hidden_states = self.ln(input_embeds)
        hidden_states = self.block(hidden_states, input_embeds, key=key)
        senses = self.final_mlp(hidden_states)

        return senses


class BackpackGpt2Embeddings(eqx.Module):
    Vocab: Axis = eqx.static_field()
    config: Gpt2Config = eqx.static_field()

    token_embeddings: NamedArray
    position_embeddings: NamedArray
    dropout: hnn.Dropout

    @staticmethod
    def init(Vocab: Axis, config: Gpt2Config, *, key) -> "BackpackGpt2Embeddings":
        k_wte, k_wpe, k_out = jrandom.split(key, 3)

        token_embeddings = hax.random.normal(k_wte, (Vocab, config.Embed)) * config.initializer_range
        position_embeddings = hax.random.normal(k_wpe, (config.Pos, config.Embed)) * (config.initializer_range / 2)
        dropout = hnn.Dropout(pdrop=config.embed_pdrop)

        return BackpackGpt2Embeddings(Vocab, config, token_embeddings, position_embeddings, dropout)

    @named_call
    def embed_input_ids(self, input_ids: NamedArray) -> NamedArray:
        return self.token_embeddings.take("vocab", input_ids)

    @named_call
    def embed(self, input_ids, *, key):
        input_embeds = self.token_embeddings.take("vocab", input_ids)
        position_embeds = self.position_embeddings
        x = input_embeds + position_embeds
        x = self.dropout(x, key=key)

        return x

    def unembed(self, x: NamedArray):
        return hax.dot("embed", x, self.token_embeddings)

    def _state_dict_key_map(self) -> Dict[str, Optional[str]]:
        return {"token_embeddings": "wte.weight", "position_embeddings": "wpe.weight"}

    def resize_embeddings(self, new_size: int, key: Optional[jrandom.PRNGKeyArray] = None):
        new_weights = hax.tree_util.resize_axis(self.token_embeddings, self.Vocab, new_size, key=key)
        return dataclasses.replace(self, Vocab=self.Vocab.resize(new_size), token_embeddings=new_weights)


class BackpackLMHeadModel(eqx.Module, LmWithHfSerializationMixin):
    transformer: Gpt2Transformer
    embeddings: BackpackGpt2Embeddings
    sense_net: BackpackSenses
    kq_selfattention: WeightsOnlyAttention

    @property
    def config(self):
        return self.transformer.config

    @property
    def Vocab(self) -> Axis:
        return self.embeddings.Vocab

    @property
    def Pos(self) -> Axis:
        return self.sense_net.Pos

    @staticmethod
    def init(Vocab: Axis, config: BackpackConfig, *, key):
        k_t, k_embeddings, k_attn = jrandom.split(key, 3)
        transformer = Gpt2Transformer.init(config, key=k_t)

        embeddings = BackpackGpt2Embeddings.init(
            Vocab=Vocab,
            config=config,
            key=k_embeddings,
        )
        sense_net = BackpackSenses.init(
            config=config,
            dropout_prob=config.embed_pdrop,
            key=k_embeddings,
        )
        kq_selfattention = WeightsOnlyAttention.init(
            config=config,
            key=k_attn,
        )

        return BackpackLMHeadModel(
            transformer=transformer,
            embeddings=embeddings,
            sense_net=sense_net,
            kq_selfattention=kq_selfattention,
        )

    @named_call
    def __call__(self, input_ids: NamedArray, attn_mask: Optional[AttnMask] = None, *, key=None) -> NamedArray:
        k_embed, k_transformer, k_senses, k_sa = haliax.jax_utils.maybe_rng_split(key, 4)

        # Compute contextualization weights
        hidden_states = self.embeddings.embed(input_ids, key=k_embed)
        hidden_states = self.transformer(hidden_states, attn_mask, key=k_transformer)
        contextualization_weights = self.kq_selfattention(
            hidden_states, mask=attn_mask, layer_idx=self.config.num_layers, key=k_sa
        )  # (seq, seq, senses)

        ## Compute sense vectors
        sense_input_embeds = self.embeddings.embed_input_ids(input_ids)  # (seq, embed
        sense_vectors = self.sense_net.sense_embed(sense_input_embeds, key=k_senses)  # (seq, senses, embed)
        sense_vectors = sense_vectors.rename({self.Pos: self.config.KeyPos})

        ## Weight-and-sum
        hidden_states = hax.dot(self.config.KeyPos, contextualization_weights, sense_vectors)  # (seq, senses, embed)
        hidden_states = hax.sum(hidden_states, axis=self.config.Senses)

        # Rescale - this is important for large num_senses
        scale = self.config.Senses.size
        hidden_states = hidden_states / scale

        lm_logits = self.embeddings.unembed(hidden_states)

        return lm_logits

    def resize_vocab(self, new_size: int, key: Optional[jrandom.PRNGKeyArray] = None):
        new_embeddings = self.embeddings.resize_embeddings(new_size, key=key)
        return dataclasses.replace(self, embeddings=new_embeddings)

    def _state_dict_key_map(self) -> Dict[str, Optional[str]]:
        return {
            "transformer": "backpack.gpt2_model",
            "embeddings": "backpack.gpt2_model",
            "sense_net": "backpack.sense_network",
            "kq_selfattention": "backpack.sense_weight_net",
        }

    def update_state_dict(self, state_dict: StateDict, prefix: Optional[str] = None) -> StateDict:
        state_dict = super().update_state_dict(state_dict, prefix=prefix)
        # In levanter's implementation, we have a shared embedding matrix for both the word
        # embeddings and the sense embeddings
        state_dict[apply_prefix(prefix, "backpack.word_embeddings.weight")] = state_dict[
            apply_prefix(prefix, "backpack.gpt2_model.wte.weight")
        ]
        state_dict[apply_prefix(prefix, "backpack.position_embeddings.weight")] = state_dict[
            apply_prefix(prefix, "backpack.gpt2_model.wpe.weight")
        ]
        return state_dict
