# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

import logging
from itertools import chain
from typing import cast, List, Optional, Tuple

import torch
from executorch.backends.transforms import get_shape
from executorch.backends.xnnpack.operators.quant_params import QuantParams
from executorch.backends.xnnpack.partition.config.xnnpack_config import (
    ConfigPrecisionType,
    XNNPartitionerConfig,
)
from executorch.backends.xnnpack.utils.quant_utils import (
    extract_qdq_affine_op_args_for_decomposed_ops,
    is_affine_qdq,
    is_dequant,
    is_dynamic_qdq,
    is_per_channel,
    is_per_channel_group,
    is_per_tensor,
    is_qparam,
    is_quant,
    tag_as_implicit_q_dq,
)
from executorch.backends.xnnpack.utils.utils import (
    get_input_node,
    is_depthwise_conv,
    is_getitem,
    is_node,
    is_param_node,
)
from executorch.exir.backend.canonical_partitioners.config_partitioner import (
    format_target_name,
)
from executorch.exir.backend.utils import WhyNoPartition
from torch.export import ExportedProgram
from torch.fx.passes.utils.source_matcher_utils import (
    get_source_partitions,
    SourcePartition,
)

logger = logging.getLogger(__name__)
why = WhyNoPartition(logger=logger)


class GEMMConfig(XNNPartitionerConfig):
    """
    GEMM-like ops like Convolution, Addmm, Linear, mostly behave in the same way, in which we
    have some weight, bias, and activation node. The only difference between these types
    of ops are that the weight, bias, and activations are in different indicies of the
    nodes arguments, this class helps to generalize the logic needed to partition these
    different ops
    """

    def __init__(self, weight_idx, bias_idx, act_idx, fused_acts, **kwargs):
        super().__init__(**kwargs)
        self.weight_idx = weight_idx
        self.bias_idx = bias_idx
        self.act_idx = act_idx
        self.fused_acts = fused_acts

    def check_constraints(self, node: torch.fx.Node, ep: ExportedProgram) -> bool:
        if not self.check_common_constraints(node, ep):
            # short circuit if we don't pass common constraints
            return False

        is_valid, _ = self.get_deps(node, ep)
        return is_valid

    def get_node_and_deps(
        self, node: torch.fx.Node, ep: ExportedProgram
    ) -> List[torch.fx.Node]:
        partition = [node]
        _, deps = self.get_deps(node, ep)
        partition.extend(deps)

        return partition

    def get_original_aten(self) -> Optional[torch._ops.OpOverload]:
        return None

    def _detect_precision(self, node: torch.fx.Node) -> ConfigPrecisionType:
        weight = get_input_node(node, self.weight_idx)

        if not is_dequant(weight):
            return ConfigPrecisionType.FP32

        activation = get_input_node(node, self.act_idx)
        if is_dynamic_qdq(activation):
            return ConfigPrecisionType.DYNAMIC_QUANT

        return ConfigPrecisionType.STATIC_QUANT

    def _overwrite_precision(self, node: torch.fx.Node):
        precision = self._detect_precision(node)
        if precision not in self.enabled_precision_types:
            # detected precision is not enabled, try to partition it as fp32
            if self.enabled_precision_types == [ConfigPrecisionType.FP32]:
                # when only fp32 is enabled, then we can still partition fp32 gemms
                # even with in a quantized graph
                if precision in [
                    ConfigPrecisionType.STATIC_QUANT,
                    ConfigPrecisionType.DYNAMIC_QUANT,
                ]:
                    precision = ConfigPrecisionType.FP32
                    logging.info(f"Overwriting precision, partitioning {node} as FP32")
                    return True, precision

        return False, precision

    def get_deps(
        self,
        node: torch.fx.Node,
        ep: ExportedProgram,
    ) -> Tuple[bool, List[torch.fx.Node]]:
        """
        Gets all dependencies for this gemm partition. Returns a tuple of
        a bool indicating if the deps are valid and a list of all the
        dep nodes
        """
        precision = self._detect_precision(node)
        if precision not in self.supported_precision_types():
            # detected precision but it is either disabled or not supported
            why(node, f"Unsupported precision type {precision}")
            return (False, [])
        _, precision = self._overwrite_precision(node)
        valid_bias, bias_deps = self._get_bias_deps(node, ep, precision)
        valid_weight, weight_deps = self._get_weight_deps(node, ep, precision)
        valid_act, act_deps = self._get_act_deps(node, ep, precision)
        valid_output, output_deps = self._get_output_deps(node, ep, precision)

        valid_deps = valid_bias and valid_weight and valid_act and valid_output
        deps = list(chain(bias_deps, weight_deps, act_deps, output_deps))

        # Tag q/dq nodes as implicit q/dq nodes
        for dep in deps:
            if is_dequant(dep) or is_quant(dep):
                tag_as_implicit_q_dq(dep)

        return valid_deps, deps

    def _get_weight_deps(
        self, node: torch.fx.Node, ep: ExportedProgram, precision: ConfigPrecisionType
    ) -> Tuple[bool, List[torch.fx.Node]]:
        gemm_deps = []
        if precision == ConfigPrecisionType.FP32:
            # First find the weight
            weight_node = get_input_node(node, self.weight_idx)
            if not is_param_node(ep, weight_node):
                why(node, "Expected weight to be a static param")
                return (False, [])
            gemm_deps.append(weight_node)

            return (True, gemm_deps)
        else:
            # Quantized Weight deps
            dequant_node = get_input_node(node, self.weight_idx)
            if not is_dequant(dequant_node):
                why(node, "Expected  weight to have a dequantized node")
                return False, []
            gemm_deps.append(dequant_node)
            weight = get_input_node(dequant_node, 0)
            if not is_param_node(ep, weight):
                why(node, "Expected weight to be a static param")
                return False, []
            gemm_deps.append(weight)

            if (
                is_per_tensor(dequant_node)
                and precision == ConfigPrecisionType.DYNAMIC_QUANT
            ):
                why(
                    node,
                    "XNNPACK does not support per tensor quantized weights for dynamic quantization of activations",
                )
                return False, []

            if is_per_channel(dequant_node) or is_per_channel_group(dequant_node):
                if len(dequant_node.all_input_nodes) < 2:
                    # Expected channel quantized to have scale/zp nodes
                    why(node, "Expected channel quantized to have scale/zp nodes")
                    return False, []

                gemm_deps.extend(dequant_node.all_input_nodes[1:3])

            return (True, gemm_deps)

    def _get_output_deps(
        self, node: torch.fx.Node, ep: ExportedProgram, precision: ConfigPrecisionType
    ) -> Tuple[bool, List[torch.fx.Node]]:
        gemm_deps = []
        if precision == ConfigPrecisionType.STATIC_QUANT:
            # Look for fused activations and tail end quant node
            node_users = list(node.users.keys())
            if len(node_users) != 1:
                why(node, "Expected quantized node to have a single output")
                return False, []

            # Check if the quantized pattern has a fused activation
            n_output = node_users[0]
            if (
                n_output.op == "call_function"
                and format_target_name(n_output.target.__name__) in self.fused_acts
            ):
                gemm_deps.append(n_output)
                fused_out_users = list(n_output.users.keys())
                if len(fused_out_users) == 1:
                    n_output = fused_out_users[0]

            if not is_quant(n_output):
                # Expected gemm_node --> fused_act (optional) --> dequant
                why(node, "Expected output node to have a dequantized node")
                return (False, [])
            gemm_deps.append(n_output)
        elif precision == ConfigPrecisionType.FP32:
            # Look for fused activations only, and partition with fp32 op
            node_users = list(node.users.keys())
            if len(node_users) == 1:
                n_output = node_users[0]
                if (
                    n_output.op == "call_function"
                    and format_target_name(n_output.target.__name__) in self.fused_acts
                ):
                    gemm_deps.append(n_output)

        # FP32 and Dynamic Quant have no output dependencies
        return (True, gemm_deps)

    def _get_bias_deps(
        self, node: torch.fx.Node, ep: ExportedProgram, precision: ConfigPrecisionType
    ) -> Tuple[bool, List[torch.fx.Node]]:
        gemm_deps = []
        if (
            precision == ConfigPrecisionType.FP32
            and self.force_non_static_weights_for_f32_linear
        ):
            # if force_non_static_weights_for_f32_linear is enabled, then we
            # do not partition the weight node
            return (True, gemm_deps)

        if len(node.all_input_nodes) > 2 and self.bias_idx is not None:
            bias_node = get_input_node(node, self.bias_idx)
            if bias_node:
                if not is_param_node(ep, bias_node):
                    why(node, "Expected bias to be a static param")
                    return (False, [])
                gemm_deps.append(bias_node)

        return (True, gemm_deps)

    def _get_act_deps(
        self, node: torch.fx.Node, ep: ExportedProgram, precision: ConfigPrecisionType
    ) -> Tuple[bool, List[torch.fx.Node]]:
        gemm_deps = []
        if precision == ConfigPrecisionType.FP32:
            return (True, [])
        else:
            dq_input = get_input_node(node, self.act_idx)
            if not is_dequant(dq_input):
                why(node, "Expected act input to be dequant node")
                return False, []
            gemm_deps.append(dq_input)
            if precision == ConfigPrecisionType.STATIC_QUANT:
                # if static quant we are done after finding first dq_input
                return (True, gemm_deps)

            # q input node
            q_input = get_input_node(dq_input, 0)
            if not is_quant(q_input):
                why(node, "Expected  dequant input to be quant node")
                return (False, [])
            gemm_deps.append(q_input)
            q_input_args = q_input.args
            if is_affine_qdq(q_input):
                q_input_args = extract_qdq_affine_op_args_for_decomposed_ops(q_input)
            if not (is_node(q_input_args[1]) and is_node(q_input_args[2])):
                why(node, "expected to find getitem node from choose qparam")
                return (False, [])

            getitem1 = q_input_args[1]
            getitem2 = q_input_args[2]

            if not (is_getitem(getitem1) and is_getitem(getitem2)):
                why(node, "expected getitem node from choose qparam")
                return (False, [])

            gemm_deps.extend([getitem1, getitem2])
            choose_qparam = get_input_node(getitem1, 0)
            if not is_qparam(choose_qparam):
                why(node, "expected to find choose_qparam node")
                return (False, [])
            gemm_deps.append(choose_qparam)
            return (True, gemm_deps)


