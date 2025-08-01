# Copyright 2024 The JAX Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import ctypes
import dataclasses
import functools
import os
from typing import Any, overload

import numpy as np

from jax._src import core
from jax._src import dispatch
from jax._src import effects
from jax._src import util
from jax._src import xla_bridge
from jax._src.hashable_array import HashableArray
from jax._src.interpreters import ad
from jax._src.interpreters import batching
from jax._src.interpreters import mlir
from jax._src.layout import Layout
from jax._src.lib import jaxlib
from jax._src.lib import xla_client
from jax._src.lib.mlir import ir
from jax._src.typing import (Array, ArrayLike, DeprecatedArg, DuckTypedArray,
                             Shape)

map, unsafe_map = util.safe_map, map
FfiLayoutOptions = Sequence[int] | Layout | None


def register_ffi_target(
    name: str,
    fn: Any,
    platform: str = "cpu",
    api_version: int = 1,
    **kwargs: Any,
) -> None:
  """Registers a foreign function target.

  Args:
    name: the name of the target.
    fn: a ``PyCapsule`` object containing the function pointer, or a ``dict``
      where the keys are FFI stage names (e.g. `"execute"`) and the values are
      ``PyCapsule`` objects containing a pointer to the handler for that stage.
    platform: the target platform.
    api_version: the XLA custom call API version to use. Supported versions are:
      1 (default) for the typed FFI or 0 for the earlier "custom call" API.
    kwargs: any extra keyword arguments are passed directly to
      :func:`~jaxlib.xla_client.register_custom_call_target` for more advanced
      use cases.
  """
  return xla_client.register_custom_call_target(name, fn, platform, api_version,
                                                **kwargs)


def register_ffi_type_id(
    name: str,
    obj: Any,
    platform: str = "cpu",
) -> None:
  """Registers a custom type ID for a FFI target.

  Args:
    name: the name of the type ID. This name must be unique within the process.
    obj: a ``PyCapsule`` object encapsulating a pointer to the type ID.
    platform: the target platform.
  """
  return xla_client.register_custom_type_id(name, obj, platform=platform)


def register_ffi_target_as_batch_partitionable(name: str) -> None:
  """Registers an FFI target as batch partitionable.

  Args:
    name: the name of the target.
  """
  xla_client.register_custom_call_as_batch_partitionable(name)
  xla_bridge.register_plugin_callbacks(
      functools.partial(xla_client.register_custom_call_as_batch_partitionable,
                        name))


def pycapsule(funcptr):
  """Wrap a ctypes function pointer in a PyCapsule.

  The primary use of this function, and the reason why it lives with in the
  ``jax.ffi`` submodule, is to wrap function calls from external compiled
  libraries to be registered as XLA custom calls.

  Example usage::

    import ctypes
    import jax
    from jax.lib import xla_client

    libfoo = ctypes.cdll.LoadLibrary('./foo.so')
    xla_client.register_custom_call_target(
        name="bar",
        fn=jax.ffi.pycapsule(libfoo.bar),
        platform=PLATFORM,
        api_version=API_VERSION
    )

  Args:
    funcptr: A function pointer loaded from a dynamic library using ``ctypes``.

  Returns:
    An opaque ``PyCapsule`` object wrapping ``funcptr``.
  """
  destructor = ctypes.CFUNCTYPE(None, ctypes.py_object)
  builder = ctypes.pythonapi.PyCapsule_New
  builder.restype = ctypes.py_object
  builder.argtypes = (ctypes.c_void_p, ctypes.c_char_p, destructor)
  return builder(funcptr, None, destructor(0))


def include_dir() -> str:
  """Get the path to the directory containing header files bundled with jaxlib"""
  jaxlib_dir = os.path.dirname(os.path.abspath(jaxlib.__file__))
  return os.path.join(jaxlib_dir, "include")


def _aval_shape(aval: core.AbstractValue) -> Shape:
  return () if aval is core.abstract_token else core.physical_aval(aval).shape  # pytype: disable=attribute-error


