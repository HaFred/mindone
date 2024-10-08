# Copyright 2024 The HuggingFace Team. All rights reserved.
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
from typing import Any, Dict, Optional, Tuple, Union

import mindspore as ms
from mindspore import nn, ops

from ...configuration_utils import ConfigMixin, FrozenDict, register_to_config
from ...loaders import UNet2DConditionLoadersMixin
from ...utils import logging
from ..attention_processor import CROSS_ATTENTION_PROCESSORS, AttentionProcessor, AttnProcessor, IPAdapterAttnProcessor
from ..embeddings import TimestepEmbedding, Timesteps
from ..modeling_utils import ModelMixin
from ..normalization import GroupNorm
from ..transformers.transformer_temporal import TransformerTemporalModel
from .unet_2d_blocks import UNetMidBlock2DCrossAttn
from .unet_2d_condition import UNet2DConditionModel
from .unet_3d_blocks import (
    CrossAttnDownBlockMotion,
    CrossAttnUpBlockMotion,
    DownBlockMotion,
    UNetMidBlockCrossAttnMotion,
    UpBlockMotion,
    get_down_block,
    get_up_block,
)
from .unet_3d_condition import UNet3DConditionOutput

logger = logging.get_logger(__name__)  # pylint: disable=invalid-name


class MotionModules(nn.Cell):
    def __init__(
        self,
        in_channels: int,
        layers_per_block: int = 2,
        num_attention_heads: int = 8,
        attention_bias: bool = False,
        cross_attention_dim: Optional[int] = None,
        activation_fn: str = "geglu",
        norm_num_groups: int = 32,
        max_seq_length: int = 32,
    ):
        super().__init__()
        self.motion_modules = []

        for i in range(layers_per_block):
            self.motion_modules.append(
                TransformerTemporalModel(
                    in_channels=in_channels,
                    norm_num_groups=norm_num_groups,
                    cross_attention_dim=cross_attention_dim,
                    activation_fn=activation_fn,
                    attention_bias=attention_bias,
                    num_attention_heads=num_attention_heads,
                    attention_head_dim=in_channels // num_attention_heads,
                    positional_embeddings="sinusoidal",
                    num_positional_embeddings=max_seq_length,
                )
            )
        self.motion_modules = nn.CellList(self.motion_modules)


class MotionAdapter(ModelMixin, ConfigMixin):
    @register_to_config
    def __init__(
        self,
        block_out_channels: Tuple[int, ...] = (320, 640, 1280, 1280),
        motion_layers_per_block: int = 2,
        motion_mid_block_layers_per_block: int = 1,
        motion_num_attention_heads: int = 8,
        motion_norm_num_groups: int = 32,
        motion_max_seq_length: int = 32,
        use_motion_mid_block: bool = True,
        conv_in_channels: Optional[int] = None,
    ):
        """Container to store AnimateDiff Motion Modules

        Args:
            block_out_channels (`Tuple[int]`, *optional*, defaults to `(320, 640, 1280, 1280)`):
            The tuple of output channels for each UNet block.
            motion_layers_per_block (`int`, *optional*, defaults to 2):
                The number of motion layers per UNet block.
            motion_mid_block_layers_per_block (`int`, *optional*, defaults to 1):
                The number of motion layers in the middle UNet block.
            motion_num_attention_heads (`int`, *optional*, defaults to 8):
                The number of heads to use in each attention layer of the motion module.
            motion_norm_num_groups (`int`, *optional*, defaults to 32):
                The number of groups to use in each group normalization layer of the motion module.
            motion_max_seq_length (`int`, *optional*, defaults to 32):
                The maximum sequence length to use in the motion module.
            use_motion_mid_block (`bool`, *optional*, defaults to True):
                Whether to use a motion module in the middle of the UNet.
        """

        super().__init__()
        down_blocks = []
        up_blocks = []

        if conv_in_channels:
            # input
            self.conv_in = nn.Conv2d(
                conv_in_channels, block_out_channels[0], kernel_size=3, pad_mode="pad", padding=1, has_bias=True
            )
        else:
            self.conv_in = None

        for i, channel in enumerate(block_out_channels):
            output_channel = block_out_channels[i]
            down_blocks.append(
                MotionModules(
                    in_channels=output_channel,
                    norm_num_groups=motion_norm_num_groups,
                    cross_attention_dim=None,
                    activation_fn="geglu",
                    attention_bias=False,
                    num_attention_heads=motion_num_attention_heads,
                    max_seq_length=motion_max_seq_length,
                    layers_per_block=motion_layers_per_block,
                )
            )

        if use_motion_mid_block:
            self.mid_block = MotionModules(
                in_channels=block_out_channels[-1],
                norm_num_groups=motion_norm_num_groups,
                cross_attention_dim=None,
                activation_fn="geglu",
                attention_bias=False,
                num_attention_heads=motion_num_attention_heads,
                layers_per_block=motion_mid_block_layers_per_block,
                max_seq_length=motion_max_seq_length,
            )
        else:
            self.mid_block = None

        reversed_block_out_channels = list(reversed(block_out_channels))
        output_channel = reversed_block_out_channels[0]
        for i, channel in enumerate(reversed_block_out_channels):
            output_channel = reversed_block_out_channels[i]
            up_blocks.append(
                MotionModules(
                    in_channels=output_channel,
                    norm_num_groups=motion_norm_num_groups,
                    cross_attention_dim=None,
                    activation_fn="geglu",
                    attention_bias=False,
                    num_attention_heads=motion_num_attention_heads,
                    max_seq_length=motion_max_seq_length,
                    layers_per_block=motion_layers_per_block + 1,
                )
            )

        self.down_blocks = nn.CellList(down_blocks)
        self.up_blocks = nn.CellList(up_blocks)

    def construct(self, sample):
        pass


