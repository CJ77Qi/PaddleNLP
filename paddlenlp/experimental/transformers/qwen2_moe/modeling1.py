# Copyright (c) 2024 PaddlePaddle Authors. All Rights Reserved.
# Copyright 2018 The OpenAI Team Authors and HuggingFace Inc. team.
# Copyright (c) 2018, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from __future__ import annotations

import os
from functools import partial

import numpy as np
import paddle
from paddle import nn
from paddle.nn.quant import weight_quantize

from paddlenlp.experimental.transformers.fused_transformer_layers import (
    FusedBlockMultiTransformer,
    FusedBlockMultiTransformerWeightOnly,
    FusedMultiTransformerBase,
    FusedMultiTransformerConfig,
    FusedMultiTransformerWeightOnly,
)
from paddlenlp.experimental.transformers.generation_utils import (
    GenerationBlockInferenceModel,
    GenerationInferenceModel,
)
from paddlenlp.transformers import Qwen2Config, Qwen2PretrainedModel
from paddlenlp.transformers.model_outputs import (  # CausalLMOutputWithCrossAttentions,
    BaseModelOutputWithPast,
    BaseModelOutputWithPastAndCrossAttentions,
    CausalLMOutputWithPast,
)
from paddlenlp.transformers.model_utils import (
    dy2st_nocheck_guard_context,
    register_base_model,
)
from paddlenlp.transformers.qwen2.modeling import Qwen2LMHead, Qwen2PretrainingCriterion
from paddlenlp.utils.log import logger

__all__ = ["Qwen2ForCausalLMInferenceModel", "Qwen2ForCausalLMBlockInferenceModel"]


class FusedQwen2RMSNorm(nn.Layer):
    def __init__(self, config):
        super().__init__()
        self.eps = config.rms_norm_eps
        self.weight = paddle.create_parameter(
            shape=[config.hidden_size],
            dtype=paddle.get_default_dtype(),
            default_initializer=nn.initializer.Constant(1.0),
        )

    def forward(self, x):
        result = paddle.incubate.nn.functional.fused_rms_norm(x, self.weight, None, self.eps, begin_norm_axis=1)
        if isinstance(result, tuple):
            return result[0]
        return result