class LinearConfig(GEMMConfig):
    target_name = "linear.default"

    def __init__(self, **kwargs):
        super().__init__(
            weight_idx=1,
            bias_idx=2,
            act_idx=0,
            fused_acts=["relu.default", "hardtanh.default"],
            **kwargs,
        )

    def get_original_aten(self) -> Optional[torch._ops.OpOverload]:
        return torch.ops.aten.linear.default

    def _get_weight_deps(
        self, node: torch.fx.Node, ep: ExportedProgram, precision: ConfigPrecisionType
    ) -> Tuple[bool, List[torch.fx.Node]]:
        if (
            precision == ConfigPrecisionType.FP32
            and self.force_non_static_weights_for_f32_linear
        ):
            # if force_non_static_weights_for_f32_linear is enabled, then we
            # do not partition the weight node
            return (True, [])

        # Since we are in Linear, we may assume that the weights are indeed static.
        overwritten_linear_precision, new_precision = self._overwrite_precision(node)
        if new_precision == ConfigPrecisionType.FP32 and overwritten_linear_precision:
            # if overwriting quantized precision to fp32, then we
            # do not partition the weight node
            return (True, [])

        return super()._get_weight_deps(node, ep, precision)

    def supported_precision_types(self):
        return [
            ConfigPrecisionType.DYNAMIC_QUANT,
            ConfigPrecisionType.FP32,
            ConfigPrecisionType.STATIC_QUANT,
        ]


