# coding=utf-8
# Copyright 2023 The HuggingFace Team. All rights reserved.
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

import os

import torch
from accelerate.state import AcceleratorState, PartialState
from accelerate.utils import is_deepspeed_available, parse_choice_from_env, parse_flag_from_env

from optimum.utils import logging

from ..distributed import parallel_state
from .utils import GaudiDistributedType


logger = logging.get_logger()


class GaudiPartialState(PartialState):
    """
    Adapted from: https://github.com/huggingface/accelerate/blob/8514c35192ac9762920f1ab052e5cea4c0e46eeb/src/accelerate/state.py#L96
    """

    def __init__(self, cpu: bool = False, **kwargs):
        self.__dict__ = self._shared_state
        if not self.initialized:
            self._cpu = cpu
            self.backend = None
            env_device = os.environ.get("ACCELERATE_TORCH_DEVICE", None)
            self.device = torch.device(env_device) if env_device is not None else None
            self.debug = parse_flag_from_env("ACCELERATE_DEBUG_MODE")

            # initialize_distributed_hpu is already called in the __init__ of
            # habana_frameworks.torch.distributed.hccl
            # It is necessary so that the env variable LOCAL_RANK is set before the
            # conditional statement right below
            from habana_frameworks.torch.distributed.hccl import initialize_distributed_hpu

            if int(os.environ.get("LOCAL_RANK", -1)) != -1 and not cpu:
                world_size, rank, local_rank = initialize_distributed_hpu()
                self.backend = kwargs.pop("backend", "hccl")
                context_parallel_size = kwargs.pop("context_parallel_size", 1)
                self.minimize_memory = kwargs.pop("minimize_memory", False)
                if os.environ.get("ACCELERATE_USE_DEEPSPEED", "false") == "true":
                    if not is_deepspeed_available():
                        raise ImportError(
                            "DeepSpeed is not available, install it with: `pip install"
                            " git+https://github.com/HabanaAI/DeepSpeed.git@1.20.0`."
                        )
                    self.distributed_type = GaudiDistributedType.DEEPSPEED
                    import deepspeed

                    if world_size > 1:
                        # override HLS_MODULE_ID only if it's not previously set by bridge
                        if "HLS_MODULE_ID" not in os.environ:
                            os.environ["HLS_MODULE_ID"] = str(local_rank)
                        os.environ["ID"] = str(rank)

                    deepspeed.init_distributed(dist_backend=self.backend, **kwargs)
                    logger.info("DeepSpeed is enabled.")
                    self._mixed_precision = "no"  # deepspeed handles mixed_precision using deepspeed_config
                elif os.environ.get("ACCELERATE_USE_FSDP", "false") == "true":
                    self.distributed_type = GaudiDistributedType.FSDP
                    if not torch.distributed.is_initialized():
                        torch.distributed.init_process_group(backend=self.backend, rank=rank, world_size=world_size)
                        logger.info("Enabled distributed run.")
                else:
                    self.distributed_type = GaudiDistributedType.MULTI_HPU
                    if not torch.distributed.is_initialized():
                        torch.distributed.init_process_group(backend=self.backend, rank=rank, world_size=world_size)
                        logger.info("Enabled distributed run.")
                self.num_processes = world_size
                self.process_index = rank
                self.local_process_index = local_rank
                if self.device is None:
                    # TODO: replace by `torch.device("hpu", self.local_process_index)` when hpu:x is supported
                    self.device = torch.device("hpu")
                if not is_deepspeed_available():
                    context_parallel_size = 1
                if parallel_state.is_unitialized():
                    parallel_state.initialize_model_parallel(
                        sequence_parallel_size=context_parallel_size, use_fp8=False
                    )
                else:
                    if parallel_state.get_sequence_parallel_world_size() != context_parallel_size:
                        raise ValueError(
                            "The initialized sequence parallel world size does not match the context parallel size."
                        )
                    if parallel_state.amax_reduction_is_initialized():
                        logger.info("FP8 amax reduction group is already initialized.")
            else:
                self.distributed_type = (
                    GaudiDistributedType.NO
                    if os.environ.get("ACCELERATE_USE_DEEPSPEED", "false") == "false"
                    else GaudiDistributedType.DEEPSPEED
                )
                self.num_processes = 1
                self.process_index = self.local_process_index = 0
                logger.info("Single-device run.")

                if self.device is None:
                    self.device = torch.device("cpu") if cpu else self.default_device

        self.fork_launched = parse_flag_from_env("FORK_LAUNCHED", 0)

    def wait_for_everyone(self):
        """
        Will stop the execution of the current process until every other process has reached that point (so this does
        nothing when the script is only run in one process). Useful to do before saving a model.

        Example:

        ```python
        >>> # Assuming two GPU processes
        >>> import time
        >>> from accelerate.state import PartialState

        >>> state = PartialState()
        >>> if state.is_main_process:
        ...     time.sleep(2)
        >>> else:
        ...     print("I'm waiting for the main process to finish its sleep...")
        >>> state.wait_for_everyone()
        >>> # Should print on every process at the same time
        >>> print("Everyone is here")
        ```
        """
        if self.distributed_type in (
            GaudiDistributedType.DEEPSPEED,
            GaudiDistributedType.MULTI_HPU,
            GaudiDistributedType.FSDP,
        ):
            torch.distributed.barrier()

    @property
    def default_device(self) -> torch.device:
        """
        Returns the default device which is:
        - HPU if it is available
        - CPU otherwise
        """
        import habana_frameworks.torch.hpu as hthpu

        if hthpu.is_available():
            return torch.device("hpu")
        else:
            return torch.device("cpu")