@register_base_model
class Qwen2InferenceModel(Qwen2PretrainedModel):
    def __init__(self, config: Qwen2Config):
        super(Qwen2PretrainedModel, self).__init__(config)
        self.vocab_size = config.vocab_size
        self.hidden_size = config.hidden_size
        self.num_attention_heads = config.num_attention_heads
        self.num_key_value_heads = config.num_key_value_heads
        self.intermediate_size = config.intermediate_size
        self.num_layers = config.num_hidden_layers
        self.rms_norm_eps = config.rms_norm_eps
        self.max_position_embeddings = config.max_position_embeddings
        self.quant_type = config.quant_type
        self.weight_only_quant_bits = config.weight_only_quant_bits
        self.rope_theta = config.rope_theta

        if self.quant_type is not None and "weight_only_int" in self.quant_type:
            self.use_weight_only = True
        else:
            self.use_weight_only = False

        if self.use_weight_only:
            assert (
                self.quant_type == "weight_only_int8" or self.quant_type == "weight_only_int4"
            ), "Expected quant_type equal to 'weight_only_int8' or 'weight_only_int4', but received {}".format(
                self.quant_type
            )

        self.wte = nn.Embedding(self.vocab_size, self.hidden_size)
        # print(self.vocab_size, self.hidden_size)

        ln_scale_attrs = [paddle.ParamAttr(name="fuseqwen2.{}.ln_scale".format(i)) for i in range(self.num_layers)]
        qkv_weight_attrs = [
            paddle.ParamAttr(
                name="fuseqwen2.{}.qkv_weight".format(i), initializer=paddle.nn.initializer.Constant(value=0)
            )
            for i in range(self.num_layers)
        ]
        qkv_bias_attrs = [paddle.ParamAttr(name="fuseqwen2.{}.qkv_bias".format(i)) for i in range(self.num_layers)]
        out_proj_weight_attrs = [
            paddle.ParamAttr(
                name="fuseqwen2.{}.out_proj_weight".format(i), initializer=paddle.nn.initializer.Constant(value=0)
            )
            for i in range(self.num_layers)
        ]
        ffn_ln_scale_attrs = [
            paddle.ParamAttr(name="fuseqwen2.{}.ffn_ln_scale".format(i)) for i in range(self.num_layers)
        ]
        ffn1_weight_attrs = [
            paddle.ParamAttr(
                name="fuseqwen2.{}.ffn1_weight".format(i), initializer=paddle.nn.initializer.Constant(value=0)
            )
            for i in range(self.num_layers)
        ]
        ffn2_weight_attrs = [
            paddle.ParamAttr(
                name="fuseqwen2.{}.ffn2_weight".format(i), initializer=paddle.nn.initializer.Constant(value=0)
            )
            for i in range(self.num_layers)
        ]

        qkv_weight_scale_attrs = None
        out_proj_weight_scale_attrs = None
        ffn1_weight_scale_attrs = None
        ffn2_weight_scale_attrs = None

        if self.use_weight_only:
            qkv_weight_scale_attrs = [
                paddle.ParamAttr(name="fuseqwen2.{}.qkv_weight_scale".format(i)) for i in range(self.num_layers)
            ]
            out_proj_weight_scale_attrs = [
                paddle.ParamAttr(name="fuseqwen2.{}.out_proj_weight_scale".format(i)) for i in range(self.num_layers)
            ]
            ffn1_weight_scale_attrs = [
                paddle.ParamAttr(name="fuseqwen2.{}.ffn1_weight_scale".format(i)) for i in range(self.num_layers)
            ]
            ffn2_weight_scale_attrs = [
                paddle.ParamAttr(name="fuseqwen2.{}.ffn2_weight_scale".format(i)) for i in range(self.num_layers)
            ]

        transformer_config = FusedMultiTransformerConfig(
            embed_dim=self.hidden_size,
            num_heads=self.num_attention_heads,
            kv_num_heads=self.num_key_value_heads,
            dim_feedforward=self.intermediate_size,
            weight_only_quant_bits=self.weight_only_quant_bits,
            activation="swiglu",
            num_layers=config.num_hidden_layers,
            nranks=1,
            ring_id=-1,
            ln_scale_attrs=ln_scale_attrs,
            qkv_weight_attrs=qkv_weight_attrs,
            qkv_weight_scale_attrs=qkv_weight_scale_attrs,
            linear_weight_attrs=out_proj_weight_attrs,
            linear_weight_scale_attrs=out_proj_weight_scale_attrs,
            ffn_ln_scale_attrs=ffn_ln_scale_attrs,
            ffn1_weight_attrs=ffn1_weight_attrs,
            ffn1_weight_scale_attrs=ffn1_weight_scale_attrs,
            ffn2_weight_attrs=ffn2_weight_attrs,
            ffn2_weight_scale_attrs=ffn2_weight_scale_attrs,
            qkv_bias_attrs=qkv_bias_attrs,
            epsilon=self.rms_norm_eps,
            norm_type="rmsnorm",
            use_neox_rotary_style=True,
        )

        self.set_transformer_block(transformer_config)

        self.ln_f = FusedQwen2RMSNorm(config)

        self.cache_kvs = None
        self.head_dim_shape_tensor = paddle.ones((self.hidden_size // self.num_attention_heads), dtype="int8")

    def set_transformer_block(self, transformer_config):
        if self.use_weight_only:
            self.transformer_block = FusedMultiTransformerWeightOnly(transformer_config)
        else:
            self.transformer_block = FusedMultiTransformerBase(transformer_config)

    def get_input_embeddings(self):
        return self.wte

    def set_input_embeddings(self, value):
        self.wte = value

    @paddle.no_grad()
    def set_state_dict(self, state_dict):
        head_size = self.hidden_size // self.num_attention_heads
        dtype = paddle.get_default_dtype()
        wte_weight = paddle.to_tensor(state_dict["qwen2.embed_tokens.weight"]).cast(dtype)
        ln_f_weight = paddle.to_tensor(state_dict["qwen2.norm.weight"]).cast(self.ln_f.weight.dtype)
        self.wte.weight.set_value(wte_weight)
        self.ln_f.weight.set_value(ln_f_weight)

        # print("wte.weight:", self.wte.weight)
        # print("ln_f.weight:", self.wte.weight)

        # print("qwen2.embed_tokens.weight:", wte_weight)
        # print("qwen2.norm.weight:", ln_f_weight)

        for idx in range(self.num_layers):
            unfused_state_dict = {}
            ln_scale = paddle.to_tensor(state_dict["qwen2.layers.{}.input_layernorm.weight".format(idx)]).cast(
                self.transformer_block.ln_scales[idx].dtype
            )
            self.transformer_block.ln_scales[idx].set_value(ln_scale)

            # print("qwen2.layers.{}.input_layernorm.weight".format(idx), ln_scale)

            unfused_state_dict["qwen2.self_attn.q_proj.weight"] = state_dict[
                "qwen2.layers.{}.self_attn.q_proj.weight".format(idx)
            ]
            unfused_state_dict["qwen2.self_attn.k_proj.weight"] = state_dict[
                "qwen2.layers.{}.self_attn.k_proj.weight".format(idx)
            ]
            unfused_state_dict["qwen2.self_attn.v_proj.weight"] = state_dict[
                "qwen2.layers.{}.self_attn.v_proj.weight".format(idx)
            ]

            # print("qwen2.layers.{}.self_attn.q_proj.weight".format(idx), unfused_state_dict["qwen2.self_attn.q_proj.weight"])
            # print("qwen2.layers.{}.self_attn.k_proj.weight".format(idx), unfused_state_dict["qwen2.self_attn.k_proj.weight"])
            # print("qwen2.layers.{}.self_attn.v_proj.weight".format(idx), unfused_state_dict["qwen2.self_attn.v_proj.weight"])

            concated_qkv_weight = (
                np.concatenate(
                    [
                        unfused_state_dict["qwen2.self_attn.q_proj.weight"],
                        unfused_state_dict["qwen2.self_attn.k_proj.weight"],
                        unfused_state_dict["qwen2.self_attn.v_proj.weight"],
                    ],
                    axis=-1,
                )
                .transpose(1, 0)
                .reshape(
                    (
                        self.num_attention_heads // self.config.tensor_parallel_degree
                        + 2 * self.num_key_value_heads // self.config.tensor_parallel_degree
                    )
                    * (head_size),
                    self.hidden_size,
                )
            )

            qkv_weight = paddle.to_tensor(concated_qkv_weight).cast(dtype)

            if self.use_weight_only:
                qkv_weight = paddle.transpose(qkv_weight, perm=[1, 0])
                qkv_quanted_weight, qkv_weight_scale = weight_quantize(qkv_weight, algo=self.quant_type)
                self.transformer_block.qkv_weights[idx].set_value(qkv_quanted_weight)
                self.transformer_block.qkv_weights_scale[idx].set_value(qkv_weight_scale)
            else:
                self.transformer_block.qkv_weights[idx].set_value(qkv_weight)

            unfused_state_dict["qwen2.self_attn.q_proj.bias"] = state_dict[
                "qwen2.layers.{}.self_attn.q_proj.bias".format(idx)
            ]
            unfused_state_dict["qwen2.self_attn.k_proj.bias"] = state_dict[
                "qwen2.layers.{}.self_attn.k_proj.bias".format(idx)
            ]
            unfused_state_dict["qwen2.self_attn.v_proj.bias"] = state_dict[
                "qwen2.layers.{}.self_attn.v_proj.bias".format(idx)
            ]

            # print("qwen2.layers.{}.self_attn.q_proj.bias".format(idx), unfused_state_dict["qwen2.self_attn.q_proj.bias"])
            # print("qwen2.layers.{}.self_attn.k_proj.bias".format(idx), unfused_state_dict["qwen2.self_attn.k_proj.bias"])
            # print("qwen2.layers.{}.self_attn.v_proj.bias".format(idx), unfused_state_dict["qwen2.self_attn.v_proj.bias"])

            concated_qkv_biases = np.concatenate(
                [
                    unfused_state_dict["qwen2.self_attn.q_proj.bias"],
                    unfused_state_dict["qwen2.self_attn.k_proj.bias"],
                    unfused_state_dict["qwen2.self_attn.v_proj.bias"],
                ],
                axis=-1,
            )
            qkv_bias = paddle.to_tensor(concated_qkv_biases).cast(dtype)
            self.transformer_block.qkv_biases[idx].set_value(qkv_bias)

            linear_weight = paddle.to_tensor(state_dict["qwen2.layers.{}.self_attn.o_proj.weight".format(idx)]).cast(
                dtype
            )

            # print("qwen2.layers.{}.self_attn.o_proj.weight".format(idx), linear_weight)

            if self.use_weight_only:
                linear_quanted_weight, linear_weight_scale = weight_quantize(linear_weight, algo=self.quant_type)
                self.transformer_block.linear_weights[idx].set_value(linear_quanted_weight)
                self.transformer_block.linear_weights_scale[idx].set_value(linear_weight_scale)
            else:
                self.transformer_block.linear_weights[idx].set_value(linear_weight)

            ffn_ln_scale = paddle.to_tensor(
                state_dict["qwen2.layers.{}.post_attention_layernorm.weight".format(idx)],
            ).cast(
                self.transformer_block.ffn_ln_scales[idx].dtype,
            )
            self.transformer_block.ffn_ln_scales[idx].set_value(ffn_ln_scale)

            # print("qwen2.layers.{}.post_attention_layernorm.weight".format(idx), ffn_ln_scale)

            up_weight = paddle.to_tensor(state_dict["qwen2.layers.{}.mlp.up_proj.weight".format(idx)]).cast(dtype)
            gate_weight = paddle.to_tensor(state_dict["qwen2.layers.{}.mlp.gate_proj.weight".format(idx)]).cast(dtype)
            ffn1_weight = paddle.concat(x=[gate_weight, up_weight], axis=-1)
            if self.use_weight_only:
                ffn1_quanted_weight, ffn1_weight_scale = weight_quantize(ffn1_weight, algo=self.quant_type)
                self.transformer_block.ffn1_weights[idx].set_value(ffn1_quanted_weight)
                self.transformer_block.ffn1_weights_scale[idx].set_value(ffn1_weight_scale)
            else:
                self.transformer_block.ffn1_weights[idx].set_value(ffn1_weight)

            ffn2_weight = paddle.to_tensor(state_dict["qwen2.layers.{}.mlp.down_proj.weight".format(idx)]).cast(dtype)
            if self.use_weight_only:
                ffn2_quanted_weight, ffn2_weight_scale = weight_quantize(ffn2_weight, algo=self.quant_type)
                self.transformer_block.ffn2_weights[idx].set_value(ffn2_quanted_weight)
                self.transformer_block.ffn2_weights_scale[idx].set_value(ffn2_weight_scale)
            else:
                self.transformer_block.ffn2_weights[idx].set_value(ffn2_weight)

            # print("qwen2.layers.{}.mlp.up_proj.weight".format(idx), up_weight)
            # print("qwen2.layers.{}.mlp.gate_proj.weight".format(idx), gate_weight)
            # print("qwen2.layers.{}.mlp.down_proj.weight".format(idx), ffn2_weight)

    def remove_padding(self, input_ids, seq_lens_this_time):
        cum_offsets_now = paddle.cumsum(paddle.max(seq_lens_this_time) - seq_lens_this_time)
        token_num = paddle.sum(seq_lens_this_time)
        from paddlenlp_ops import get_padding_offset

        ids_remove_padding, cum_offsets, padding_offset = get_padding_offset(
            input_ids, cum_offsets_now, token_num, seq_lens_this_time
        )
        return ids_remove_padding, padding_offset, cum_offsets

    # This function is a little different from prepare_input_ids_for_generation in paddlenlp/transformers/generation/utils.py,
    # it is used to generate fake input_ids according to inputs_embeds length.
    @staticmethod
    def prepare_input_ids_for_generation(bos_token_id, encoder_output=None):
        batch_size = 1
        seq_len = 1
        if bos_token_id is None:
            raise ValueError("`bos_token_id` should be defined when no " "`input_ids` are provided.")
        if encoder_output is not None:
            batch_size = encoder_output.shape[0]
            seq_len = encoder_output.shape[1]
        return paddle.full([batch_size, seq_len], bos_token_id, dtype="int64")

    def forward(
        self,
        input_ids=None,
        position_ids=None,
        attention_mask=None,
        inputs_embeds=None,
        use_cache=None,
        cache_kvs=None,
        pre_caches=None,
        seq_len_encoder=None,
        seq_len_decoder=None,
        past_key_values=None,
        output_attentions=False,
        output_hidden_states=None,
        return_dict=False,
        **kwargs,
    ):
        # kwargs["cache"] is used used to distinguish between encoder and decoder phase.
        past_key_values = kwargs.get("cache", None)
        is_decoder = past_key_values is not None

        if input_ids is not None and inputs_embeds is not None:
            raise ValueError("You cannot specify both input_ids and inputs_embeds at the same time")
        elif input_ids is None and inputs_embeds is None:
            raise ValueError("You have to specify either input_ids or inputs_embeds")

        # generate a fake input_ids according to inputs_embeds
        # this is usually occurred in img2txt multimodal model when first enter into this forward function.
        if input_ids is None and inputs_embeds is not None:
            input_ids = self.prepare_input_ids_for_generation(self.config.bos_token_id, inputs_embeds)
        if inputs_embeds is not None:
            batch, seq_len, hidden_dim = inputs_embeds.shape
            inputs_embeds = inputs_embeds.reshape([batch * seq_len, hidden_dim])

        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        use_cache = use_cache if use_cache is not None else self.config.use_cache
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        if past_key_values is None:
            past_key_values = tuple([None] * self.config.num_hidden_layers)

        if not is_decoder:
            ids_remove_padding, padding_offset, cum_offsets = self.remove_padding(input_ids, seq_len_encoder)
        else:
            ids_remove_padding = input_ids
            padding_offset = None
            cum_offsets = None

        if inputs_embeds is None:
            inputs_embeds = self.wte(ids_remove_padding)
            # print("input_ids:", input_ids)
            # print("ids_remove_padding:", ids_remove_padding)
            # print("inputs_embeds wte:", inputs_embeds)

        hidden_states = inputs_embeds

        # decoder layers
        presents = () if use_cache else None
        all_hidden_states = () if output_hidden_states else None
        all_self_attentions = () if output_attentions else None

        seq_lens = seq_len_decoder if is_decoder else seq_len_encoder

        position_offset = 0
        if not is_decoder and pre_caches is not None:
            position_offset = 128

        from paddlenlp_ops import fused_get_rotary_embedding

        new_rope = fused_get_rotary_embedding(
            input_ids, position_ids, self.head_dim_shape_tensor, position_offset, self.rope_theta, True
        )
        # print("new_rope:", new_rope)

        with dy2st_nocheck_guard_context():
            hidden_states, _ = self.transformer_block(
                input_ids,
                hidden_states,
                cum_offsets=cum_offsets,
                padding_offset=padding_offset,
                attn_mask=paddle.cast(attention_mask, dtype=hidden_states.dtype),
                caches=cache_kvs,
                pre_caches=pre_caches,
                pre_caches_length=position_offset,
                seq_lens=seq_lens,
                rotary_embs=new_rope,
                rotary_emb_dims=1,
                time_step=paddle.increment(paddle.shape(attention_mask)[-1], -1) if is_decoder else None,
            )

        # print("hidden_states:", hidden_states)

        hidden_states = self.ln_f(hidden_states)
        # print("hidden_states_ln:", hidden_states)

        if output_hidden_states:
            all_hidden_states = all_hidden_states + (hidden_states,)

        if not return_dict:
            return tuple(v for v in [hidden_states, presents, all_hidden_states, all_self_attentions] if v is not None)

        return BaseModelOutputWithPast(
            last_hidden_state=hidden_states,
            past_key_values=presents,
            hidden_states=all_hidden_states,
            attentions=all_self_attentions,
        )


class Qwen2ForCausalLMInferenceModel(GenerationInferenceModel, Qwen2PretrainedModel):
    def __init__(self, config: Qwen2Config, **kwargs):
        super(Qwen2ForCausalLMInferenceModel, self).__init__(config)
        self.qwen2 = Qwen2InferenceModel(config)
        if config.tie_word_embeddings:
            self.lm_head = Qwen2LMHead(config, embedding_weights=self.qwen2.wte.weight, transpose_y=True)
            self.tie_weights()
        else:
            self.lm_head = Qwen2LMHead(config)
        self.criterion = Qwen2PretrainingCriterion(config)

    def get_output_embeddings(self):
        return self.lm_head

    def set_output_embeddings(self, new_embeddings):
        self.lm_head = new_embeddings

    @classmethod
    def from_pretrained(
        cls, pretrained_model_name_or_path, from_hf_hub: bool = False, subfolder: str | None = None, *args, **kwargs
    ):
        kwargs["use_safetensors"] = False
        return super().from_pretrained(pretrained_model_name_or_path, from_hf_hub, subfolder, *args, **kwargs)

    @classmethod
    def get_cache_kvs_shape(
        cls, config: Qwen2Config, max_batch_size: int = None, max_length: int = None
    ) -> list[list[int]]:
        """get cache_kvs tensor for qwen model

        Args:
            max_batch_size (int): the max batch size
            max_length (int | None, optional): the max_length of cache_kvs. Defaults to None.

        Returns:
            list[paddle.Tensor]: the list tensor shape for cache
        """
        if max_length is None:
            max_length = config.max_position_embeddings

        cache_kvs = []
        for _ in range(config.num_hidden_layers):
            cache_kvs.append(
                [
                    2,
                    max_batch_size,
                    config.num_key_value_heads // max(config.tensor_parallel_degree, 1),
                    max_length,
                    config.hidden_size // config.num_attention_heads,
                ]
            )
        return cache_kvs

    def prepare_inputs_for_generation(
        self,
        input_ids,
        cache_kvs,
        seq_len_encoder,
        seq_len_decoder,
        tgt_ids,
        tgt_pos,
        tgt_generation_mask,
        **kwargs,
    ):
        position_ids = kwargs.get("position_ids", None)
        attention_mask = kwargs.get("attention_mask", None)
        cache = kwargs.get("cache", None)
        pre_caches = kwargs.get("pre_caches", None)
        inputs_embeds = kwargs.get("inputs_embeds", None)
        if cache is not None:
            input_ids = tgt_ids
            position_ids = tgt_pos
            attention_mask = (tgt_generation_mask - 1) * 1e4
            # make inputs_embeds be none in decoder phase.
            # in forward function, it will be assigned according to input_ids.
            inputs_embeds = None
        else:
            attention_mask = (attention_mask - 1) * 1e4
        model_inputs = {
            "input_ids": input_ids,
            "inputs_embeds": inputs_embeds,
            "position_ids": position_ids,
            "attention_mask": attention_mask,
            "cache_kvs": cache_kvs,
            "seq_len_encoder": seq_len_encoder,
            "seq_len_decoder": seq_len_decoder,
            "cache": cache,
            "pre_caches": pre_caches,
        }
        return model_inputs

    def forward(
        self,
        input_ids,
        position_ids=None,
        attention_mask=None,
        inputs_embeds=None,
        use_cache=False,
        cache=None,
        cache_kvs=None,
        pre_caches=None,
        seq_len_encoder=None,
        seq_len_decoder=None,
        past_key_values=None,
        labels=None,
        output_attentions=None,
        output_hidden_states=None,
        return_dict=None,
    ):
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        outputs = self.qwen2(
            input_ids,
            position_ids=position_ids,
            attention_mask=attention_mask,
            inputs_embeds=inputs_embeds,
            use_cache=use_cache,
            cache=cache,
            cache_kvs=cache_kvs,
            pre_caches=pre_caches,
            seq_len_encoder=seq_len_encoder,
            seq_len_decoder=seq_len_decoder,
            past_key_values=past_key_values,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        hidden_states = outputs[0]

        # if labels is None，means we need full output, instead of tensor_parallel_output
        # tensor_parallel_output is togather with ParallelCrossEntropy
        tensor_parallel_output = (
            self.config.tensor_parallel_output and labels is not None and self.config.tensor_parallel_degree > 1
        )
        lm_logits = self.lm_head(hidden_states, tensor_parallel_output=tensor_parallel_output)

        loss = None
        if labels is not None:
            loss = self.criterion(lm_logits, labels)

        if not return_dict:
            output = (lm_logits,) + outputs[1:]
            return ((loss,) + output) if loss is not None else output

        return CausalLMOutputWithPast(
            loss=loss,
            logits=lm_logits,
            past_key_values=outputs.past_key_values,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
        )

    @paddle.no_grad()
    def set_state_dict(self, state_dict):
        if "lm_head.weight" in state_dict:
            lm_head_weight = paddle.to_tensor(state_dict["lm_head.weight"]).cast(self.lm_head.weight.dtype)
            self.lm_head.weight.set_value(lm_head_weight)
        self.qwen2.set_state_dict({k: state_dict[k] for k in state_dict.keys()})


@register_base_model
class Qwen2BlockInferenceModel(Qwen2InferenceModel):
    def __init__(self, config: Qwen2Config):
        super().__init__(config)
        self.max_seq_len = config.max_seq_len
        self.block_size = config.block_size

    def set_transformer_block(self, transformer_config):
        if self.use_weight_only:
            self.transformer_block = FusedBlockMultiTransformerWeightOnly(transformer_config)
        else:
            self.transformer_block = FusedBlockMultiTransformer(transformer_config)

    def remove_padding(self, input_ids, seq_lens_this_time):
        cum_offsets_now = paddle.cumsum(self.max_seq_len - seq_lens_this_time)
        token_num = paddle.sum(seq_lens_this_time)
        from paddlenlp_ops import get_padding_offset_v2

        ids_remove_padding, cum_offsets, padding_offset, cu_seqlens_q, cu_seqlens_k = get_padding_offset_v2(
            input_ids, cum_offsets_now, token_num, seq_lens_this_time
        )
        return ids_remove_padding, padding_offset, cum_offsets, cu_seqlens_q, cu_seqlens_k

    def forward(
        self,
        input_ids=None,
        attention_mask=None,
        inputs_embeds=None,
        caches=None,
        pre_caches=None,
        output_attentions=False,
        output_hidden_states=None,
        return_dict=False,
        **kwargs,
    ):

        seq_lens_this_time = kwargs.get("seq_lens_this_time", None)
        rope_emb = kwargs.get("rope_emb", None)
        ids_remove_padding, padding_offset, cum_offsets, cu_seqlens_q, cu_seqlens_k = self.remove_padding(
            input_ids, seq_lens_this_time
        )
        kwargs["cu_seqlens_q"] = cu_seqlens_q
        kwargs["cu_seqlens_k"] = cu_seqlens_k
        kwargs["padding_offsets"] = padding_offset
        kwargs["max_input_length"] = self.max_seq_len

        inputs_embeds = self.wte(ids_remove_padding)
        # import pdb;pdb.set_trace()

        with dy2st_nocheck_guard_context():
            hidden_states, _ = self.transformer_block(
                input_ids=input_ids,
                src=inputs_embeds,
                cum_offsets=cum_offsets,
                attn_mask=attention_mask,
                caches=caches,
                pre_caches=pre_caches,
                rotary_embs=rope_emb,
                **kwargs,
            )
        hidden_states = self.ln_f(hidden_states)

        return BaseModelOutputWithPastAndCrossAttentions(
            last_hidden_state=hidden_states,
            past_key_values=None,
            hidden_states=None,
            attentions=None,
        )


class Qwen2ForCausalLMBlockInferenceModel(GenerationBlockInferenceModel, Qwen2PretrainedModel):
    """
    Dynamic Batching for Qwen2 Model with pretraining tasks on top.
    """

    _keys_to_ignore_on_load_missing = [r"lm_head.weight"]

    def __init__(self, config):
        super().__init__(config)
        self.qwen2 = Qwen2BlockInferenceModel(config)
        if config.tie_word_embeddings:
            self.lm_head = Qwen2LMHead(config, embedding_weights=self.qwen2.wte.weight, transpose_y=True)
            self.tie_weights()
        else:
            self.lm_head = Qwen2LMHead(config)

    @classmethod
    def _get_tensor_parallel_mappings(cls, config: Qwen2Config, is_split=True):

        logger.info("Qwen2 inference model _get_tensor_parallel_mappings")

        from paddlenlp.transformers.conversion_utils import split_or_merge_func

        fn = split_or_merge_func(
            is_split=is_split,
            tensor_parallel_degree=config.tensor_parallel_degree,
            tensor_parallel_rank=config.tensor_parallel_rank,
            num_attention_heads=config.num_attention_heads,
        )

        def get_tensor_parallel_split_mappings(num_layers):
            final_actions = {}

            base_actions = {
                "lm_head.weight": partial(fn, is_column=True),
                # Row Linear
                "embed_tokens.weight": partial(fn, is_column=False),
                "layers.0.self_attn.o_proj.weight": partial(fn, is_column=False),
                "layers.0.mlp.down_proj.weight": partial(fn, is_column=False),
            }

            # Column Linear
            if config.fuse_attention_qkv:
                base_actions["layers.0.self_attn.qkv_proj.weight"] = partial(fn, is_column=True)
            else:
                base_actions["layers.0.self_attn.q_proj.weight"] = partial(fn, is_column=True)
                # if we have enough num_key_value_heads to split, then split it.
                if config.num_key_value_heads % config.tensor_parallel_degree == 0:
                    base_actions["layers.0.self_attn.k_proj.weight"] = partial(fn, is_column=True)
                    base_actions["layers.0.self_attn.v_proj.weight"] = partial(fn, is_column=True)

            if config.fuse_attention_ffn:
                base_actions["layers.0.mlp.gate_up_fused_proj.weight"] = partial(
                    fn, is_column=True, is_naive_2fuse=True
                )
            else:
                base_actions["layers.0.mlp.gate_proj.weight"] = partial(fn, is_column=True)
                base_actions["layers.0.mlp.up_proj.weight"] = partial(fn, is_column=True)

            for key, action in base_actions.items():
                if "layers.0." in key:
                    for i in range(num_layers):
                        final_actions[key.replace("layers.0.", f"layers.{i}.")] = action
                final_actions[key] = action

            return final_actions

        mappings = get_tensor_parallel_split_mappings(config.num_hidden_layers)

        return mappings

    @classmethod
    def from_pretrained(cls, pretrained_model_name_or_path, *args, **kwargs):
        # TODO: Support safetensors loading.
        kwargs["use_safetensors"] = False
        from paddlenlp.transformers.utils import (
            ContextManagers,
            is_safetensors_available,
        )

        from_hf_hub = kwargs.pop("from_hf_hub", False)
        config = kwargs.pop("config", None)
        from_aistudio = kwargs.get("from_aistudio", False)
        subfolder = kwargs.get("subfolder", None)
        variant = kwargs.pop("variant", None)
        use_safetensors = kwargs.pop("use_safetensors", None if is_safetensors_available() else False)
        convert_from_torch = kwargs.pop("convert_from_torch", None)
        cache_dir = kwargs.pop("cache_dir", None)

        init_contexts = []
        with ContextManagers(init_contexts):
            model = cls(config)

        if not config.single_card_ptq:
            resolved_archive_file = pretrained_model_name_or_path
        else:
            resolved_archive_file = cls._resolve_model_file_path(
                pretrained_model_name_or_path,
                cache_dir=cache_dir,
                subfolder=subfolder,
                from_hf_hub=from_hf_hub,
                from_aistudio=from_aistudio,
                config=config,
                convert_from_torch=convert_from_torch,
                use_safetensors=use_safetensors,
                variant=variant,
            )[0]
        logger.info(f"Load model form {resolved_archive_file}")

        if config.tensor_parallel_degree > 1 and config.single_card_ptq:
            logger.info(f"convert_tensor_parallel {config.tensor_parallel_degree}")
            model.state_dict = model.convert_tensor_parallel(resolved_archive_file, config)
        elif config.tensor_parallel_degree > 1:
            resolved_archive_file = os.path.join(
                resolved_archive_file, f"mp_{config.tensor_parallel_rank:0>2d}_sharding_00_pp_00", "model.pdparams"
            )
            model.state_dict = paddle.load(resolved_archive_file, return_numpy=True)
        else:
            model.state_dict = paddle.load(resolved_archive_file, return_numpy=True)
        model.set_state_dict(model.state_dict)

        return model

    @classmethod
    def get_cache_kvs_shape(
        cls, config: Qwen2Config, max_batch_size: int = None, max_length: int = None
    ) -> list[list[int]]:
        """get cache_kvs tensor for Qwen2 model

        Args:
            max_batch_size (int): the max batch size
            max_length (int | None, optional): the max_length of cache_kvs. Defaults to None.

        Returns:
            list[paddle.Tensor]: the list tensor shape for cache
        """
        max_block_per_seq = (config.max_seq_len + config.block_size - 1) // config.block_size
        if max_batch_size == -1:
            max_block_nums = None
        else:
            max_block_nums = max_batch_size * max_block_per_seq

        cache_kvs = []
        for _ in range(config.num_hidden_layers):
            cache_kv_shape = [
                max_block_nums,
                config.num_key_value_heads // max(config.tensor_parallel_degree, 1),
                config.block_size,
                config.hidden_size // config.num_attention_heads,
            ]
            cache_kvs.append(cache_kv_shape)
            cache_kvs.append(cache_kv_shape)
        return cache_kvs

    def prepare_inputs_for_generation(self, **kwargs):
        # only last token for inputs_ids if cache is defined in kwargs
        input_ids = kwargs["input_ids"]
        src_mask = kwargs.get("src_mask", None)
        block_tables = kwargs.get("block_tables", None)

        pre_caches = kwargs.get("pre_caches", None)
        caches = kwargs.get("caches", None)

        rope_emb = kwargs["rope_emb"]
        seq_lens_this_time = kwargs["seq_lens_this_time"]
        seq_lens_encoder = kwargs["seq_lens_encoder"]
        seq_lens_decoder = kwargs["seq_lens_decoder"]
        k_quant_scales = kwargs.get("k_quant_scales", None)
        v_quant_scales = kwargs.get("v_quant_scales", None)
        k_dequant_scales = kwargs.get("k_dequant_scales", None)
        v_dequant_scales = kwargs.get("v_dequant_scales", None)
        model_inputs = {
            "input_ids": input_ids,
            "src_mask": src_mask,
            "rope_emb": rope_emb,
            "pre_caches": pre_caches,
            "caches": caches,
            "seq_lens_this_time": seq_lens_this_time,
            "seq_lens_encoder": seq_lens_encoder,
            "seq_lens_decoder": seq_lens_decoder,
            "block_tables": block_tables,
            "k_quant_scales": k_quant_scales,
            "v_quant_scales": v_quant_scales,
            "k_dequant_scales": k_dequant_scales,
            "v_dequant_scales": v_dequant_scales,
        }
        return model_inputs

    def forward(
        self,
        input_ids,
        src_mask=None,
        pre_caches=None,
        caches=None,
        seq_lens_this_time=None,
        seq_lens_encoder=None,
        seq_lens_decoder=None,
        rope_emb=None,
        block_tables=None,
        k_quant_scales=None,
        v_quant_scales=None,
        k_dequant_scales=None,
        v_dequant_scales=None,
    ):
        outputs = self.qwen2(
            input_ids,
            src_mask=src_mask,
            caches=caches,
            rope_emb=rope_emb,
            block_tables=block_tables,
            pre_caches=pre_caches,
            seq_lens_this_time=seq_lens_this_time,
            seq_lens_encoder=seq_lens_encoder,
            seq_lens_decoder=seq_lens_decoder,
            k_quant_scales=k_quant_scales,
            v_quant_scales=v_quant_scales,
            k_dequant_scales=k_dequant_scales,
            v_dequant_scales=v_dequant_scales,
        )

        hidden_states = outputs[0]
        logits = self.lm_head(
            hidden_states,
            tensor_parallel_output=False,
        )

        return logits

    @paddle.no_grad()
    def set_state_dict(self, state_dict):
        if "lm_head.weight" in state_dict:
            self.lm_head.weight.set_value(
                paddle.to_tensor(state_dict["lm_head.weight"]).cast(self.lm_head.weight.dtype)
            )
        self.qwen2.set_state_dict({k: state_dict[k] for k in state_dict.keys()})