class ConvolutionConfig(GEMMConfig):
    target_name = "convolution.default"

    def __init__(self, **kwargs):
        super().__init__(
            weight_idx=1,
            bias_idx=2,
            act_idx=0,
            fused_acts=["relu.default", "hardtanh.default"],
            **kwargs,
        )

    def check_constraints(self, node: torch.fx.Node, ep: ExportedProgram) -> bool:
        """
        Currently we have no support for convolution 3d
        """
        if not super().check_constraints(node, ep):
            return False

        conv_stride = cast(List[int], node.args[3])
        if len(conv_stride) > 2:
            why(node, "Only support 1D + 2D Conv")
            return False  # Only support 1D + 2D Conv

        kernel_node = get_input_node(node, 1)
        kernel_shape = get_shape(kernel_node)
        weight_quant_params = QuantParams.from_weights(kernel_node, ep)
        groups = cast(int, node.args[8])
        is_transpose = node.args[6]

        # XNNPACK does not support dynamic quantization convs that are not 2D or are depthwise
        if self._detect_precision(node) == ConfigPrecisionType.DYNAMIC_QUANT and (
            len(conv_stride) != 2
            or is_depthwise_conv(kernel_shape, groups, is_transpose)
        ):
            why(
                node,
                "XNNPACK only supports standard 2D convolutions for dynamic quantization",
            )
            return False

        # XNNPACK does not support non-zero output padding in transposed
        # convolutions.
        if is_transpose and any(
            out_pad != 0 for out_pad in cast(List[int], node.args[7])
        ):
            why(
                node,
                "XNNPACK does not support transposed convolutions with"
                "non-zero output padding",
            )
            return False

        if (
            is_transpose
            and weight_quant_params is not None
            and weight_quant_params.per_channel
            and (groups > 1 or weight_quant_params.axis != 1)
        ):
            why(
                node,
                "XNNPACK does not support per input channel quantization"
                "for transpose convolutions with groups > 1",
            )
            return False
        return True

    def supported_precision_types(self):
        return [
            ConfigPrecisionType.FP32,
            ConfigPrecisionType.STATIC_QUANT,
            ConfigPrecisionType.DYNAMIC_QUANT,
        ]