class GaudiAcceleratorState(AcceleratorState):
    """
    Adapted from: https://github.com/huggingface/accelerate/blob/8514c35192ac9762920f1ab052e5cea4c0e46eeb/src/accelerate/state.py#L683
    """

    def __init__(
        self,
        mixed_precision: str = None,
        cpu: bool = False,
        dynamo_plugin=None,
        deepspeed_plugin=None,
        fsdp_plugin=None,
        megatron_lm_plugin=None,
        _from_accelerator: bool = False,
        **kwargs,
    ):
        self.__dict__ = self._shared_state
        if parse_flag_from_env("ACCELERATE_USE_CPU"):
            cpu = True
        if GaudiPartialState._shared_state == {}:
            GaudiPartialState(cpu, **kwargs)
        self.__dict__.update(GaudiPartialState._shared_state)
        self._check_initialized(mixed_precision, cpu)
        if not self.initialized:
            self.deepspeed_plugin = None
            self.use_ipex = None
            mixed_precision = (
                parse_choice_from_env("ACCELERATE_MIXED_PRECISION", "no")
                if mixed_precision is None
                else mixed_precision.lower()
            )
            self.is_fp8_enabled = mixed_precision == "fp8"
            self.dynamo_plugin = dynamo_plugin
            # deepspeed handles mixed_precision using deepspeed_config
            self._mixed_precision = (
                "no" if self.distributed_type == GaudiDistributedType.DEEPSPEED else mixed_precision
            )
            if os.environ.get("ACCELERATE_USE_DEEPSPEED", "false") == "true" and not cpu:
                self.deepspeed_plugin = deepspeed_plugin
            if os.environ.get("ACCELERATE_USE_FSDP", "false") == "true" and not cpu:
                if self._mixed_precision != "no":
                    fsdp_plugin.set_mixed_precision(self._mixed_precision)
                self.fsdp_plugin = fsdp_plugin
            GaudiPartialState._shared_state["distributed_type"] = self.distributed_type
            self.use_ipex = False

    @property
    def mixed_precision(self):
        if self.distributed_type == GaudiDistributedType.DEEPSPEED:
            config = self.deepspeed_plugin.deepspeed_config
            if config.get("fp16", {}).get("enabled", False):
                mixed_precision = "fp16"
            elif config.get("bf16", {}).get("enabled", False):
                mixed_precision = "bf16"
            else:
                mixed_precision = "no"
        else:
            mixed_precision = self._mixed_precision

        if mixed_precision == "fp16":
            raise ValueError("fp16 is not supported on Habana Gaudi.")

        return mixed_precision