def _convert_layout_for_lowering(
    aval: core.AbstractValue, layout: FfiLayoutOptions = None) -> Sequence[int]:
  """Convert a layout to the minor-to-major order used by the custom call API."""
  if layout is None:
    return tuple(reversed(range(len(_aval_shape(aval)))))
  elif isinstance(layout, Layout):
    if layout.tiling is not None:
      raise ValueError("The FFI does not support layouts with tiling")
    return layout.major_to_minor[::-1]
  else:
    return tuple(layout)


def build_ffi_lowering_function(
    call_target_name: str,
    *,
    operand_layouts: Sequence[FfiLayoutOptions] | None = None,
    result_layouts: Sequence[FfiLayoutOptions] | None = None,
    backend_config: Mapping[str, ir.Attribute] | str | None = None,
    skip_ffi_layout_processing: bool = False,
    **lowering_args: Any,
) -> Callable[..., ir.Operation]:
  """Build a lowering op for an foreign function interface (FFI) target.

  By default, this lowering rule can use the input and output abstract values to
  compute the input and output types and shapes for the custom call, assuming
  row-major layouts.

  Note that layouts passed to this function as tuples should be in
  minor-to-major order (as expected by XLA) rather than major-to-minor as used
  by :func:`~jax.ffi.ffi_call` and ``Layout``.

  If keyword arguments are passed to the lowering rule, these are treated as
  attributes, and added to `backend_config`.

  Args:
    call_target_name: The name of the custom call target.
    operand_layouts: A sequence of layouts (dimension orders) for each operand.
      By default, the operands are assumed to be row-major.
    result_layouts: A sequence of layouts (dimension orders) for each result.
      By default, the results are assumed to be row-major.
    backend_config: Configuration data for the custom call. Any keyword
      arguments passed to the lowering rule will added to this dictionary.
    lowering_args: Any other arguments to :func:`mlir.custom_call` will also be
      passed through if provided as extra arguments to this function.
    skip_ffi_layout_processing: If true, skip processing of operand and result
      layout arguments passed to the lowering rule.
  """

  def _lowering_op(
    ctx: mlir.LoweringRuleContext, *operands: ir.Value, **params: Any
  ) -> ir.Operation:
    kwargs = dict(lowering_args)
    kwargs.setdefault("api_version", 4)
    if kwargs["api_version"] >= 4:
      if backend_config is not None and not isinstance(backend_config, dict):
        raise ValueError(
            "When api_version > 4, backend_config must be a dictionary.")
      kwargs["backend_config"] = dict(
        backend_config or {}, **{k: mlir.ir_attribute(v) for k, v in params.items()})
    else:
      if params:
        raise ValueError(
            "The use of ffi_call attributes requires a custom call API version "
            f"of at least 4; got api_version={kwargs['api_version']}.")
      kwargs["backend_config"] = backend_config
    if "result_types" not in kwargs:
      kwargs["result_types"] = [mlir.aval_to_ir_type(aval) for aval in ctx.avals_out]
    if not skip_ffi_layout_processing:
      if operand_layouts is None:
        kwargs["operand_layouts"] = map(
            _convert_layout_for_lowering, ctx.avals_in
        )
      else:
        kwargs["operand_layouts"] = [
            _convert_layout_for_lowering(*args)
            for args in zip(ctx.avals_in, operand_layouts)
        ]
      if result_layouts is None:
        kwargs["result_layouts"] = map(
            _convert_layout_for_lowering, ctx.avals_out
        )
      else:
        kwargs["result_layouts"] = [
            _convert_layout_for_lowering(*args)
            for args in zip(ctx.avals_out, result_layouts)
        ]
    if "result_shapes" not in kwargs and not all(
        core.is_constant_shape(_aval_shape(aval)) for aval in ctx.avals_out):
      kwargs["result_shapes"] = [
          mlir.shape_tensor(mlir.eval_dynamic_shape_as_ivals(ctx, _aval_shape(aval)))
          for aval in ctx.avals_out]

    return mlir.custom_call(call_target_name, operands=operands, **kwargs)

  return _lowering_op