class UNetMotionModel(ModelMixin, ConfigMixin, UNet2DConditionLoadersMixin):
    r"""
    A modified conditional 2D UNet model that takes a noisy sample, conditional state, and a timestep and returns a
    sample shaped output.

    This model inherits from [`ModelMixin`]. Check the superclass documentation for it's generic methods implemented
    for all models (such as downloading or saving).
    """

    _supports_gradient_checkpointing = True

    @register_to_config
    def __init__(
        self,
        sample_size: Optional[int] = None,
        in_channels: int = 4,
        out_channels: int = 4,
        down_block_types: Tuple[str, ...] = (
            "CrossAttnDownBlockMotion",
            "CrossAttnDownBlockMotion",
            "CrossAttnDownBlockMotion",
            "DownBlockMotion",
        ),
        up_block_types: Tuple[str, ...] = (
            "UpBlockMotion",
            "CrossAttnUpBlockMotion",
            "CrossAttnUpBlockMotion",
            "CrossAttnUpBlockMotion",
        ),
        block_out_channels: Tuple[int, ...] = (320, 640, 1280, 1280),
        layers_per_block: int = 2,
        downsample_padding: int = 1,
        mid_block_scale_factor: float = 1,
        act_fn: str = "silu",
        norm_num_groups: int = 32,
        norm_eps: float = 1e-5,
        cross_attention_dim: int = 1280,
        transformer_layers_per_block: Union[int, Tuple[int], Tuple[Tuple]] = 1,
        reverse_transformer_layers_per_block: Optional[Tuple[Tuple[int]]] = None,
        use_linear_projection: bool = False,
        num_attention_heads: Union[int, Tuple[int, ...]] = 8,
        motion_max_seq_length: int = 32,
        motion_num_attention_heads: int = 8,
        use_motion_mid_block: int = True,
        encoder_hid_dim: Optional[int] = None,
        encoder_hid_dim_type: Optional[str] = None,
        addition_embed_type: Optional[str] = None,
        addition_time_embed_dim: Optional[int] = None,
        projection_class_embeddings_input_dim: Optional[int] = None,
        time_cond_proj_dim: Optional[int] = None,
    ):
        super().__init__()

        self.sample_size = sample_size

        # Check inputs
        if len(down_block_types) != len(up_block_types):
            raise ValueError(
                f"Must provide the same number of `down_block_types` as `up_block_types`. "
                f"`down_block_types`: {down_block_types}. `up_block_types`: {up_block_types}."
            )

        if len(block_out_channels) != len(down_block_types):
            raise ValueError(
                f"Must provide the same number of `block_out_channels` as `down_block_types`. "
                f"`block_out_channels`: {block_out_channels}. `down_block_types`: {down_block_types}."
            )

        if not isinstance(num_attention_heads, int) and len(num_attention_heads) != len(down_block_types):
            raise ValueError(
                f"Must provide the same number of `num_attention_heads` as `down_block_types`. "
                f"`num_attention_heads`: {num_attention_heads}. `down_block_types`: {down_block_types}."
            )

        if isinstance(cross_attention_dim, list) and len(cross_attention_dim) != len(down_block_types):
            raise ValueError(
                f"Must provide the same number of `cross_attention_dim` as `down_block_types`. `cross_attention_dim`: {cross_attention_dim}. `down_block_types`: {down_block_types}."  # noqa: E501
            )

        if not isinstance(layers_per_block, int) and len(layers_per_block) != len(down_block_types):
            raise ValueError(
                f"Must provide the same number of `layers_per_block` as `down_block_types`. `layers_per_block`: {layers_per_block}. `down_block_types`: {down_block_types}."  # noqa: E501
            )

        if isinstance(transformer_layers_per_block, list) and reverse_transformer_layers_per_block is None:
            for layer_number_per_block in transformer_layers_per_block:
                if isinstance(layer_number_per_block, list):
                    raise ValueError("Must provide 'reverse_transformer_layers_per_block` if using asymmetrical UNet.")

        # input
        conv_in_kernel = 3
        conv_out_kernel = 3
        conv_in_padding = (conv_in_kernel - 1) // 2
        self.conv_in = nn.Conv2d(
            in_channels,
            block_out_channels[0],
            kernel_size=conv_in_kernel,
            pad_mode="pad",
            padding=conv_in_padding,
            has_bias=True,
        )

        # time
        time_embed_dim = block_out_channels[0] * 4
        self.time_proj = Timesteps(block_out_channels[0], True, 0)
        timestep_input_dim = block_out_channels[0]

        self.time_embedding = TimestepEmbedding(
            timestep_input_dim, time_embed_dim, act_fn=act_fn, cond_proj_dim=time_cond_proj_dim
        )

        if encoder_hid_dim_type is None:
            self.encoder_hid_proj = None

        if addition_embed_type == "text_time":
            self.add_time_proj = Timesteps(addition_time_embed_dim, True, 0)
            self.add_embedding = TimestepEmbedding(projection_class_embeddings_input_dim, time_embed_dim)

        # class embedding
        down_blocks = []
        up_blocks = []

        if isinstance(num_attention_heads, int):
            num_attention_heads = (num_attention_heads,) * len(down_block_types)

        if isinstance(cross_attention_dim, int):
            cross_attention_dim = (cross_attention_dim,) * len(down_block_types)

        if isinstance(layers_per_block, int):
            layers_per_block = [layers_per_block] * len(down_block_types)

        if isinstance(transformer_layers_per_block, int):
            transformer_layers_per_block = [transformer_layers_per_block] * len(down_block_types)

        # down
        output_channel = block_out_channels[0]
        for i, down_block_type in enumerate(down_block_types):
            input_channel = output_channel
            output_channel = block_out_channels[i]
            is_final_block = i == len(block_out_channels) - 1

            down_block = get_down_block(
                down_block_type,
                num_layers=layers_per_block[i],
                in_channels=input_channel,
                out_channels=output_channel,
                temb_channels=time_embed_dim,
                add_downsample=not is_final_block,
                resnet_eps=norm_eps,
                resnet_act_fn=act_fn,
                resnet_groups=norm_num_groups,
                cross_attention_dim=cross_attention_dim[i],
                num_attention_heads=num_attention_heads[i],
                downsample_padding=downsample_padding,
                use_linear_projection=use_linear_projection,
                dual_cross_attention=False,
                temporal_num_attention_heads=motion_num_attention_heads,
                temporal_max_seq_length=motion_max_seq_length,
                transformer_layers_per_block=transformer_layers_per_block[i],
            )
            down_blocks.append(down_block)
        self.down_blocks = nn.CellList(down_blocks)

        # mid: only definition, binding attribute to UNetMotionModel later to maintain the order of sub-modules within
        # UNetMotionModel as self.down_blocks -> self.up_blocks -> self.mid_block, ensuring the correct sequence of
        # sub-modules is loaded when the ip-adpater is loaded.
        if use_motion_mid_block:
            mid_block = UNetMidBlockCrossAttnMotion(
                in_channels=block_out_channels[-1],
                temb_channels=time_embed_dim,
                resnet_eps=norm_eps,
                resnet_act_fn=act_fn,
                output_scale_factor=mid_block_scale_factor,
                cross_attention_dim=cross_attention_dim[-1],
                num_attention_heads=num_attention_heads[-1],
                resnet_groups=norm_num_groups,
                dual_cross_attention=False,
                use_linear_projection=use_linear_projection,
                temporal_num_attention_heads=motion_num_attention_heads,
                temporal_max_seq_length=motion_max_seq_length,
                transformer_layers_per_block=transformer_layers_per_block[-1],
            )

        else:
            mid_block = UNetMidBlock2DCrossAttn(
                in_channels=block_out_channels[-1],
                temb_channels=time_embed_dim,
                resnet_eps=norm_eps,
                resnet_act_fn=act_fn,
                output_scale_factor=mid_block_scale_factor,
                cross_attention_dim=cross_attention_dim[-1],
                num_attention_heads=num_attention_heads[-1],
                resnet_groups=norm_num_groups,
                dual_cross_attention=False,
                use_linear_projection=use_linear_projection,
                transformer_layers_per_block=transformer_layers_per_block[-1],
            )

        # count how many layers upsample the images
        self.num_upsamplers = 0

        # up
        layers_per_resnet_in_up_blocks = []
        reversed_block_out_channels = list(reversed(block_out_channels))
        reversed_num_attention_heads = list(reversed(num_attention_heads))
        reversed_layers_per_block = list(reversed(layers_per_block))
        reversed_cross_attention_dim = list(reversed(cross_attention_dim))
        reversed_transformer_layers_per_block = list(reversed(transformer_layers_per_block))

        output_channel = reversed_block_out_channels[0]
        for i, up_block_type in enumerate(up_block_types):
            is_final_block = i == len(block_out_channels) - 1

            prev_output_channel = output_channel
            output_channel = reversed_block_out_channels[i]
            input_channel = reversed_block_out_channels[min(i + 1, len(block_out_channels) - 1)]

            # add upsample block for all BUT final layer
            if not is_final_block:
                add_upsample = True
                self.num_upsamplers += 1
            else:
                add_upsample = False

            up_block = get_up_block(
                up_block_type,
                num_layers=reversed_layers_per_block[i] + 1,
                in_channels=input_channel,
                out_channels=output_channel,
                prev_output_channel=prev_output_channel,
                temb_channels=time_embed_dim,
                add_upsample=add_upsample,
                resnet_eps=norm_eps,
                resnet_act_fn=act_fn,
                resnet_groups=norm_num_groups,
                cross_attention_dim=reversed_cross_attention_dim[i],
                num_attention_heads=reversed_num_attention_heads[i],
                dual_cross_attention=False,
                resolution_idx=i,
                use_linear_projection=use_linear_projection,
                temporal_num_attention_heads=motion_num_attention_heads,
                temporal_max_seq_length=motion_max_seq_length,
                transformer_layers_per_block=reversed_transformer_layers_per_block[i],
            )
            up_blocks.append(up_block)
            prev_output_channel = output_channel
            layers_per_resnet_in_up_blocks.append(len(up_block.resnets))
        self.up_blocks = nn.CellList(up_blocks)
        self.layers_per_resnet_in_up_blocks = layers_per_resnet_in_up_blocks

        # bind mid_block to self here
        self.mid_block = mid_block

        # out
        if norm_num_groups is not None:
            self.conv_norm_out = GroupNorm(num_channels=block_out_channels[0], num_groups=norm_num_groups, eps=norm_eps)
            self.conv_act = nn.SiLU()
        else:
            self.conv_norm_out = None
            self.conv_act = None

        conv_out_padding = (conv_out_kernel - 1) // 2
        self.conv_out = nn.Conv2d(
            block_out_channels[0],
            out_channels,
            kernel_size=conv_out_kernel,
            pad_mode="pad",
            padding=conv_out_padding,
            has_bias=True,
        )

    @classmethod
    def from_unet2d(
        cls,
        unet: UNet2DConditionModel,
        motion_adapter: Optional[MotionAdapter] = None,
        load_weights: bool = True,
    ):
        has_motion_adapter = motion_adapter is not None

        # based on https://github.com/guoyww/AnimateDiff/blob/895f3220c06318ea0760131ec70408b466c49333/animatediff/models/unet.py#L459
        config = dict(unet.config)
        config["_class_name"] = cls.__name__

        down_blocks = []
        for down_blocks_type in config["down_block_types"]:
            if "CrossAttn" in down_blocks_type:
                down_blocks.append("CrossAttnDownBlockMotion")
            else:
                down_blocks.append("DownBlockMotion")
        config["down_block_types"] = down_blocks

        up_blocks = []
        for down_blocks_type in config["up_block_types"]:
            if "CrossAttn" in down_blocks_type:
                up_blocks.append("CrossAttnUpBlockMotion")
            else:
                up_blocks.append("UpBlockMotion")

        config["up_block_types"] = up_blocks

        if has_motion_adapter:
            config["motion_num_attention_heads"] = motion_adapter.config["motion_num_attention_heads"]
            config["motion_max_seq_length"] = motion_adapter.config["motion_max_seq_length"]
            config["use_motion_mid_block"] = motion_adapter.config["use_motion_mid_block"]

            # For PIA UNets we need to set the number input channels to 9
            if motion_adapter.config["conv_in_channels"]:
                config["in_channels"] = motion_adapter.config["conv_in_channels"]

        # Need this for backwards compatibility with UNet2DConditionModel checkpoints
        if not config.get("num_attention_heads"):
            config["num_attention_heads"] = config["attention_head_dim"]

        config = FrozenDict(config)
        model = cls.from_config(config)

        # Move dtype conversion code here to avoid dtype mismatch issues when loading weights
        # ensure that the Motion UNet is the same dtype as the UNet2DConditionModel
        model.to(unet.dtype)

        if not load_weights:
            return model

        # Logic for loading PIA UNets which allow the first 4 channels to be any UNet2DConditionModel conv_in weight
        # while the last 5 channels must be PIA conv_in weights.
        if has_motion_adapter and motion_adapter.config["conv_in_channels"]:
            model.conv_in = motion_adapter.conv_in
            updated_conv_in_weight = ops.cat([unet.conv_in.weight, motion_adapter.conv_in.weight[:, 4:, :, :]], axis=1)
            ms.load_param_into_net(model.conv_in, {"weight": updated_conv_in_weight, "bias": unet.conv_in.bias})
        else:
            ms.load_param_into_net(model.conv_in, unet.conv_in.parameters_dict())

        ms.load_param_into_net(model.time_proj, unet.time_proj.parameters_dict())
        ms.load_param_into_net(model.time_embedding, unet.time_embedding.parameters_dict())

        if any(isinstance(proc, IPAdapterAttnProcessor) for proc in unet.attn_processors.values()):
            attn_procs = {}
            for name, processor in unet.attn_processors.items():
                if name.endswith("attn1.processor"):
                    attn_processor_class = AttnProcessor
                    attn_procs[name] = attn_processor_class()
                else:
                    attn_processor_class = IPAdapterAttnProcessor
                    attn_procs[name] = attn_processor_class(
                        hidden_size=processor.hidden_size,
                        cross_attention_dim=processor.cross_attention_dim,
                        scale=processor.scale,
                        num_tokens=processor.num_tokens,
                    )
            for name, processor in model.attn_processors.items():
                if name not in attn_procs:
                    attn_procs[name] = processor.__class__()
            model.set_attn_processor(attn_procs)
            model.config.encoder_hid_dim_type = "ip_image_proj"
            model.encoder_hid_proj = unet.encoder_hid_proj

        for i, down_block in enumerate(unet.down_blocks):
            ms.load_param_into_net(model.down_blocks[i].resnets, down_block.resnets.parameters_dict())
            if hasattr(model.down_blocks[i], "attentions"):
                ms.load_param_into_net(model.down_blocks[i].attentions, down_block.attentions.parameters_dict())
            if model.down_blocks[i].downsamplers:
                ms.load_param_into_net(model.down_blocks[i].downsamplers, down_block.downsamplers.parameters_dict())

        for i, up_block in enumerate(unet.up_blocks):
            ms.load_param_into_net(model.up_blocks[i].resnets, up_block.resnets.parameters_dict())
            if hasattr(model.up_blocks[i], "attentions"):
                ms.load_param_into_net(model.up_blocks[i].attentions, up_block.attentions.parameters_dict())
            if model.up_blocks[i].upsamplers:
                ms.load_param_into_net(model.up_blocks[i].upsamplers, up_block.upsamplers.parameters_dict())

        ms.load_param_into_net(model.mid_block.resnets, unet.mid_block.resnets.parameters_dict())
        ms.load_param_into_net(model.mid_block.attentions, unet.mid_block.attentions.parameters_dict())

        if unet.conv_norm_out is not None:
            ms.load_param_into_net(model.conv_norm_out, unet.conv_norm_out.parameters_dict())
        if unet.conv_act is not None:
            ms.load_param_into_net(model.conv_act, unet.conv_act.parameters_dict())
        ms.load_param_into_net(model.conv_out, unet.conv_out.parameters_dict())

        if has_motion_adapter:
            model.load_motion_modules(motion_adapter)

        return model

    def freeze_unet2d_params(self) -> None:
        """Freeze the weights of just the UNet2DConditionModel, and leave the motion modules
        unfrozen for fine tuning.
        """
        # Freeze everything
        for param in self.get_parameters():
            param.requires_grad = False

        # Unfreeze Motion Modules
        for down_block in self.down_blocks:
            motion_modules = down_block.motion_modules
            for param in motion_modules.get_parameters():
                param.requires_grad = True

        for up_block in self.up_blocks:
            motion_modules = up_block.motion_modules
            for param in motion_modules.get_parameters():
                param.requires_grad = True

        if hasattr(self.mid_block, "motion_modules"):
            motion_modules = self.mid_block.motion_modules
            for param in motion_modules.get_parameters():
                param.requires_grad = True

    def load_motion_modules(self, motion_adapter: Optional[MotionAdapter]) -> None:
        for i, down_block in enumerate(motion_adapter.down_blocks):
            ms.load_param_into_net(self.down_blocks[i].motion_modules, down_block.motion_modules.parameters_dict())
        for i, up_block in enumerate(motion_adapter.up_blocks):
            ms.load_param_into_net(self.up_blocks[i].motion_modules, up_block.motion_modules.parameters_dict())

        # to support older motion modules that don't have a mid_block
        if hasattr(self.mid_block, "motion_modules"):
            ms.load_param_into_net(
                self.mid_block.motion_modules, motion_adapter.mid_block.motion_modules.parameters_dict()
            )

    def save_motion_modules(
        self,
        save_directory: str,
        is_main_process: bool = True,
        safe_serialization: bool = True,
        variant: Optional[str] = None,
        push_to_hub: bool = False,
        **kwargs,
    ) -> None:
        state_dict = self.parameters_dict()

        # Extract all motion modules
        motion_state_dict = {}
        for k, v in state_dict.items():
            if "motion_modules" in k:
                motion_state_dict[k] = v

        adapter = MotionAdapter(
            block_out_channels=self.config["block_out_channels"],
            motion_layers_per_block=self.config["layers_per_block"],
            motion_norm_num_groups=self.config["norm_num_groups"],
            motion_num_attention_heads=self.config["motion_num_attention_heads"],
            motion_max_seq_length=self.config["motion_max_seq_length"],
            use_motion_mid_block=self.config["use_motion_mid_block"],
        )
        ms.load_param_into_net(adapter, motion_state_dict)
        adapter.save_pretrained(
            save_directory=save_directory,
            is_main_process=is_main_process,
            safe_serialization=safe_serialization,
            variant=variant,
            push_to_hub=push_to_hub,
            **kwargs,
        )

    @property
    # Copied from diffusers.models.unets.unet_2d_condition.UNet2DConditionModel.attn_processors
    def attn_processors(self) -> Dict[str, AttentionProcessor]:  # type: ignore
        r"""
        Returns:
            `dict` of attention processors: A dictionary containing all attention processors used in the model with
            indexed by its weight name.
        """
        # set recursively
        processors = {}

        def fn_recursive_add_processors(name: str, module: nn.Cell, processors: Dict[str, AttentionProcessor]):  # type: ignore
            if hasattr(module, "get_processor"):
                processors[f"{name}.processor"] = module.get_processor()

            for sub_name, child in module.name_cells().items():
                fn_recursive_add_processors(f"{name}.{sub_name}", child, processors)

            return processors

        for name, module in self.name_cells().items():
            fn_recursive_add_processors(name, module, processors)

        return processors

    # Copied from diffusers.models.unets.unet_2d_condition.UNet2DConditionModel.set_attn_processor
    def set_attn_processor(self, processor: Union[AttentionProcessor, Dict[str, AttentionProcessor]]):  # type: ignore
        r"""
        Sets the attention processor to use to compute attention.
        Parameters:
            processor (`dict` of `AttentionProcessor` or only `AttentionProcessor`):
                The instantiated processor class or a dictionary of processor classes that will be set as the processor
                for **all** `Attention` layers.
                If `processor` is a dict, the key needs to define the path to the corresponding cross attention
                processor. This is strongly recommended when setting trainable attention processors.
        """
        count = len(self.attn_processors.keys())

        if isinstance(processor, dict) and len(processor) != count:
            raise ValueError(
                f"A dict of processors was passed, but the number of processors {len(processor)} does not match the"
                f" number of attention layers: {count}. Please make sure to pass {count} processor classes."
            )

        def fn_recursive_attn_processor(name: str, module: nn.Cell, processor):
            if hasattr(module, "set_processor"):
                if not isinstance(processor, dict):
                    module.set_processor(processor)
                else:
                    module.set_processor(processor.pop(f"{name}.processor"))

            for sub_name, child in module.name_cells().items():
                fn_recursive_attn_processor(f"{name}.{sub_name}", child, processor)

        for name, module in self.name_cells().items():
            fn_recursive_attn_processor(name, module, processor)

    # Copied from diffusers.models.unets.unet_3d_condition.UNet3DConditionModel.enable_forward_chunking
    def enable_forward_chunking(self, chunk_size: Optional[int] = None, dim: int = 0) -> None:
        """
        Sets the attention processor to use [feed forward
        chunking](https://huggingface.co/blog/reformer#2-chunked-feed-forward-layers).

        Parameters:
            chunk_size (`int`, *optional*):
                The chunk size of the feed-forward layers. If not specified, will run feed-forward layer individually
                over each tensor of dim=`dim`.
            dim (`int`, *optional*, defaults to `0`):
                The dimension over which the feed-forward computation should be chunked. Choose between dim=0 (batch)
                or dim=1 (sequence length).
        """
        if dim not in [0, 1]:
            raise ValueError(f"Make sure to set `dim` to either 0 or 1, not {dim}")

        # By default chunk size is 1
        chunk_size = chunk_size or 1

        def fn_recursive_feed_forward(module: nn.Cell, chunk_size: int, dim: int):
            if hasattr(module, "set_chunk_feed_forward"):
                module.set_chunk_feed_forward(chunk_size=chunk_size, dim=dim)

            for child in module.name_cells().values():
                fn_recursive_feed_forward(child, chunk_size, dim)

        for module in self.name_cells().values():
            fn_recursive_feed_forward(module, chunk_size, dim)

    # Copied from diffusers.models.unets.unet_3d_condition.UNet3DConditionModel.disable_forward_chunking
    def disable_forward_chunking(self):
        def fn_recursive_feed_forward(module: nn.Cell, chunk_size: int, dim: int):
            if hasattr(module, "set_chunk_feed_forward"):
                module.set_chunk_feed_forward(chunk_size=chunk_size, dim=dim)

            for child in module.name_cells().values():
                fn_recursive_feed_forward(child, chunk_size, dim)

        for module in self.name_cells().values():
            fn_recursive_feed_forward(module, None, 0)

    # Copied from diffusers.models.unets.unet_2d_condition.UNet2DConditionModel.set_default_attn_processor
    def set_default_attn_processor(self):
        """
        Disables custom attention processors and sets the default attention implementation.
        """
        if all(proc.__class__ in CROSS_ATTENTION_PROCESSORS for proc in self.attn_processors.values()):
            processor = AttnProcessor()
        else:
            raise ValueError(
                f"Cannot call `set_default_attn_processor` when attention processors are of type {next(iter(self.attn_processors.values()))}"
            )

        self.set_attn_processor(processor)

    def _set_gradient_checkpointing(self, module, value: bool = False) -> None:
        if isinstance(module, (CrossAttnDownBlockMotion, DownBlockMotion, CrossAttnUpBlockMotion, UpBlockMotion)):
            module.gradient_checkpointing = value

    def construct(
        self,
        sample: ms.Tensor,
        timestep: Union[ms.Tensor, float, int],
        encoder_hidden_states: ms.Tensor,
        timestep_cond: Optional[ms.Tensor] = None,
        attention_mask: Optional[ms.Tensor] = None,
        cross_attention_kwargs: Optional[Dict[str, Any]] = None,
        added_cond_kwargs: Optional[Dict[str, ms.Tensor]] = None,
        down_block_additional_residuals: Optional[Tuple[ms.Tensor]] = None,
        mid_block_additional_residual: Optional[ms.Tensor] = None,
        return_dict: bool = False,
    ) -> Union[UNet3DConditionOutput, Tuple[ms.Tensor]]:
        r"""
        The [`UNetMotionModel`] forward method.

        Args:
            sample (`ms.Tensor`):
                The noisy input tensor with the following shape `(batch, num_frames, channel, height, width`.
            timestep (`ms.Tensor` or `float` or `int`): The number of timesteps to denoise an input.
            encoder_hidden_states (`ms.Tensor`):
                The encoder hidden states with shape `(batch, sequence_length, feature_dim)`.
            timestep_cond: (`ms.Tensor`, *optional*, defaults to `None`):
                Conditional embeddings for timestep. If provided, the embeddings will be summed with the samples passed
                through the `self.time_embedding` layer to obtain the timestep embeddings.
            attention_mask (`ms.Tensor`, *optional*, defaults to `None`):
                An attention mask of shape `(batch, key_tokens)` is applied to `encoder_hidden_states`. If `1` the mask
                is kept, otherwise if `0` it is discarded. Mask will be converted into a bias, which adds large
                negative values to the attention scores corresponding to "discard" tokens.
            cross_attention_kwargs (`dict`, *optional*):
                A kwargs dictionary that if specified is passed along to the `AttentionProcessor` as defined under
                `self.processor` in
                [diffusers.models.attention_processor](https://github.com/huggingface/diffusers/blob/main/src/diffusers/models/attention_processor.py).
            down_block_additional_residuals: (`tuple` of `ms.Tensor`, *optional*):
                A tuple of tensors that if specified are added to the residuals of down unet blocks.
            mid_block_additional_residual: (`ms.Tensor`, *optional*):
                A tensor that if specified is added to the residual of the middle unet block.
            return_dict (`bool`, *optional*, defaults to `True`):
                Whether or not to return a [`~models.unets.unet_3d_condition.UNet3DConditionOutput`] instead of a plain
                tuple.

        Returns:
            [`~models.unets.unet_3d_condition.UNet3DConditionOutput`] or `tuple`:
                If `return_dict` is True, an [`~models.unets.unet_3d_condition.UNet3DConditionOutput`] is returned,
                otherwise a `tuple` is returned where the first element is the sample tensor.
        """
        # By default samples have to be AT least a multiple of the overall upsampling factor.
        # The overall upsampling factor is equal to 2 ** (# num of upsampling layears).
        # However, the upsampling interpolation output size can be forced to fit any upsampling size
        # on the fly if necessary.
        default_overall_up_factor = 2**self.num_upsamplers

        # upsample size should be forwarded when sample is not a multiple of `default_overall_up_factor`
        forward_upsample_size = False
        upsample_size = None

        if sample.shape[-2] % default_overall_up_factor != 0 or sample.shape[-1] % default_overall_up_factor != 0:
            forward_upsample_size = True

        # prepare attention_mask
        if attention_mask is not None:
            attention_mask = (1 - attention_mask.to(sample.dtype)) * -10000.0
            attention_mask = attention_mask.unsqueeze(1)

        # 1. time
        timesteps = timestep
        if not ops.is_tensor(timesteps):
            # TODO: this requires sync between CPU and GPU. So try to pass timesteps as tensors if you can
            if isinstance(timestep, float):
                dtype = ms.float64
            else:
                dtype = ms.int64
            timesteps = ms.Tensor([timesteps], dtype=dtype)
        elif len(timesteps.shape) == 0:
            timesteps = timesteps[None]

        # broadcast to batch dimension in a way that's compatible with ONNX/Core ML
        num_frames = sample.shape[2]
        timesteps = timesteps.broadcast_to((sample.shape[0],))

        t_emb = self.time_proj(timesteps)

        # timesteps does not contain any weights and will always return f32 tensors
        # but time_embedding might actually be running in fp16. so we need to cast here.
        # there might be better ways to encapsulate this.
        t_emb = t_emb.to(dtype=self.dtype)

        emb = self.time_embedding(t_emb, timestep_cond)
        aug_emb = None

        if self.config["addition_embed_type"] == "text_time":
            if "text_embeds" not in added_cond_kwargs:
                raise ValueError(
                    f"{self.__class__} has the config param `addition_embed_type` set to 'text_time' which requires the keyword argument `text_embeds` to be passed in `added_cond_kwargs`"  # noqa: E501
                )

            text_embeds = added_cond_kwargs.get("text_embeds")
            if "time_ids" not in added_cond_kwargs:
                raise ValueError(
                    f"{self.__class__} has the config param `addition_embed_type` set to 'text_time' which requires the keyword argument `time_ids` to be passed in `added_cond_kwargs`"  # noqa: E501
                )
            time_ids = added_cond_kwargs.get("time_ids")
            time_embeds = self.add_time_proj(time_ids.flatten()).to(text_embeds.dtype)
            time_embeds = time_embeds.reshape((text_embeds.shape[0], -1))

            add_embeds = ops.concat([text_embeds, time_embeds], axis=-1)
            add_embeds = add_embeds.to(emb.dtype)
            aug_emb = self.add_embedding(add_embeds)

        emb = emb if aug_emb is None else emb + aug_emb
        emb = emb.repeat_interleave(repeats=num_frames, dim=0)
        encoder_hidden_states = encoder_hidden_states.repeat_interleave(repeats=num_frames, dim=0)

        if self.encoder_hid_proj is not None and self.config["encoder_hid_dim_type"] == "ip_image_proj":
            if "image_embeds" not in added_cond_kwargs:
                raise ValueError(
                    f"{self.__class__} has the config param `encoder_hid_dim_type` set to "
                    f"'ip_image_proj' which requires the keyword argument `image_embeds` to be passed in  `added_conditions`"
                )
            image_embeds = added_cond_kwargs.get("image_embeds")
            image_embeds = self.encoder_hid_proj(image_embeds)
            image_embeds = [image_embed.repeat_interleave(repeats=num_frames, dim=0) for image_embed in image_embeds]
            encoder_hidden_states = (encoder_hidden_states, image_embeds)

        # 2. pre-process
        sample = sample.permute(0, 2, 1, 3, 4).reshape((sample.shape[0] * num_frames, -1) + sample.shape[3:])
        sample = self.conv_in(sample)

        # 3. down
        down_block_res_samples = (sample,)
        for downsample_block in self.down_blocks:
            if downsample_block.has_cross_attention:
                sample, res_samples = downsample_block(
                    hidden_states=sample,
                    temb=emb,
                    encoder_hidden_states=encoder_hidden_states,
                    attention_mask=attention_mask,
                    num_frames=num_frames,
                    cross_attention_kwargs=cross_attention_kwargs,
                )
            else:
                sample, res_samples = downsample_block(hidden_states=sample, temb=emb, num_frames=num_frames)

            down_block_res_samples += res_samples

        if down_block_additional_residuals is not None:
            new_down_block_res_samples = ()

            for down_block_res_sample, down_block_additional_residual in zip(
                down_block_res_samples, down_block_additional_residuals
            ):
                down_block_res_sample = down_block_res_sample + down_block_additional_residual
                new_down_block_res_samples += (down_block_res_sample,)

            down_block_res_samples = new_down_block_res_samples

        # 4. mid
        if self.mid_block is not None:
            # To support older versions of motion modules that don't have a mid_block
            if self.mid_block.has_motion_modules:
                sample = self.mid_block(
                    sample,
                    emb,
                    encoder_hidden_states=encoder_hidden_states,
                    attention_mask=attention_mask,
                    num_frames=num_frames,
                    cross_attention_kwargs=cross_attention_kwargs,
                )
            else:
                sample = self.mid_block(
                    sample,
                    emb,
                    encoder_hidden_states=encoder_hidden_states,
                    attention_mask=attention_mask,
                    cross_attention_kwargs=cross_attention_kwargs,
                )

        if mid_block_additional_residual is not None:
            sample = sample + mid_block_additional_residual

        # 5. up
        for i, upsample_block in enumerate(self.up_blocks):
            is_final_block = i == len(self.up_blocks) - 1

            res_samples = down_block_res_samples[-self.layers_per_resnet_in_up_blocks[i] :]
            down_block_res_samples = down_block_res_samples[: -self.layers_per_resnet_in_up_blocks[i]]

            # if we have not reached the final block and need to forward the
            # upsample size, we do it here
            if not is_final_block and forward_upsample_size:
                upsample_size = down_block_res_samples[-1].shape[2:]

            if upsample_block.has_cross_attention:
                sample = upsample_block(
                    hidden_states=sample,
                    temb=emb,
                    res_hidden_states_tuple=res_samples,
                    encoder_hidden_states=encoder_hidden_states,
                    upsample_size=upsample_size,
                    attention_mask=attention_mask,
                    num_frames=num_frames,
                    cross_attention_kwargs=cross_attention_kwargs,
                )
            else:
                sample = upsample_block(
                    hidden_states=sample,
                    temb=emb,
                    res_hidden_states_tuple=res_samples,
                    upsample_size=upsample_size,
                    num_frames=num_frames,
                )

        # 6. post-process
        if self.conv_norm_out:
            sample = self.conv_norm_out(sample)
            sample = self.conv_act(sample)

        sample = self.conv_out(sample)

        # reshape to (batch, channel, framerate, width, height)
        sample = sample[None, :].reshape((-1, num_frames) + sample.shape[1:]).permute(0, 2, 1, 3, 4)

        if not return_dict:
            return (sample,)

        return UNet3DConditionOutput(sample=sample)