class AddmmConfig(GEMMConfig):
    """
    We will handle the legacy form of addmm partitioning which will include
    partitioning using source partitions.
    """

    target_name = "addmm.default"

    def __init__(self, **kwargs):
        super().__init__(
            weight_idx=2,
            bias_idx=0,
            act_idx=1,
            fused_acts=["relu.default", "hardtanh.default"],
            **kwargs,
        )
        self.src_partitions = None
        self.linear_modules = [torch.nn.functional.linear, torch.nn.Linear]

    def _get_weight_deps(
        self, node: torch.fx.Node, ep: ExportedProgram, precision: ConfigPrecisionType
    ) -> Tuple[bool, List[torch.fx.Node]]:
        if (
            precision == ConfigPrecisionType.FP32
            and self.force_non_static_weights_for_f32_linear
        ):
            # if force_non_static_weights_for_f32_linear is on and we detected this as fp32, then we
            # do not partition the weight node
            return (True, [])

        return super()._get_weight_deps(node, ep, precision)

    def get_deps(
        self,
        node: torch.fx.Node,
        ep: ExportedProgram,
    ) -> Tuple[bool, List[torch.fx.Node]]:
        """
        Gets all dependencies for this gemm partition. Returns a tuple of
        a bool indicating if the deps are valid and a list of all the
        dep nodes. This handles the src partition for
        """
        if self.src_partitions is None:
            # Cache src partitions so we don't have to recompute them every time
            self.src_partitions = get_source_partitions(ep.graph, self.linear_modules)

        # src_partition is None if node is not in source partition,
        # otherwise gives us the linear source partition it belongs to
        src_partition = None
        for partition_list in self.src_partitions.values():
            for partition in partition_list:
                if node in partition.nodes:
                    src_partition = partition

        if src_partition:
            # if addmm belongs to linear src partition, then partition the
            # src partition and get its deps
            return self.get_deps_from_src_partition(node, ep, src_partition)

        return super().get_deps(node, ep)

    def get_deps_from_src_partition(
        self, node: torch.fx.Node, ep: ExportedProgram, src_partition: SourcePartition
    ):
        """
        Gets all the dependencies for the src partition. This is done by simulating the
        linear node from the src partition. We find the associated weights, act, bias
        from the linear src partition, and plug those in as the addmm node's args. We also
        take the users of the src partitions output node as the addmm node's users. Finally
        we just run the GEMMConfig's get_deps method no this faked linear node. After
        getting the deps, we return the addmm nodes users and args back.
        """

        def find_partition_args(input_node):
            while (
                len(input_node.all_input_nodes) != 0
                and input_node not in src_partition.input_nodes
            ):
                input_node = input_node.all_input_nodes[0]
            return input_node

        old_args, old_users = node.args, node.users

        fake_args = []
        for arg in node.args:
            # map addmm's args to the source partition's inputs
            # basically simulating what the args of the linear node would be
            fake_args.append(find_partition_args(arg))

        # validate source partition
        if (
            # bias must be in source partition
            (self.bias_idx and fake_args[self.bias_idx] not in src_partition.nodes)
            # activation input must be an input node to partition
            or fake_args[self.act_idx] not in src_partition.input_nodes
            # weight can either be in the nodes or input_nodes
            or fake_args[self.weight_idx]
            not in (src_partition.nodes + src_partition.input_nodes)
            # there can only be a single output node in partition
            or len(src_partition.output_nodes) != 1
        ):
            why(node, "invalid source partition")
            return (False, [])

        # map addmm's args to the source partition linear's inputs and users
        node.args = tuple(fake_args)
        node.users = src_partition.output_nodes[0].users
        valid_deps, deps = super().get_deps(node, ep)

        # Reset addmm node back to old args and users
        node.args = old_args
        node.users = old_users

        # When using force_non_static_weights_for_f32_linear, we want to get_deps to overwrite the source partition nodes.
        # Else we want to be greedy.
        ret_deps = (
            list(set(deps) & set(src_partition.nodes))
            if self.force_non_static_weights_for_f32_linear
            else list(set(deps) | set(src_partition.nodes))
        )

        return valid_deps, ret_deps

    def supported_precision_types(self):
        return [
            ConfigPrecisionType.FP32,
            ConfigPrecisionType.STATIC_QUANT,
            ConfigPrecisionType.DYNAMIC_QUANT,
        ]


class MMConfig(AddmmConfig):
    target_name = "mm.default"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.bias_idx = None
        self.weight_idx = 1
        self.act_idx = 0

    def _get_weight_deps(
        self, node: torch.fx.Node, ep: ExportedProgram, precision: ConfigPrecisionType
    ) -> Tuple[bool, List[torch.fx.Node]]:
        if (
            precision == ConfigPrecisionType.FP32
            and self.force_non_static_weights_for_f32_linear
        ):
            # if force_non_static_weights_for_f32_linear is on and we detected this as fp32, then we
            # do not partition the weight node
            return (True, [])

        return super()._get_weight_deps(node, ep, precision)

    def supported_precision_types(self):
        return [
            ConfigPrecisionType.FP32,
            ConfigPrecisionType.STATIC_QUANT,
            ConfigPrecisionType.DYNAMIC_QUANT,
        ]