def ffi_lowering(
    call_target_name: str,
    *,
    operand_layouts: Sequence[FfiLayoutOptions] | None = None,
    result_layouts: Sequence[FfiLayoutOptions] | None = None,
    backend_config: Mapping[str, ir.Attribute] | str | None = None,
    skip_ffi_layout_processing: bool = False,
    **lowering_args: Any
) -> mlir.LoweringRule:
  """Build a lowering rule for an foreign function interface (FFI) target.

  By default, this lowering rule can use the input and output abstract values to
  compute the input and output types and shapes for the custom call, assuming
  row-major layouts.

  Note that layouts passed to this function as tuples should be in
  minor-to-major order (as expected by XLA) rather than major-to-minor as used
  by :func:`~jax.ffi.ffi_call` and ``Layout``.

  If keyword arguments are passed to the lowering rule, these are treated as
  attributes, and added to `backend_config`.

  Args:
    call_target_name: The name of the custom call target.
    operand_layouts: A sequence of layouts (dimension orders) for each operand.
      By default, the operands are assumed to be row-major.
    result_layouts: A sequence of layouts (dimension orders) for each result.
      By default, the results are assumed to be row-major.
    backend_config: Configuration data for the custom call. Any keyword
      arguments passed to the lowering rule will added to this dictionary.
    lowering_args: Any other arguments to :func:`mlir.custom_call` will also be
      passed through if provided as extra arguments to this function.
    skip_ffi_layout_processing: If true, skip processing of operand and result
      layout arguments passed to the lowering rule.
  """

  def _lowering(
    ctx: mlir.LoweringRuleContext, *operands: ir.Value, **params: Any
  ) -> Sequence[ir.Value | Sequence[ir.Value]]:
    result = build_ffi_lowering_function(
        call_target_name,
        operand_layouts=operand_layouts,
        result_layouts=result_layouts,
        backend_config=backend_config,
        skip_ffi_layout_processing=skip_ffi_layout_processing,
        **lowering_args,
    )(ctx, *operands, **params)

    return result.results  # type: ignore

  return _lowering


ResultMetadata = DuckTypedArray | core.AbstractToken


def _result_avals(results: Sequence[ResultMetadata]) -> tuple[core.AbstractValue, ...]:
  avals: list[core.AbstractValue] = []
  for idx, result in enumerate(results):
    if isinstance(result, core.AbstractToken):
      avals.append(result)
    else:
      if not hasattr(result, "shape") or not hasattr(result, "dtype"):
        raise ValueError(
            "All elements of result_shape_dtypes must have 'shape' and 'dtype' "
            f"attributes. Got {result} at position {idx}.")
      avals.append(core.ShapedArray(result.shape, result.dtype))
  return tuple(avals)


def _check_compatible_avals(a: core.AbstractValue, b: core.AbstractValue) -> bool:
  if isinstance(a, core.AbstractToken) and isinstance(b, core.AbstractToken):
    return True
  if getattr(a, "shape", ()) != getattr(b, "shape", ()):
    return False
  if getattr(a, "dtype", ()) != getattr(b, "dtype", ()):
    return False
  return True


def _convert_layouts_for_ffi_call(
    avals: Sequence[core.AbstractValue],
    layouts: Sequence[FfiLayoutOptions]) -> tuple[Sequence[int], ...]:
  return tuple(
      _convert_layout_for_lowering(
          aval,
          layout if layout is None or isinstance(layout, Layout)
          else layout[::-1]
      )
      for aval, layout in zip(avals, layouts))


# ffi_call() returns as many results as result_shape_dtypes.
@overload
def ffi_call(
    target_name: str,
    result_shape_dtypes: ResultMetadata,
    *,
    has_side_effect: bool = ...,
    vmap_method: str | None = ...,
    input_layouts: Sequence[FfiLayoutOptions] | None = ...,
    output_layouts: FfiLayoutOptions | Sequence[FfiLayoutOptions] | None = ...,
    input_output_aliases: dict[int, int] | None = ...,
    custom_call_api_version: int = ...,
    legacy_backend_config: str | None = ...,
    vectorized: bool | None | DeprecatedArg = DeprecatedArg(),
) -> Callable[..., Array]:
  ...


