# Copyright 2023 Huawei Technologies Co., Ltd
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
"""poly layer"""
import math
from typing import Any

import mindspore
from mindspore import nn, ops
from mindspore.common.initializer import HeUniform, Zero, initializer

from mindnlp._legacy.abc import ParameterDict
from mindnlp.peft.tuners.tuners_utils import BaseTunerLayer

from .config import PolyConfig
from .router import get_router


class PolyLayer(BaseTunerLayer):
    # All names of layers that may contain (trainable) adapter weights
    adapter_layer_names = ("poly_lora_A", "poly_lora_B", "poly_router")
    # All names of other parameters that may contain adapter-related parameters
    other_param_names = ("r", "n_tasks", "n_skills", "n_splits")

    def __init__(self, base_layer: nn.Cell, **kwargs):
        self.base_layer = base_layer
        self.r = {}
        self.n_tasks = {}
        self.n_skills = {}
        self.n_splits = {}
        self.poly_type = {}
        self.poly_router = nn.CellDict()
        self.poly_lora_A = ParameterDict()
        self.poly_lora_B = ParameterDict()
        self.kwargs = kwargs

        base_layer = self.get_base_layer()
        if isinstance(base_layer, nn.Dense):
            in_channels, out_channels = base_layer.in_channels, base_layer.out_channels
        else:
            raise ValueError(f"Unsupported layer type {type(base_layer)}")

        self.in_channels = in_channels
        self.out_channels = out_channels

    def update_layer(self, adapter_name, poly_config):
        if poly_config.r <= 0:
            raise ValueError(
                f"`r` should be a positive integer value but the value passed is {poly_config.r}"
            )

        self.r[adapter_name] = poly_config.r
        self.n_tasks[adapter_name] = poly_config.n_tasks
        self.n_skills[adapter_name] = poly_config.n_skills
        self.n_splits[adapter_name] = poly_config.n_splits
        self.poly_type[adapter_name] = poly_config.poly_type

        self.poly_lora_A[adapter_name] = mindspore.Parameter(
            ops.zeros(
                poly_config.n_splits,
                poly_config.n_skills,
                self.in_channels // poly_config.n_splits,
                poly_config.r,
            )
        )
        self.poly_lora_B[adapter_name] = mindspore.Parameter(
            ops.zeros(
                poly_config.n_splits,
                poly_config.n_skills,
                poly_config.r,
                self.out_channels // poly_config.n_splits,
            )
        )
        self.poly_router[adapter_name] = get_router(poly_config)

        self.reset_poly_parameters(adapter_name, init_weights=poly_config.init_weights)

        # self._move_adapter_to_device_of_base_layer(adapter_name)
        self.set_adapter(self.active_adapters)

    def reset_poly_parameters(self, adapter_name, init_weights):
        if adapter_name in self.poly_lora_A.keys():
            # initialize A the same way as the default for nn.Dense
            # https://github.com/microsoft/mttl/blob/ce4ca51dbca73be656feb9b3e5233633e3c5dec7/mttl/models/poly.py#L269
            n_splits, n_skills, d, r = self.poly_lora_A[adapter_name].shape
            for skill in range(n_skills):
                for split in range(n_splits):
                    param = ops.zeros(r, d)
                    param = mindspore.Parameter(
                        initializer(HeUniform(math.sqrt(5)), param.shape, param.dtype)
                    )
                    self.poly_lora_A[adapter_name].data[split, skill, :, :] = param.T

            if init_weights:
                # initialize B to zero
                self.poly_lora_B[adapter_name] = mindspore.Parameter(
                    initializer(
                        Zero(),
                        self.poly_lora_B[adapter_name].shape,
                        self.poly_lora_B[adapter_name].dtype,
                    )
                )

            else:
                # initialize B the same way as the default for nn.Dense
                n_splits, n_skills, r, d = self.poly_lora_B[adapter_name].shape
                for skill in range(n_skills):
                    for split in range(n_splits):
                        param = ops.zeros(d, r)
                        param = initializer(
                            HeUniform(math.sqrt(5)), param.shape, param.dtype
                        )
                        self.poly_lora_B[adapter_name].data[
                            split, skill, :, :
                        ] = param.T

            # initialized router
            self.poly_router[adapter_name].reset()


class Dense(nn.Cell, PolyLayer):
    # Lora implemented in a dense layer
    def __init__(
        self,
        base_layer,
        adapter_name: str,
        poly_config: PolyConfig,
        **kwargs,
    ) -> None:
        super().__init__()
        PolyLayer.__init__(self, base_layer, **kwargs)

        self._active_adapter = adapter_name
        self.update_layer(adapter_name, poly_config)

    def construct(
        self,
        x: mindspore.Tensor,
        *args: Any,
        task_ids: mindspore.Tensor = None,
        **kwargs: Any,
    ) -> mindspore.Tensor:
        previous_dtype = x.dtype
        if self.disable_adapters:
            result = self.base_layer(x, *args, **kwargs)
        else:
            result = self.base_layer(x, *args, **kwargs)
            for active_adapter in self.active_adapters:
                if active_adapter not in self.poly_lora_A.keys():
                    continue

                r = self.r[active_adapter]
                poly_router = self.poly_router[active_adapter]
                poly_lora_A = self.poly_lora_A[active_adapter]
                poly_lora_B = self.poly_lora_B[active_adapter]

                # Combine the output of LoRAs
                # https://github.com/microsoft/mttl/blob/ce4ca51dbca73be656feb9b3e5233633e3c5dec7/mttl/models/poly.py#L293
                mixing_weights = poly_router(task_ids=task_ids, input_ids=x)
                bs, n_splits, n_skills = mixing_weights.size()

                # A is    n_splits, n_skills, D // n_splits, rank
                # we want bs,       n_splits, D // n_splits, rank
                A = ops.einsum("bqs,qsdr->bqdr", (mixing_weights, poly_lora_A))
                B = ops.einsum("bqs,qsrd->bqrd", (mixing_weights, poly_lora_B))

                A = A.reshape(bs, self.in_features, r)
                B = B.transpose(1, 2).reshape(bs, r, self.out_features)

                x = x.to(A.dtype)
                result += x.bmm(A).bmm(B) / r

        result = result.to(previous_dtype)
        return result

    def __repr__(self) -> str:
        rep = super().__repr__()
        return "poly." + rep