@overload
def ffi_call(
    target_name: str,
    result_shape_dtypes: Sequence[ResultMetadata],
    *,
    has_side_effect: bool = ...,
    vmap_method: str | None = ...,
    input_layouts: Sequence[FfiLayoutOptions] | None = ...,
    output_layouts: FfiLayoutOptions | Sequence[FfiLayoutOptions] | None = ...,
    input_output_aliases: dict[int, int] | None = ...,
    custom_call_api_version: int = ...,
    legacy_backend_config: str | None = ...,
    vectorized: bool | None | DeprecatedArg = DeprecatedArg(),
) -> Callable[..., Sequence[Array]]:
  ...


def ffi_call(
    target_name: str,
    result_shape_dtypes: ResultMetadata | Sequence[ResultMetadata],
    *,
    has_side_effect: bool = False,
    vmap_method: str | None = None,
    input_layouts: Sequence[FfiLayoutOptions] | None = None,
    output_layouts: FfiLayoutOptions | Sequence[FfiLayoutOptions] | None = None,
    input_output_aliases: dict[int, int] | None = None,
    custom_call_api_version: int = 4,
    legacy_backend_config: str | None = None,
    vectorized: bool | None | DeprecatedArg = DeprecatedArg(),
) -> Callable[..., Array | Sequence[Array]]:
  """Call a foreign function interface (FFI) target.

  See the :ref:`ffi-tutorial` tutorial for more information.

  Like :func:`~jax.pure_callback`, the behavior of ``ffi_call`` under
  :func:`~jax.vmap` depends on the value of ``vmap_method``. See the
  :func:`~jax.pure_callback` documentation for more details about the allowed
  values and examples of their behavior.

  The current default behavior is to use ``vmap_method="sequential"`` when
  not specified, but this behavior is deprecated, and in the future, the
  default will be to raise a ``NotImplementedError`` unless ``vmap_method`` is
  explicitly specified.

  Args:
    target_name: the name of the XLA FFI custom call target that was registered
      using :func:`~jax.ffi.register_ffi_target`.
    result_shape_dtypes: an object, or sequence of objects, with ``shape`` and
      ``dtype`` attributes which are expected to match the shape and dtype of
      the custom call output or outputs. :class:`~jax.ShapeDtypeStruct` is often
      used to define the elements of ``result_shape_dtypes``.
      ``jax.core.abstract_token`` may be used to represent a token-typed output.
    has_side_effect: boolean specifying whether the custom call has side
      effects. When ``True``, the FFI call will be executed even when the
      outputs are not used.
    vmap_method: string specifying how the FFI call transforms under
      :func:`~jax.vmap` as described above.
    input_layouts: a sequence of layouts for each input argument. In each case,
      the layout can be (a) ``None`` indicating that this input is in default
      row-major order, (b) a ``Layout`` specifying the axis order,
      or (c) a sequence of integers specifying the major-to-minor axis
      ordering. Users who are familiar with XLA layouts should note that this
      function expects layouts in major-to-minor order instead of the
      minor-to-major order that XLA uses. For example, a batch of row-major
      matrices could be specified using the layout ``[0, 1, 2]``, whereas a
      batch of column-major matrices would have layout ``[0, 2, 1]``. In both
      of these examples, the leading/batch dimension is the "slowest" axis. The
      ``input_layouts`` parameter should be used to request the memory layout
      expected by the FFI call target, and XLA will ensure that the buffers
      have the correct layouts before the handler is executed.
    output_layouts: like ``input_layouts``, but specifying the required layouts
      for the output arrays.
    input_output_aliases: a dictionary where the keys are input indices and the
      values are output indices. This mapping indicates which output arrays
      alias specific input arrays.
    custom_call_api_version: the version number of the custom call API
      implemented by the FFI target ``target_name``. The only formally
      supported version is the typed FFI API with ``custom_call_api_version=4``,
      but earlier unsupported custom calls can be executed using this argument.
    legacy_backend_config: for legacy targets implemented using
      ``custom_call_api_version<4``, attributes are passed using the opaque
      string representation provided by this argument. This parameter cannot be
      used with ``custom_call_api_version>=4``.

  Returns:
    A function that can be called with the input arrays as positional arguments
    to execute the FFI handler. Any keyword arguments are passed as named
    attributes to the FFI handler using XLA's FFI interface.
  """
  # TODO(danfm): Remove this check 3 months after v0.6.0 is released.
  if not isinstance(vectorized, DeprecatedArg):
    raise ValueError(
        "The 'vectorized' argument of jax.ffi.ffi_call was removed in JAX "
        "v0.6.0. Use 'vmap_method' instead.")
  allowed_vmap_methods = ["sequential", "sequential_unrolled", "expand_dims",
                          "broadcast_all", "legacy_vectorized", None]
  if vmap_method not in allowed_vmap_methods:
    raise ValueError(
        f"vmap_method must be on of the allowed methods {allowed_vmap_methods}, "
        f"but got: {vmap_method}")

  output_layouts_: Sequence[FfiLayoutOptions] | None
  if isinstance(result_shape_dtypes, Sequence):
    output_layouts_ = output_layouts  # type: ignore
    multiple_results = True
    result_avals = _result_avals(result_shape_dtypes)
  else:
    multiple_results = False
    result_avals = _result_avals((result_shape_dtypes,))
    output_layouts_ = (output_layouts,)  # type: ignore

  if custom_call_api_version >= 4 and legacy_backend_config is not None:
    raise ValueError(
        "The use of the legacy_backend_config parameter requires "
        f"custom_call_api_version < 4; got {custom_call_api_version}.")

  def wrapped(*args: ArrayLike, **kwargs: Any):
    in_avals = [core.get_aval(x) for x in args]

    if input_layouts is None:
      static_input_layouts = tuple(map(_convert_layout_for_lowering, in_avals))
    else:
      if len(input_layouts) != len(in_avals):
        raise ValueError(
            f"The number of input arguments ({len(in_avals)}) must equal the "
            f"number of input layouts ({len(input_layouts)}).")
      static_input_layouts = _convert_layouts_for_ffi_call(in_avals,
                                                           input_layouts)
    if output_layouts_ is None:
      static_output_layouts = tuple(map(_convert_layout_for_lowering,
                                        result_avals))
    else:
      if len(output_layouts_) != len(result_avals):
        raise ValueError(
            f"The number of outputs ({len(result_avals)}) must equal the "
            f"number of output layouts ({len(output_layouts_)}).")
      static_output_layouts = _convert_layouts_for_ffi_call(result_avals,
                                                            output_layouts_)

    static_input_output_aliases: tuple[tuple[int, int], ...] = ()
    if input_output_aliases is not None:
      for i_idx, o_idx in sorted(input_output_aliases.items()):
        i_idx, o_idx = int(i_idx), int(o_idx)
        if i_idx >= len(args):
          raise ValueError(
              f"input_output_aliases contains the mapping '{i_idx}:{o_idx}' "
              f"with input index {i_idx} outside the range [0, "
              f"{len(args)}).")
        if o_idx >= len(result_avals):
          raise ValueError(
              f"input_output_aliases contains the mapping '{i_idx}:{o_idx}' "
              f"with output index {o_idx} outside the range [0, "
              f"{len(result_avals)}).")
        in_aval = in_avals[i_idx]
        out_aval = result_avals[o_idx]
        if not _check_compatible_avals(in_aval, out_aval):
          raise ValueError(
              f"input_output_aliases contains the mapping '{i_idx}:{o_idx}' "
              f"referring to an input with abstract value {in_aval} and an "
              f"output with a different abstract value {out_aval}.")
        if static_input_layouts[i_idx] != static_output_layouts[o_idx]:
          raise ValueError(
              f"input_output_aliases contains the mapping '{i_idx}:{o_idx}' "
              f"referring to an input with layout {static_input_layouts[i_idx]} "
              "and an output with a different layout "
              f"{static_output_layouts[o_idx]}.")
        static_input_output_aliases += ((i_idx, o_idx),)
    args = core.standard_insert_pvary(*args)
    results = ffi_call_p.bind(
        *args,
        result_avals=result_avals,
        vmap_method=vmap_method,
        target_name=target_name,
        has_side_effect=has_side_effect,
        input_layouts=static_input_layouts,
        output_layouts=static_output_layouts,
        input_output_aliases=static_input_output_aliases,
        custom_call_api_version=custom_call_api_version,
        legacy_backend_config=legacy_backend_config,
        attributes=_wrap_kwargs_hashable(kwargs),
    )
    if multiple_results:
      if isinstance(result_shape_dtypes, tuple):
        return tuple(results)
      return results
    else:
      return results[0]

  return wrapped


# ffi_call must support some small non-hashable input arguments, like np.arrays
# and dicts, to support calling FFI targets with array inputs or user defined
# structs. Since these arguments will eventually be embedded in the HLO as
# dense attributes, we assume that they are small and hash by making an
# immutable copy and hashing by value.
def _wrap_kwargs_hashable(kwargs: dict[str, Any]) -> Sequence[tuple[str, Any]]:
  hashable_kwargs: list[tuple[str, Any]] = []
  for k, v in sorted(kwargs.items()):
    if isinstance(v, np.ndarray):
      hashable_kwargs.append((k, HashableArray(v)))
    elif isinstance(v, dict):
      hashable_kwargs.append((k, HashableDict(v)))
    else:
      try:
        hash(v)
      except TypeError as e:
        raise TypeError(
            f"Non-hashable keyword argument to ffi_call {k}: {v}") from e
      else:
        hashable_kwargs.append((k, v))
  return tuple(hashable_kwargs)


def _unwrap_kwargs_hashable(kwargs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
  unwrapped_kwargs: dict[str, Any] = {}
  for k, v in kwargs:
    if isinstance(v, HashableArray):
      unwrapped_kwargs[k] = v.val
    elif isinstance(v, HashableDict):
      unwrapped_kwargs[k] = dict(v.val)
    else:
      unwrapped_kwargs[k] = v
  return unwrapped_kwargs


class HashableDict:
  __slots__ = ["val"]

  def __init__(self, val):
    assert isinstance(val, dict)
    self.val = tuple(sorted(val.items()))

  def __repr__(self):
    return f"HashableDict({dict(self.val)})"

  def __hash__(self):
    return hash(self.val)

  def __eq__(self, other):
    return isinstance(other, HashableDict) and self.val == other.val


@dataclasses.dataclass(frozen=True)
class FfiEffect(effects.Effect):
  def __str__(self):
    return "FFI"


_FfiEffect = FfiEffect()
effects.lowerable_effects.add_type(FfiEffect)
effects.control_flow_allowed_effects.add_type(FfiEffect)


def ffi_call_abstract_eval(
    *avals_in,
    result_avals: tuple[core.AbstractValue, ...],
    has_side_effect: bool,
    **_,
):
  out_vma = core.standard_vma_rule('ffi_call', *avals_in)
  effects = {_FfiEffect} if has_side_effect else core.no_effects
  return tuple(r if r is core.abstract_token else r.update(vma=out_vma)
               for r in result_avals), effects


def ffi_call_jvp(*args, target_name, **_):
  del args
  raise ValueError(
      f"The FFI call to `{target_name}` cannot be differentiated. "
      "You can use `jax.custom_jvp` or `jax.custom_jvp` to add support.")


def ffi_call_transpose(*args, target_name, **_):
  del args
  raise ValueError(
      f"The FFI call to `{target_name}` cannot be differentiated. "
      "You can use `jax.custom_jvp` or `jax.custom_jvp` to add support.")


def ffi_call_lowering(
    ctx: mlir.LoweringRuleContext,
    *operands: ir.Value,
    target_name: str,
    has_side_effect: bool,
    input_layouts: Sequence[Sequence[int]],
    output_layouts: Sequence[Sequence[int]],
    input_output_aliases: Sequence[tuple[int, int]],
    custom_call_api_version: int,
    legacy_backend_config: str | None,
    attributes: Sequence[tuple[str, Any]],
    **_,
) -> Sequence[ir.Value]:
  rule = ffi_lowering(target_name, has_side_effect=has_side_effect,
                      operand_layouts=input_layouts,
                      result_layouts=output_layouts,
                      operand_output_aliases=dict(input_output_aliases),
                      api_version=custom_call_api_version,
                      backend_config=legacy_backend_config)
  return rule(ctx, *operands, **_unwrap_kwargs_hashable(attributes))


def ffi_batching_rule(
    prim,
    args,
    dims,
    *,
    vmap_method: str | None,
    result_avals: Sequence[core.ShapedArray],
    **kwargs: Any,
):
  from jax._src.lax import control_flow  # pytype: disable=import-error
  from jax._src.lax import lax  # pytype: disable=import-error

  axis_size, = {a.shape[d] for a, d in zip(args, dims)
                if d is not batching.not_mapped}
  new_args = [arg if dim is batching.not_mapped else
              batching.moveaxis(arg, dim, 0) for arg, dim in zip(args, dims)]
  batched_result_avals = tuple(
      core.unmapped_aval(axis_size, 0, aval) for aval in result_avals)

  # For FFI calls we must update the layouts. We handle the output layouts
  # here, but the input layout updates depend on the vmap_method parameter.
  if (
      vmap_method not in ("sequential", "sequential_unrolled") and
      kwargs.get("output_layouts") is not None
  ):
    kwargs["output_layouts"] = tuple(
        None if layout is None else tuple(n + 1 for n in layout) + (0,)
        for layout in kwargs["output_layouts"])

  if vmap_method == "legacy_vectorized":
    # This method is kept to support the behavior that was previously exposed
    # when using `vectorized=True`.
    if kwargs.get("input_layouts") is not None:
      kwargs["input_layouts"] = tuple(
          layout if d is batching.not_mapped else
          (None if layout is None else tuple(n + 1 for n in layout) + (0,))
          for layout, d in zip(kwargs["input_layouts"], dims))
    outvals = prim.bind(
        *new_args,
        vmap_method=vmap_method,
        result_avals=batched_result_avals,
        **kwargs,
    )
  elif vmap_method == "expand_dims" or vmap_method == "broadcast_all":
    size = axis_size if vmap_method == "broadcast_all" else 1
    bcast_args = [
        lax.broadcast(x, (size,)) if d is batching.not_mapped else x
        for x, d in zip(new_args, dims)]
    if kwargs.get("input_layouts") is not None:
      kwargs["input_layouts"] = tuple(
          None if layout is None else tuple(n + 1 for n in layout) + (0,)
          for layout in kwargs["input_layouts"])
    outvals = prim.bind(
      *bcast_args,
      vmap_method=vmap_method,
      result_avals=batched_result_avals,
      **kwargs,
    )
  elif vmap_method == "sequential" or vmap_method == "sequential_unrolled":
    is_batched = [d is not batching.not_mapped for d in dims]
    unbatched_args, batched_args = util.partition_list(is_batched, new_args)
    def _batch_fun(batched_args):
      merged_args = util.merge_lists(is_batched, unbatched_args, batched_args)
      return prim.bind(
          *merged_args,
          result_avals=result_avals,
          vmap_method=vmap_method,
          **kwargs,
      )
    unroll = vmap_method == "sequential_unrolled"
    g = lambda _, x: ((), _batch_fun(x))
    _, outvals = control_flow.scan(g, (), batched_args, unroll=unroll)
  else:
    raise NotImplementedError(
        f"vmap is only supported for the {prim.name} primitive when vmap_method "
        "is one of 'sequential', 'sequential_unrolled', 'expand_dims', "
        f"'broadcast_all', or 'legacy_vectorized'. Got {vmap_method=}.")
  return tuple(outvals), (0,) * len(outvals)


ffi_call_p = core.Primitive("ffi_call")
ffi_call_p.multiple_results = True
dispatch.simple_impl(ffi_call_p)
ffi_call_p.def_effectful_abstract_eval(ffi_call_abstract_eval)
ad.primitive_jvps[ffi_call_p] = ffi_call_jvp
ad.primitive_transposes[ffi_call_p] = ffi_call_transpose
batching.primitive_batchers[ffi_call_p] = functools.partial(
    ffi_batching_rule, ffi_call_p)
mlir.register_lowering(ffi_call_p, ffi_call_lowering)
