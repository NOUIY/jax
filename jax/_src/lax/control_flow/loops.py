# Copyright 2022 The JAX Authors.
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
"""Module for the loop primitives."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from functools import partial
import inspect
import itertools as it
import operator
from typing import Any, TypeVar
import weakref

from jax._src import ad_checkpoint
from jax._src import ad_util
from jax._src import api
from jax._src import api_util
from jax._src import config
from jax._src import core
from jax._src import dispatch
from jax._src import dtypes
from jax._src import effects
from jax._src import linear_util as lu
from jax._src import source_info_util
from jax._src import state
from jax._src import util
from jax._src.api_util import (
    _check_no_aliased_ref_args, _check_no_aliased_closed_over_refs)
from jax._src.core import ShapedArray, typeof, ClosedJaxpr
from jax._src.interpreters import ad
from jax._src.interpreters import batching
from jax._src.interpreters import mlir
from jax._src.interpreters import partial_eval as pe
from jax._src.interpreters import pxla
from jax._src import sharding_impls as sharding
from jax._src.interpreters import xla
from jax._src.lax import lax
from jax._src.lax import slicing
from jax._src.lax import windowed_reductions
from jax._src.lax.control_flow.common import (
    _avals_short, _initial_style_jaxpr, _prune_zeros, _typecheck_param,
    _make_closed_jaxpr)
from jax._src.lax.other import logaddexp
from jax._src.pjit import auto_axes, PartitionSpec as P
from jax._src.lib.mlir import ir
from jax._src.lib.mlir.dialects import hlo
from jax._src.sharding_impls import canonicalize_sharding
from jax._src.state import discharge as state_discharge, AbstractRef
from jax._src.traceback_util import api_boundary
from jax._src.tree_util import equality_errors
from jax._src.typing import Array
from jax._src.util import (
    merge_lists, partition_list, safe_map, safe_zip, split_list,
    split_list_checked, unzip2, weakref_lru_cache, subs_list)
from jax._src import xla_bridge as xb
from jax._src.tree_util import (
    keystr, tree_flatten, tree_flatten_with_path, tree_map, tree_unflatten,
    treedef_is_leaf)
import numpy as np

_map = safe_map
zip = safe_zip

T = TypeVar('T')
BooleanNumeric = Any  # A bool, or a Boolean array.

### Helper functions

def _stack(arrs: Sequence[Array], axis: int=0) -> Array:
  return lax.concatenate([lax.expand_dims(arr, (axis,)) for arr in arrs], dimension=axis)

def _promote_weak_typed_inputs(in_vals, in_avals, out_avals):
  """Promote weakly-typed in_vals to be compatible with out_avals.

  Args:
    in_vals : flattened list of input values.
    in_avals : corresponding list of avals.
    out_avals : list of target output avals.
  Returns:
    in_vals_new : flattened list of modified in_vals with no weak types.
    changed : bool; true if in_vals required modification.
  """
  if len(in_vals) != len(in_avals) or len(in_avals) != len(out_avals):
    # Calling function is responsible for catching this.
    return in_vals, False
  weak_mismatches = [i for i, (a1, a2) in enumerate(zip(in_avals, out_avals))
                    if getattr(a1, 'weak_type', False) and not core.typematch(a1, a2)]
  if not weak_mismatches:
    return in_vals, False
  for i in weak_mismatches:
    new_dtype = dtypes.result_type(in_vals[i], out_avals[i])
    in_vals[i] = lax.convert_element_type(in_vals[i], new_dtype)
  return in_vals, True


### scan

Carry = TypeVar('Carry')
X = TypeVar('X')
Y = TypeVar('Y')

@api_boundary
def scan(f: Callable[[Carry, X], tuple[Carry, Y]],
         init: Carry,
         xs: X | None = None,
         length: int | None = None,
         reverse: bool = False,
         unroll: int | bool = 1,
         _split_transpose: bool = False) -> tuple[Carry, Y]:
  """Scan a function over leading array axes while carrying along state.

  The `Haskell-like type signature`_ in brief is

  .. code-block:: haskell

    scan :: (c -> a -> (c, b)) -> c -> [a] -> (c, [b])

  where for any array type specifier ``t``, ``[t]`` represents the type with an additional
  leading axis, and if ``t`` is a pytree (container) type with array leaves then ``[t]``
  represents the type with the same pytree structure and corresponding leaves
  each with an additional leading axis.

  When the type of ``xs`` (denoted `a` above) is an array type or None, and the type
  of ``ys`` (denoted `b` above) is an array type, the semantics of :func:`~scan` are
  given roughly by this Python implementation::

    def scan(f, init, xs, length=None):
      if xs is None:
        xs = [None] * length
      carry = init
      ys = []
      for x in xs:
        carry, y = f(carry, x)
        ys.append(y)
      return carry, np.stack(ys)

  Unlike that Python version, both ``xs`` and ``ys`` may be arbitrary pytree
  values, and so multiple arrays can be scanned over at once and produce multiple
  output arrays. ``None`` is actually a special case of this, as it represents an
  empty pytree.

  Also unlike that Python version, :func:`~scan` is a JAX primitive and is
  lowered to a single WhileOp. That makes it useful for reducing
  compilation times for JIT-compiled functions, since native Python
  loop constructs in an :func:`~jax.jit` function are unrolled, leading to large
  XLA computations.

  Finally, the loop-carried value ``carry`` must hold a fixed shape and dtype
  across all iterations (and not just be consistent up to NumPy rank/shape
  broadcasting and dtype promotion rules, for example). In other words, the type
  ``c`` in the type signature above represents an array with a fixed shape and
  dtype (or a nested tuple/list/dict container data structure with a fixed
  structure and arrays with fixed shape and dtype at the leaves).

  .. note::
    :py:func:`scan` compiles ``f``, so while it can be combined with
    :py:func:`jit`, it's usually unnecessary.

  .. note::
    :func:`scan` is designed for iterating with a static number of iterations.
    For iteration with a dynamic number of iterations, use :func:`fori_loop`
    or :func:`while_loop`.

  Args:
    f: a Python function to be scanned of type ``c -> a -> (c, b)``, meaning
      that ``f`` accepts two arguments where the first is a value of the loop
      carry and the second is a slice of ``xs`` along its leading axis, and that
      ``f`` returns a pair where the first element represents a new value for
      the loop carry and the second represents a slice of the output.
    init: an initial loop carry value of type ``c``, which can be a scalar,
      array, or any pytree (nested Python tuple/list/dict) thereof, representing
      the initial loop carry value. This value must have the same structure as
      the first element of the pair returned by ``f``.
    xs: the value of type ``[a]`` over which to scan along the leading axis,
      where ``[a]`` can be an array or any pytree (nested Python
      tuple/list/dict) thereof with consistent leading axis sizes.
    length: optional integer specifying the number of loop iterations, which
      must agree with the sizes of leading axes of the arrays in ``xs`` (but can
      be used to perform scans where no input ``xs`` are needed).
    reverse: optional boolean specifying whether to run the scan iteration
      forward (the default) or in reverse, equivalent to reversing the leading
      axes of the arrays in both ``xs`` and in ``ys``.
    unroll: optional positive int or bool specifying, in the underlying
      operation of the scan primitive, how many scan iterations to unroll within
      a single iteration of a loop. If an integer is provided, it determines how
      many unrolled loop iterations to run within a single rolled iteration of
      the loop. If a boolean is provided, it will determine if the loop is
      completely unrolled (i.e. `unroll=True`) or left completely rolled (i.e.
      `unroll=False`).
    _split_transpose: experimental optional bool specifying whether to further
      split the transpose into a scan (computing activation gradients), and a
      map (computing gradients corresponding to the array arguments). Enabling
      this may increase memory requirements, and so is an experimental feature
      that may evolve or even be rolled back.

  Returns:
    A pair of type ``(c, [b])`` where the first element represents the final
    loop carry value and the second element represents the stacked outputs of
    the second output of ``f`` when scanned over the leading axis of the inputs.

  .. _Haskell-like type signature: https://wiki.haskell.org/Type_signature
  """
  if not callable(f):
    raise TypeError("lax.scan: f argument should be a callable.")
  xs_flat, xs_tree = tree_flatten(xs)

  try:
    lengths = [x.shape[0] for x in xs_flat]
  except AttributeError as err:
    msg = "scan got value with no leading axis to scan over: {}."
    raise ValueError(
      msg.format(', '.join(str(x) for x in xs_flat
                           if not hasattr(x, 'shape')))) from err

  xs_avals = [core.get_aval(x) for x in xs_flat]

  if not all(a.sharding.spec[0] is None for a in xs_avals):
    raise ValueError('0th dimension of all xs should be replicated. Got '
                     f'{", ".join(str(a.sharding.spec) for a in xs_avals)}')

  if length is not None:
    try:
      length = int(length)
    except core.ConcretizationTypeError as err:
      msg = ('The `length` argument to `scan` expects a concrete `int` value.'
             ' For scan-like iteration with a dynamic length, use `while_loop`'
             ' or `fori_loop`.')
      raise core.ConcretizationTypeError(length, msg) from None  # type: ignore[arg-type]
    if not all(length == l for l in lengths):
      msg = ("scan got `length` argument of {} which disagrees with "
             "leading axis sizes {}.")
      raise ValueError(msg.format(length, [x.shape[0] for x in xs_flat]))
  else:
    unique_lengths = set(lengths)
    if len(unique_lengths) > 1:
      msg = "scan got values with different leading axis sizes: {}."
      raise ValueError(msg.format(', '.join(str(x.shape[0]) for x in xs_flat)))
    elif len(unique_lengths) == 0:
      msg = "scan got no values to scan over and `length` not provided."
      raise ValueError(msg)
    else:
      length, = unique_lengths

  if config.disable_jit.value:
    if length == 0:
      raise ValueError("zero-length scan is not supported in disable_jit() "
                       "mode because the output type is unknown.")
    carry = init
    ys = []
    maybe_reversed = reversed if reverse else lambda x: x
    for i in maybe_reversed(range(length)):
      xs_slice = [slicing.index_in_dim(x, i, keepdims=False) for x in xs_flat]
      carry, y = f(carry, tree_unflatten(xs_tree, xs_slice))
      ys.append(y)
    stack = lambda *ys: _stack(ys)
    stacked_y = tree_map(stack, *maybe_reversed(ys))
    return carry, stacked_y

  x_avals = [core.mapped_aval(length, 0, aval) for aval in xs_avals]
  dbg_body = api_util.debug_info("scan", f, (init, xs), {})

  if config.mutable_array_checks.value:
    in_flat, in_tree = tree_flatten((init, xs))
    in_avals = tuple(_map(core.get_aval, in_flat))
    _check_no_aliased_ref_args(dbg_body, in_avals, in_flat)

  def _create_jaxpr(init):
    init_flat, init_tree = tree_flatten(init)
    in_flat, in_tree = tree_flatten((init, xs))
    carry_avals = tuple(_map(core.get_aval, init_flat))
    jaxpr, consts, out_tree = _initial_style_jaxpr(
        f, in_tree, (*carry_avals, *x_avals), debug_info=dbg_body)
    if config.mutable_array_checks.value:
      _check_no_aliased_closed_over_refs(dbg_body, (*jaxpr.consts, *consts), in_flat)
    out_tree_children = out_tree.children()
    if len(out_tree_children) != 2:
      msg = "scan body output must be a pair, got {}."
      raise TypeError(msg.format(tree_unflatten(out_tree, jaxpr.out_avals)))

    carry_avals_out, _ = split_list(jaxpr.out_avals, [out_tree_children[0].num_leaves])
    return (init_flat, carry_avals, carry_avals_out, init_tree, in_flat, jaxpr,
            consts, out_tree, out_tree_children)

  # The carry input and output avals must match exactly. However, we want to account for
  # the case when init contains weakly-typed values (e.g. Python scalars), with avals that
  # may not match the output despite being compatible by virtue of their weak type.
  # To do this, we compute the jaxpr in two passes: first with the raw inputs, and if
  # necessary, a second time with modified init values.
  init_flat, carry_avals, carry_avals_out, init_tree, *rest = _create_jaxpr(init)
  new_init_flat, changed = _promote_weak_typed_inputs(init_flat, carry_avals, carry_avals_out)
  if changed:
    init = tree_unflatten(init_tree, new_init_flat)
    init_flat, carry_avals, carry_avals_out, init_tree, *rest = _create_jaxpr(init)
  in_flat, jaxpr, consts, out_tree, out_tree_children = rest
  num_carry = len(init_flat)
  num_xs = len(x_avals)
  num_ys = len(jaxpr.out_avals) - num_carry
  del init_flat

  _check_carry_type('scan body', f, init, out_tree_children[0], carry_avals_out)
  disallowed_effects = effects.control_flow_allowed_effects.filter_not_in(jaxpr.effects)
  if disallowed_effects:
    raise NotImplementedError(
        f'Effects not supported in `scan`: {disallowed_effects}')

  unroll = core.concrete_or_error(
      None, unroll,
      "The `unroll` argument to `scan` expects a concrete `int` or `bool` "
      "value.")
  if isinstance(unroll, bool):
    unroll = max(length, 1) if unroll else 1
  if unroll < 1:
    raise ValueError("`unroll` must be a `bool` or a positive `int`.")

  # If the body forwards an input carry to an output carry, that input is
  # read-only and can be moved to be a const. Doing so can lead to efficiency
  # wins, e.g. if the scan is inside a cond with a batched predicate.
  carry_fwd, ext_fwd = split_list(pe._jaxpr_forwarding(jaxpr.jaxpr), [num_carry])
  move_to_const = [len(consts) + i == f for i, f in enumerate(carry_fwd)]
  if any(move_to_const):
    jaxpr = pe.prune_closed_jaxpr_outputs(
        jaxpr, [not m for m in move_to_const] + [True] * num_ys)
    jaxpr = pe.move_binders_to_front(
        jaxpr, [False] * len(consts) + move_to_const + [False] * num_xs)
    in_flat, new_consts = partition_list(move_to_const + [False] * num_xs, in_flat)
    consts = [*new_consts, *consts]
    num_carry -= len(new_consts)

  # When an extensive output is forwarded from an extensive input, we can
  # avoid copying it by pruning it from the jaxpr and forwarding manually. We
  # don't need to update the indexing based on the optimization above since it
  # doesn't change the total number of consts and carries combined, and
  # `ext_fwd` already only includes the extensive outputs. But, we do remove
  # the number of consts from the index since we're going to use it to index
  # into `in_flat`, which doesn't include consts.
  ext_to_ext_fwd = [
      in_idx - len(consts) if in_idx is not None and
      in_idx >= num_carry + len(consts) else None for in_idx in ext_fwd]
  jaxpr = pe.prune_closed_jaxpr_outputs(
      jaxpr, [True] * num_carry + [i is None for i in ext_to_ext_fwd])

  out = scan_p.bind(*consts, *in_flat,
                    reverse=reverse, length=length, jaxpr=jaxpr,
                    num_consts=len(consts), num_carry=num_carry,
                    linear=(False,) * (len(consts) + len(in_flat)),
                    unroll=unroll, _split_transpose=_split_transpose)

  # Apply input to output forwarding that was computed above.
  carry_out, out = split_list(out, [num_carry])
  out_ = iter(out)
  out = [next(out_) if f is None else _maybe_put(in_flat[f]) for f in ext_to_ext_fwd]
  assert next(out_, None) is None
  out = [*carry_out, *out]

  if any(move_to_const):
    out = pe.merge_lists(move_to_const + [False] * num_ys, out, new_consts)

  return tree_unflatten(out_tree, out)


def _capitalize(s):
  # s.capitalize() converts s[1:] to lowercase which we don't want.
  return s[0].capitalize() + s[1:]

def _check_carry_type(name, body_fun, in_carry, out_carry_tree, out_avals):
  try:
    sig = inspect.signature(body_fun)
  except (ValueError, TypeError):
    sig = None
  carry_name = sig and list(sig.parameters)[0]
  if carry_name:
    component = lambda p: (f'the input carry component {carry_name}{keystr(p)}'
                           if p else f'the input carry {carry_name}')
  else:
    component = lambda p: (f'the input carry at path {keystr(p)}'
                           if p else 'the input carry')
  leaves_and_paths, in_carry_tree = tree_flatten_with_path(in_carry)
  paths, in_carry_flat = unzip2(leaves_and_paths)
  in_avals = _map(core.get_aval, in_carry_flat)
  if in_carry_tree != out_carry_tree:
    try:
      out_carry = tree_unflatten(out_carry_tree, out_avals)
    except:
      out_carry = None

    if out_carry is None:
      differences = [f'the input tree structure is:\n{in_carry_tree}\n',
                     f'the output tree structure is:\n{out_carry_tree}\n']
    else:
      diffs = [f'{component(path)} is a {thing1} but the corresponding component '
               f'of the carry output is a {thing2}, so {explanation}'
               for path, thing1, thing2, explanation
               in equality_errors(in_carry, out_carry)]
      if len(diffs) == 0:
        return  # the trees may have different aux data, but structures are same
      elif len(diffs) == 1:
        differences = f'{_capitalize(diffs[0])}.\n'
      else:
        differences = ('\n'.join(f'  * {d};\n' for d in diffs[:-1])
                       + f'  * {diffs[-1]}.\n')
    raise TypeError(
        f"{name} function carry input and carry output must have the same "
        "pytree structure, but they differ:\n\n"
        f"{differences}\n"
        "Revise the function so that the carry output has the same pytree "
        "structure as the carry input.")
  if not all(_map(core.typematch, in_avals, out_avals)):
    diffs = [f'{component(path)} has type {in_aval.str_short()}'
             ' but the corresponding output carry component has type '
             f'{out_aval.str_short()}{core.aval_mismatch_extra(in_aval, out_aval)}'
             for path, in_aval, out_aval in zip(paths, in_avals, out_avals)
             if not core.typematch(in_aval, out_aval)]

    if len(diffs) == 0:
      return  # seems unreachable but in any case we don't have a good error msg
    if len(diffs) == 1:
      differences = f'{_capitalize(diffs[0])}.\n'
    else:
      differences = ('\n'.join(f'  * {d};\n' for d in diffs[:-1])
                     + f'  * {diffs[-1]}.\n')

    pvary_applications = [
        f'applying `jax.lax.pvary(..., {tuple(out_aval.vma - in_aval.vma)})` '
        f'to the initial carry value corresponding to {component(path)}'
        for path, in_aval, out_aval in zip(paths, in_avals, out_avals)
        if not core.typematch(in_aval, out_aval) and
        isinstance(in_aval, ShapedArray) and isinstance(out_aval, ShapedArray)
        and in_aval.vma != out_aval.vma and out_aval.vma - in_aval.vma]

    if not pvary_applications:
      pvary_msg = ''
    elif len(pvary_applications) == 1:
      pvary_msg = f'This might be fixed by {pvary_applications[0]}.\n'
    else:
      pvary_msg = ('This might be fixed by:\n' +
                   '\n'.join(f'  * {d};\n' for d in pvary_applications[:-1])
                   + f'  * {pvary_applications[-1]}.\n')
    if pvary_msg:
      pvary_msg += ("See https://docs.jax.dev/en/latest/notebooks/shard_map.html#scan-vma "
                    "for more information.\n\n")

    raise TypeError(
        f"{name} function carry input and carry output must have equal types, "
        "but they differ:\n\n"
        f"{differences}\n"
        f"{pvary_msg}"
        "Revise the function so that all output types match the corresponding "
        "input types.")

# TODO(mattjj): re-land #19819 version? simpler, but caused ~1 perf regression.
def _scan_impl(*args, reverse, length, num_consts, num_carry, jaxpr, linear,
               unroll, _split_transpose):
  del _split_transpose
  consts, carry, xs_ = split_list(args, [num_consts, num_carry])
  _, y_avals = split_list(jaxpr.out_avals, [num_carry])
  num_trips, remainder = divmod(length, unroll)

  if unroll != 1 and num_trips == 1 and remainder == 0:
    # In that case, we explicitly want to fully unroll the loop. Put everything
    # into the remainder block and avoid lowering to a while loop.
    num_trips, remainder = 0, length
  if unroll == 1:
    xss = xs_
    yss = _map(partial(_empty_array, (length,), (None,)), y_avals)
  else:
    if remainder:
      if not reverse:
        xs_, xs_rem = unzip2(_map(partial(_split_leading, num_trips*unroll), xs_))
      else:
        xs_rem, xs_ = unzip2(_map(partial(_split_leading, remainder), xs_))
    if num_trips:
      xss = [lax.reshape(x, (num_trips, unroll, *x.shape[1:])) for x in xs_]
      yss = _map(partial(_empty_array, (num_trips, unroll), (None, None)), y_avals)
    else:
      yss = _map(partial(_empty_array, (num_trips * unroll,), (None,)), y_avals)

  def inner(n, carry, xs):
    ys = []
    if unroll == 1:
      carry_y = eval_jaxpr_p.bind(*consts, *carry, *xs, jaxpr=jaxpr)
      return split_list(carry_y, [num_carry])
    for i_ in range(n):
      i = n - i_ - 1 if reverse else i_
      x = [slicing.index_in_dim(x, i, keepdims=False) for x in xs]
      carry_y = eval_jaxpr_p.bind(*consts, *carry, *x, jaxpr=jaxpr)
      carry, y = split_list(carry_y, [num_carry])
      ys.append(y)
    ys = list(reversed(ys)) if reverse else ys
    return carry, _map(_stack, zip(*ys))

  def body_fun(while_carry):
    i_, carry, yss = while_carry
    i = num_trips - i_ - 1 if reverse else i_
    xs = [slicing.dynamic_index_in_dim(xs, i, keepdims=False,
                                       allow_negative_indices=False)
          for xs in xss]
    carry, ys = inner(unroll, carry, xs)
    yss = [slicing.dynamic_update_index_in_dim(y, upd, i, 0,
                                               allow_negative_indices=False)
           for y, upd in zip(yss, ys)]
    return i_ + 1, carry, yss

  def cond_fun(while_carry):
    i, _, _ = while_carry
    return i < num_trips

  if num_trips:
    i = lax._const(num_trips, 0)
    _, carry, yss = while_loop(cond_fun, body_fun, (i, carry, yss))
  if unroll != 1 and num_trips != 0:
    ys = [lax.reshape(ys, (num_trips * unroll, *ys.shape[2:])) for ys in yss]
  else:
    ys = yss
  if remainder:
    carry, ys_rem = inner(remainder, carry, xs_rem)
    ys = _map(_concat, ys, ys_rem) if not reverse else _map(_concat, ys_rem, ys)
  return [*carry, *ys]

def _split_leading(sz, x):
  return (slicing.slice_in_dim(x, 0, sz),
          slicing.slice_in_dim(x, sz, x.shape[0]))

def _concat(a, b): return lax.concatenate([a, b], 0)

def _empty_array(prefix, length_spec, aval):
  sharding = aval.sharding.update(spec=(*length_spec, *aval.sharding.spec))
  empty = core.pvary(lax.empty(aval.dtype), tuple(aval.vma))
  return lax.broadcast(empty, (*prefix, *aval.shape), out_sharding=sharding)

eval_jaxpr_p = core.Primitive('eval_jaxpr')
eval_jaxpr_p.multiple_results = True
def _stage_jaxpr(trace: pe.DynamicJaxprTrace, source_info, *tracers,
                 jaxpr: ClosedJaxpr):
  params = dict(call_jaxpr=jaxpr)
  return trace.default_process_primitive(core.closed_call_p, tracers, params,
                                         source_info=source_info)
pe.custom_staging_rules[eval_jaxpr_p] = _stage_jaxpr

@eval_jaxpr_p.def_effectful_abstract_eval  # abstract eval only used for jax2tf
def _stage_jaxpr_abstract_eval(*_, jaxpr):
  return jaxpr.out_avals, jaxpr.effects

def _prepend_dim_to_aval(sz, aval):
  return core.unmapped_aval(sz, 0, aval)

def _scan_abstract_eval(*args, reverse, length, num_consts, num_carry, jaxpr,
                        linear, unroll, _split_transpose):
  if len(args) != len(jaxpr.in_avals):
    raise ValueError("scan number of arguments doesn't match the number "
                     "of jaxpr arguments: {len(args)} vs {len(jaxpr.in_avals)}")
  out_carry_avals, y_avals = split_list(jaxpr.out_avals, [num_carry])
  _, in_carry_avals, _ = split_list(args, [num_consts, num_carry])
  if [i.vma for i in in_carry_avals] != [o.vma for o in out_carry_avals]:
    raise ValueError(
        'Scan carry input and output got mismatched varying manual axes '
        f'{in_carry_avals} and {out_carry_avals}. Please open an '
        'issue at https://github.com/jax-ml/jax/issues, and as a '
        'temporary workaround pass the check_vma=False argument to '
        '`jax.shard_map`')
  ys_avals = _map(partial(_prepend_dim_to_aval, length), y_avals)
  return out_carry_avals + ys_avals, jaxpr.effects

def _scan_jvp(primals, tangents, reverse, length, jaxpr, num_consts, num_carry,
              linear, unroll, _split_transpose):
  num_xs = len(jaxpr.in_avals) - num_carry - num_consts
  num_ys = len(jaxpr.out_avals) - num_carry
  nonzeros = [type(t) is not ad_util.Zero for t in tangents]
  const_nz, init_nz, xs_nz = split_list(nonzeros, [num_consts, num_carry])

  # Fixpoint computation of which carry are not ad.zero: either
  # non-zero from init, or the carry out is non-zero. Each iteration promotes
  # at least one carry to non-zero. We need at most len(carry) iterations,
  # but we need one last iteration to prepare the jaxpr based on the final
  # carry_nz.
  carry_nz = init_nz
  for _ in range(1 + len(carry_nz)):
    nonzeros = const_nz + carry_nz + xs_nz
    jaxpr_jvp, nonzeros_out = ad.jvp_jaxpr(
        jaxpr, nonzeros, instantiate=carry_nz + [False] * num_ys)
    carry_nz_out, _ = nonzeros_out[:num_carry], nonzeros_out[num_carry:]
    if carry_nz_out == carry_nz:
      break
    else:
      carry_nz = _map(operator.or_, carry_nz, carry_nz_out)
  else:
    assert False, "Fixpoint not reached"

  tangents = [ad.instantiate_zeros(t) if nz else t
              for t, nz in zip(tangents, nonzeros)]

  consts, init, xs = split_list(primals, [num_consts, num_carry])
  all_tangents = split_list(tangents, [num_consts, num_carry])
  consts_dot, init_dot, xs_dot = _map(_prune_zeros, all_tangents)

  jaxpr_jvp_rearranged = ad.rearrange_binders(
      jaxpr_jvp,
      [num_consts, num_carry, num_xs], [len(consts_dot), len(init_dot), len(xs_dot)],
      [num_carry, num_ys], [len(init_dot), sum(nonzeros_out) - len(init_dot)])

  consts_linear, init_linear, xs_linear = split_list(linear, [num_consts, num_carry])
  jaxpr_jvp_linear = tuple(consts_linear + [True] * len(consts_dot)
                           + init_linear + [True] * len(init_dot)
                           + xs_linear + [True] * len(xs_dot))

  out_flat = scan_p.bind(
      *(consts + consts_dot + init + init_dot + xs + xs_dot),
      reverse=reverse, length=length, jaxpr=jaxpr_jvp_rearranged,
      num_consts=num_consts + len(consts_dot),
      num_carry=num_carry + len(init_dot),
      linear=jaxpr_jvp_linear, unroll=unroll,
      _split_transpose=_split_transpose)

  carry, carry_dot, ys, ys_dot = split_list(out_flat, [num_carry, len(init_dot), num_ys])
  primals_out = carry + ys
  tangents_out_iter = iter(carry_dot + ys_dot)
  tangents_out = [next(tangents_out_iter) if nz else ad_util.Zero.from_primal_value(p)
                  for p, nz in zip(primals_out, nonzeros_out)]
  return primals_out, tangents_out

def _scan_linearize(nzs, *primals_in, reverse: bool, length: int, num_consts:
                    int, num_carry: int, jaxpr: ClosedJaxpr, linear:
                    Sequence[bool], unroll: int, _split_transpose: bool):
  const_nz, init_nz, xs_nz = split_list(nzs, [num_consts, num_carry])
  num_ys = len(jaxpr.out_avals) - num_carry
  carry_nz = init_nz
  allow_fwds = ([True] * len(jaxpr.consts) +
                [(i < num_consts or i >= num_consts + num_carry) and
                 not isinstance(x, np.ndarray) for i, x in enumerate(primals_in)])
  for _ in range(1 + num_carry):
    nzs = const_nz + carry_nz + xs_nz
    primal_jaxpr, num_res_out, nzs_out, in_fwd_res, tangent_jaxpr = \
        ad.linearize_jaxpr(jaxpr, nzs, allow_fwds=allow_fwds,
                           instantiate=carry_nz + [False] * num_ys)
    carry_nz_out = nzs_out[:num_carry]
    if carry_nz_out == carry_nz:
      break
    else:
      carry_nz = _map(operator.or_, carry_nz, carry_nz_out)
  else:
    assert False, "Fixpoint not reached"
  num_res_in = len(in_fwd_res)
  num_primals_out = len(primal_jaxpr.out_avals) - num_res_out

  # At this point all non-forwarded residuals produced by primal_jaxpr are at
  # the end. We want to hoist out loop-invariant ones:
  # Before:
  #  [*const_primals_in , *carry_ext_primals_in] -> [*primals_out, *non_fwd_res]
  # After:
  #  [*const_primals_in_, *carry_ext_primals_in] -> [*primals_out, *ext_res]
  # where, modulo hoisted res not being broadcasted by the scan,
  #  non_fwd_res = merge_lists(which_hoisted, ext_res, hoisted_res)
  const_primals_in, carry_ext_primals_in = split_list(primals_in, [num_consts])
  primal_jaxpr, const_primals_in_, which_hoisted, hoisted_res = \
      _scan_known_hoisting(primal_jaxpr, const_primals_in, num_res_out)
  del num_res_out

  # To make tangent_jaxpr match the scan calling convention, move to the back
  # binders that don't correspond to hoisted or const-forwarded residuals.
  #   Before: [*res, *tangents_in] -> [*tangents_out]
  #   After: [*int_res, *tangents_in, *ext_res] -> [*tangents_out]
  num_tangents_in = len(tangent_jaxpr.in_avals) - num_res_in
  which_hoisted_ = iter(which_hoisted)
  res_to_move = [not next(which_hoisted_) if f is None else
                 f >= len(jaxpr.consts) + num_consts + num_carry
                 for f in in_fwd_res]
  assert next(which_hoisted_, None) is None
  tangent_jaxpr = pe.move_binders_to_back(
      tangent_jaxpr, res_to_move + [False] * num_tangents_in)

  # Run the primal scan (if it has any outputs or effects).
  if not primal_jaxpr.out_avals and not primal_jaxpr.effects:
    out = []
  else:
    linear_ = (False,) * len(primal_jaxpr.in_avals)  # TODO conservative
    out = scan_p.bind(*const_primals_in_, *carry_ext_primals_in,
                      jaxpr=primal_jaxpr, reverse=reverse, length=length,
                      num_consts=len(const_primals_in_), num_carry=num_carry,
                      linear=linear_, unroll=unroll,
                      _split_transpose=_split_transpose)
  primals_out, ext_res = split_list(out, [num_primals_out])

  # Complete res using hoisted_res and input forwards.
  res = subs_list(in_fwd_res, [*jaxpr.consts, *primals_in],
                  merge_lists(which_hoisted, ext_res, hoisted_res))

  def tangent_fun(res, *tangents):
    int_res, ext_res = partition_list(res_to_move, res)
    nz_tangents = [ad.instantiate_zeros(x) for nz, x in zip(nzs, tangents) if nz]
    tangent_linear = ((False,) * len(int_res) + (True,) * len(nz_tangents) +
                      (False,) * len(ext_res))
    tangent_num_consts = len(int_res) + sum(nzs[:num_consts])
    tangent_num_carry = sum(nzs[num_consts:num_consts + num_carry])
    nz_tangents_out = scan_p.bind(
        *int_res, *nz_tangents, *ext_res, jaxpr=tangent_jaxpr, reverse=reverse,
        length=length, num_consts=tangent_num_consts,
        num_carry=tangent_num_carry, linear=tangent_linear, unroll=unroll,
        _split_transpose=_split_transpose)
    tangent_avals_out = [v.aval.to_tangent_aval() for v in jaxpr.jaxpr.outvars]
    nz_tangents_out_ = iter(nz_tangents_out)
    tangents_out = [next(nz_tangents_out_) if nz else ad.Zero(aval)
                    for aval, nz in zip(tangent_avals_out, nzs_out)]
    assert next(nz_tangents_out_, None) is None
    return tangents_out

  return primals_out, nzs_out, res, tangent_fun

def _scan_known_hoisting(jaxpr_known, known_consts, num_res):
  # To disable:
  # return jaxpr_known, known_consts, [False] * num_res, []

  consts = [pe.PartialVal.unknown(a) if isinstance(a := typeof(c), AbstractRef)
            else pe.PartialVal.known(c) for c in known_consts]
  others = _map(pe.PartialVal.unknown, jaxpr_known.in_avals[len(consts):])
  num_known_outs = len(jaxpr_known.out_avals) - num_res
  with source_info_util.reset_name_stack():
    jaxpr_known_, pvals_out, new_known_consts = pe.trace_to_jaxpr_nounits(
        lu.wrap_init(core.jaxpr_as_fun(jaxpr_known),
                     debug_info=jaxpr_known.jaxpr.debug_info),
        consts + others, instantiate=[True] * num_known_outs + [False] * num_res)
  jaxpr_known = pe.close_jaxpr(pe.convert_constvars_jaxpr(jaxpr_known_))
  res_pvals = pvals_out[num_known_outs:]
  which_hoisted = [pval.is_known() for pval in res_pvals]
  hoisted_res = [pval.get_known() for pval in res_pvals if pval.is_known()]
  mut_consts = [c for c in known_consts if isinstance(typeof(c), AbstractRef)]
  return jaxpr_known, [*new_known_consts, *mut_consts], which_hoisted, hoisted_res


def _scan_partial_eval(trace, *tracers, reverse: bool,
                       length: int, num_consts: int, num_carry: int,
                       jaxpr: ClosedJaxpr, linear: Sequence[bool],
                       unroll: int, _split_transpose: bool):
  num_ys = len(jaxpr.out_avals) - num_carry
  unknowns = [not t.pval.is_known() for t in tracers]
  const_uk, init_uk, xs_uk = split_list(unknowns, [num_consts, num_carry])

  # Fixpoint computation of which carry elements are unknown. Each iteration
  # promotes at least one carry to unknown. We need at most len(carry)
  # iterations to decide carry_uk, plus one to prepare the jaxpr.
  carry_uk = init_uk
  # Don't allow forwarding from the carry or numpy.ndarrays.
  fwd = [(i < num_consts or i >= num_consts + num_carry) and
         not isinstance(t.pval.get_known(), np.ndarray) for i, t in enumerate(tracers)]
  for _ in range(1 + len(carry_uk)):
    unknowns = const_uk + carry_uk + xs_uk
    jaxpr_known, jaxpr_unknown, out_uk, res_avals, in_fwd_res = \
        pe.partial_eval_jaxpr_nounits_fwd(
            jaxpr, unknowns, instantiate=carry_uk + [False] * num_ys, fwd=fwd)
    carry_uk_out, ys_uk = split_list(out_uk, [num_carry])
    if carry_uk_out == carry_uk:
      break
    else:
      carry_uk = _map(operator.or_, carry_uk, carry_uk_out)
  else:
    assert False, "Fixpoint not reached"
  num_res_out, num_res_in = len(res_avals), len(in_fwd_res)
  num_knowns_out = len(jaxpr_known.out_avals) - num_res_out
  num_consts_known = num_consts - sum(const_uk)
  num_carry_known = num_carry - sum(carry_uk)
  del res_avals, carry_uk_out

  # Instantiate those inputs which must be treated as unknown from the fixpoint.
  tracers = [trace.instantiate_const(t) if uk else t
             for t, uk in zip(tracers, unknowns)]
  known_ins   = [t.pval.get_known() for t in tracers if     t.pval.is_known()]
  unknown_ins = [t                  for t in tracers if not t.pval.is_known()]

  # At this point all non-forwarded residuals are treated as extensive outputs
  # of jaxpr_known. Hoist out those that only depend on consts.
  #   Before: jaxpr_known: [*known_ins] -> [*known_outs, *non_fwd_res]
  #   After: jaxpr_known: [*known_consts_, *known_ins] -> [*known_outs, *ext_res]
  # where, modulo hoisted res not being broadcast, we have
  #   non_fwd_res = merge_lists(which_hoisted, ext_res, hoisted_res)
  known_consts, known_ins = split_list(known_ins, [num_consts_known])
  jaxpr_known, known_consts_, which_hoisted, hoisted_res = \
      _scan_known_hoisting(jaxpr_known, known_consts, num_res_out)
  del num_res_out  # changed

  # To make jaxpr_unknown match the scan calling convention, move to the back
  # binders that don't correspond to hoisted or const-forwarded residuals.
  #   Before: jaxpr_unknown: [*res, *unknown_ins] -> [*unkown_outs]
  #   After: jaxpr_unkonwn: [*int_res, *unknown_ins, *ext_res] -> [*unknown_outs]
  num_unk_in = len(jaxpr_unknown.in_avals) - num_res_in
  which_hoisted_ = iter(which_hoisted)
  res_to_move = [not next(which_hoisted_) if f is None else
                 f >= len(jaxpr.consts) + num_consts_known + num_carry_known
                 for f in in_fwd_res]
  assert next(which_hoisted_, None) is None
  jaxpr_unknown = pe.move_binders_to_back(
      jaxpr_unknown, res_to_move + [False] * num_unk_in)

  # Run the known part of the scan (if it has any outputs or effects).
  linear_known, linear_unknown = partition_list(unknowns, linear)
  if not jaxpr_known.out_avals and not jaxpr_known.effects:
    known_outs_ext_res = []
  else:
    linear_known = [False] * len(jaxpr_known.in_avals)  # TODO conservative
    assert len(known_consts_) + len(known_ins) == len(jaxpr_known.in_avals)
    known_outs_ext_res = scan_p.bind(
        *known_consts_, *known_ins, jaxpr=jaxpr_known, reverse=reverse,
        length=length, num_consts=len(known_consts_),
        num_carry=num_carry_known, linear=(*linear_known,), unroll=unroll,
        _split_transpose=_split_transpose)
  known_outs, ext_res = split_list(known_outs_ext_res, [num_knowns_out])

  # Complete non_fwd_res and then res, then split to match binders.
  non_fwd_res = merge_lists(which_hoisted, ext_res, hoisted_res)
  non_fwd_res_ = iter(non_fwd_res)
  res = [next(non_fwd_res_) if f is None
         else [*jaxpr.consts, *known_consts, *known_ins][f] for f in in_fwd_res]
  assert next(non_fwd_res_, None) is None
  int_res, ext_res = partition_list(res_to_move, res)

  # Create input tracers for jaxpr_unknown bind.
  unknown_inputs = [t for t in tracers if not t.pval.is_known()]
  int_res = _map(trace.new_instantiated_const, int_res)
  ext_res = _map(trace.new_instantiated_const, ext_res)
  # Create output tracers for jaxpr_unknown bind, adapting extensive shapes.
  carry_avals, y_avals = split_list(jaxpr_unknown.out_avals, [sum(carry_uk)])
  ys_avals = [core.unmapped_aval(length, 0, y_aval) for y_aval in y_avals]
  out_tracers = [pe.JaxprTracer(trace, pe.PartialVal.unknown(a), None)
                 for a in it.chain(carry_avals, ys_avals)]
  del carry_avals, y_avals
  # Create equation.
  linear_unknown = [False] * len(int_res) + linear_unknown + [False] * len(ext_res)
  assert len(linear_unknown) == len(jaxpr_unknown.in_avals)
  name_stack = source_info_util.current_name_stack()[len(trace.name_stack):]
  source = source_info_util.current().replace(name_stack=name_stack)
  unknown_tracers_in = [*int_res, *unknown_inputs, *ext_res]
  eqn = pe.new_eqn_recipe(trace, unknown_tracers_in, out_tracers, scan_p,
                          dict(reverse=reverse, length=length, unroll=unroll,
                               jaxpr=jaxpr_unknown, linear=(*linear_unknown,),
                               num_consts=len(int_res) + sum(const_uk),
                               num_carry=sum(carry_uk),
                               _split_transpose=_split_transpose),
                          jaxpr_unknown.effects, source)
  for t in out_tracers: t.recipe = eqn
  if effects.partial_eval_kept_effects.filter_in(jaxpr_unknown.effects):
    trace.effect_handles.append(pe.EffectHandle(unknown_tracers_in, eqn))

  # Merge known and unknown outputs into final result.
  return util.merge_lists(out_uk, known_outs, out_tracers)

def _maybe_put(x):
  if isinstance(x, np.ndarray):
    aval = core.shaped_abstractify(x)
    s = sharding.SingleDeviceSharding(xb.local_devices(backend='cpu')[0])
    result_handler = pxla.global_aval_to_result_handler(aval, s, False)
    return result_handler(pxla.shard_args([s], [None], [None], [x]))
  else:
    return x

@weakref_lru_cache
def _rearrange_mutable_binders(
    jaxpr: ClosedJaxpr, num_prefix: int, num_binders: int
) -> ClosedJaxpr:
  fst, invars, rst = split_list(jaxpr.jaxpr.invars, [num_prefix, num_binders])
  is_mutable = [isinstance(v.aval, AbstractRef) for v in invars]
  immut_invars, mut_invars = partition_list(is_mutable, invars)
  new_invars = [*fst, *mut_invars, *immut_invars, *rst]

  arg_names = jaxpr.jaxpr.debug_info.safe_arg_names(len(jaxpr.in_avals))
  fst, names, rst = split_list(arg_names, [num_prefix, num_binders])
  immut_names, mut_names = partition_list(is_mutable, names)
  dbg = jaxpr.jaxpr.debug_info._replace(
      arg_names=[*fst, *mut_names, *immut_names, *rst])

  # TODO(mattjj): don't we need to re-number effects? test coverage?
  new_effs = pe._renumber_effects((*jaxpr.jaxpr.constvars, *new_invars),
                                  (*jaxpr.jaxpr.constvars, *jaxpr.jaxpr.invars),
                                  jaxpr.jaxpr.effects)
  new_jaxpr = jaxpr.jaxpr.replace(invars=new_invars, effects=new_effs,
                                  debug_info=dbg)
  if config.enable_checks.value: core.check_jaxpr(new_jaxpr)
  return ClosedJaxpr(new_jaxpr, jaxpr.consts)

def _scan_transpose(cts, *args, reverse, length, num_consts,
                    num_carry, jaxpr, linear, unroll, _split_transpose):
  # we've only implemented transposing scans with specific lin/nonlin patterns
  consts_lin, init_lin, xs_lin = split_list(linear, [num_consts, num_carry])
  num_ires = len(consts_lin) - sum(consts_lin)
  num_eres = len(xs_lin) - sum(xs_lin)
  if consts_lin != [False] * num_ires + [True] * (len(consts_lin) - num_ires):
    raise NotImplementedError
  if xs_lin != [True] * (len(xs_lin) - num_eres) + [False] * num_eres:
    raise NotImplementedError
  if not all(init_lin):
    pass  # TODO(mattjj): error check https://github.com/jax-ml/jax/issues/1963

  # We follow a funny convention of passing cotangent refs like primals, so they
  # appear in `args` mixed in with the UndefinedPrimals of `T d` and `T a`.
  # Rearrange jaxpr binders and arguments to put cotangent mutable arrays first:
  #   Before: [ires,               T d, T c,               T a, eres] -> [T c, T b]
  #   After:  [ires, T d_mut, T d_pure, T c, T a_mut, T a_pure, eres] -> [T c, T b]
  # where
  #   * `ires` means intensive (not scanned over / const) residuals
  #   * `T d` means the intensive tangents
  #   * `T c` means the tangent carry
  #   * `T a` means the extensive (scanned over) tangent inputs
  #   * `eres` means the extensive residuals
  #   * `T b` means the extensive tangent outputs
  ires, consts_dot, carry_dot, xs_dot, eres = split_list(
      args, [num_ires, num_consts - num_ires, num_carry, sum(xs_lin)])
  _, const_avals, _, xs_avals, _ = split_list(
      jaxpr.in_avals, [num_ires, num_consts - num_ires, num_carry, sum(xs_lin)])
  is_mutable = [isinstance(a, AbstractRef) for a in const_avals]
  immut_consts_dot, mut_consts_bar = partition_list(is_mutable, consts_dot)
  jaxpr = _rearrange_mutable_binders(jaxpr, num_ires, num_consts - num_ires)
  del const_avals, consts_dot
  is_mutable_ = [isinstance(a, AbstractRef) for a in xs_avals]
  immut_xs_dot, mut_xs_bar = partition_list(is_mutable_, xs_dot)
  jaxpr = _rearrange_mutable_binders(jaxpr, num_consts + num_carry, sum(xs_lin))
  del xs_avals, xs_dot
  # Check that pure tangent values are all UndefinedPrimals, and mutable
  # 'tangent values' are not (since we actually put cotangent refs there).
  assert not any(ad.is_undefined_primal(r) for r in ires)
  assert not any(ad.is_undefined_primal(x) for x in mut_consts_bar)
  # TODO(mattjj): re-enable these asserts
  # assert     all(ad.is_undefined_primal(x) for x in immut_consts_dot)
  # assert     all(ad.is_undefined_primal(x) for x in carry_dot)
  # assert     all(ad.is_undefined_primal(x) for x in immut_xs_dot)
  assert not any(ad.is_undefined_primal(r) for r in eres)
  del args

  # Take apart passed-in cotangents to identify which are sym zeros.
  ct_carry, ct_ys = split_list(cts, [num_carry])
  ct_carry = _map(ad.instantiate_zeros, ct_carry)
  ct_ys_is_zeros = [type(ct_y) is ad.Zero for ct_y in ct_ys]
  ct_ys_nz = [x for x in ct_ys if type(x) is not ad.Zero]
  ct_immut_consts = _map(ad_util.zeros_like_aval,
                         jaxpr.in_avals[num_ires+len(mut_consts_bar):num_consts])

  jaxpr_trans = _transpose_scan_jaxpr(
      jaxpr, num_ires, len(mut_consts_bar), len(immut_consts_dot),
      len(mut_xs_bar), len(immut_xs_dot), num_eres, tuple(ct_ys_is_zeros))

  linear_trans = ([False] * num_ires +
                  [True] * (len(mut_consts_bar) + len(immut_consts_dot) +
                            len(carry_dot) + len(mut_xs_bar) + len(ct_ys_nz)) +
                  [False] * num_eres)
  transpose_inputs = [*ires, *mut_consts_bar, *ct_immut_consts, *ct_carry,
                      *mut_xs_bar, *ct_ys_nz, *eres]

  if not _split_transpose:
    outs = scan_p.bind(
        *transpose_inputs,
        reverse=not reverse, length=length, jaxpr=jaxpr_trans,
        num_consts=num_ires + len(mut_consts_bar),
        num_carry=len(immut_consts_dot) + len(carry_dot),
        linear=tuple(linear_trans), unroll=unroll,
        _split_transpose=False)
  else:
    if len(mut_consts_bar): raise NotImplementedError
    transpose_num_out_carry = num_consts-num_ires+num_carry
    inst_mask = [False] * transpose_num_out_carry +  [True] * (
        len(jaxpr_trans.out_avals) - transpose_num_out_carry)

    unknowns_mask = [False] * (len(transpose_inputs) - len(eres)) + [
        True
    ] * len(eres)

    # The residuals may contain original parameters (e.g. forwarded extensive
    # array arguments) and residuals from the primal. Hence we iterate and
    # update all values of the mask that we've set to True (i.e. 'unknown') to
    # see if we should actually push them to the known computation in order to
    # perform the scan (known) - map (unknown) split. The test effectively is
    # done by comparing the output masks.
    #
    # TODO(dvytin): improve performance by doing backwards abstract eval.
    #
    # For example, a mask arising from a relu() is an extensive residual, yet
    # only really used in the backpropagation scan, not in the unknown map. But
    # an intermediate activation of a matmul will be used only in the map part.
    # If we were to erroneously push the relu mask to the unknown part, then,
    # in the output, the partial evaluator will also pull the loop-carried state
    # to the unknown, and that is something we can test by comparing the output
    # mask of pe against our intended inst mask.
    for index in range(len(jaxpr_trans.in_avals)):
      if unknowns_mask[index]:
        mask_for_dependence = [False]*len(jaxpr_trans.in_avals)
        mask_for_dependence[index] = True  # try moving this to unknown
        _, _, outs_for_dependence, _ = pe.partial_eval_jaxpr_nounits(
            jaxpr_trans, mask_for_dependence, inst_mask)
        if inst_mask != outs_for_dependence:
          unknowns_mask[index] = False

    jaxpr_known_body, jaxpr_unknown_body, outs_mask, res_avals = (
        pe.partial_eval_jaxpr_nounits(jaxpr_trans, unknowns_mask, inst_mask)
    )

    num_knowns = len(outs_mask) - sum(outs_mask)

    linear_list = list(linear_trans)
    known_linear = [
        l for mask, l in zip(unknowns_mask, linear_list) if not mask
    ]
    unknown_linear = [l for mask, l in zip(unknowns_mask, linear_list) if mask]
    unknown_linear = [False] * len(res_avals) + unknown_linear

    known_args = [
        arg for mask, arg in zip(unknowns_mask, transpose_inputs) if not mask
    ]
    unknown_args = [
        arg for mask, arg in zip(unknowns_mask, transpose_inputs) if mask
    ]
    # 1. Apply the known scan.
    knowns_and_residual = scan_p.bind(
        *known_args,
        reverse=not reverse,
        length=length,
        num_consts=num_ires,
        num_carry=transpose_num_out_carry,
        jaxpr=jaxpr_known_body,
        linear=tuple(known_linear),
        unroll=unroll,
        _split_transpose=False,  # Just generate the loop now.
    )
    known_results, residuals = split_list(knowns_and_residual, [num_knowns])

    # 2. Apply the unknown map to residuals and unknown arguments.
    unknown_results = scan_p.bind(
        *residuals, *unknown_args,
        reverse=reverse,  # Keep reverse as is for better scheduling.
        length=length,
        num_consts=0,
        num_carry=0,
        jaxpr=jaxpr_unknown_body,
        linear=tuple(unknown_linear),
        unroll=unroll,
        _split_transpose=False,  # Just generate the loop now.
    )
    known_results_iter = iter(known_results)
    unknown_results_iter = iter(unknown_results)
    outs = [
        next(known_results_iter) if not mask else next(unknown_results_iter)
        for mask in outs_mask
    ]

  ct_immut_consts, ct_init, ct_immut_xs = split_list(outs, [len(immut_consts_dot), len(carry_dot)])
  ct_consts = merge_lists(is_mutable, ct_immut_consts, [None] * len(mut_consts_bar))
  ct_xs = merge_lists(is_mutable_, ct_immut_xs, [None] * len(mut_xs_bar))
  return [None] * num_ires + ct_consts + ct_init + ct_xs + [None] * num_eres


# transpose_scan_jaxpr converts the jaxpr signature:
#  Before: [(ires,  T d_mut     T d_pure), T c,  (CT a_mut, T a, eres)] -> [T c,  T b]
#           ---------- consts -----------        --------- ext -------
#
#  After: [(ires, CT d_mut), (CT d_pure,  CT c), (CT a_mut, CT b, eres)] -> [(CT d_pure, CT c), CT a]
#           --- consts ----  ----- carry ------  --------- ext --------
@weakref_lru_cache
def _transpose_scan_jaxpr(
    jaxpr: ClosedJaxpr,
    num_ires: int,
    num_d_mut: int,
    num_d_pure: int,
    num_a_mut: int,
    num_a_pure: int,
    num_eres: int,
    ct_b_is_zeros: Sequence[bool]):
  num_d = num_d_mut + num_d_pure
  num_a = num_a_mut + num_a_pure
  num_b_nz = len(ct_b_is_zeros) - sum(ct_b_is_zeros)
  num_c = len(jaxpr.out_avals) - len(ct_b_is_zeros)
  assert num_a == len(jaxpr.in_avals) - num_ires - num_d - num_c - num_eres

  ires_avals, d_mut_avals, d_pure_avals, c_avals, a_mut_avals, a_pure_avals, eres_avals = split_list(
      jaxpr.in_avals, [num_ires, num_d_mut, num_d_pure, num_c, num_a_mut, num_a_pure])
  _, b_avals = split_list(jaxpr.out_avals, [num_c])
  b_avals_nz = [a for a, z in zip(b_avals, ct_b_is_zeros) if not z]

  # TODO(mattjj,dougalm): map to cotangent types...
  def transposed(*ct_args):
    ires, d_mut_bar, d_pure, c_bar, a_mut_bar, b_bar, eres = split_list(
        ct_args, [num_ires, num_d_mut, num_d_pure, num_c, num_a_mut, num_b_nz])
    b_bar_ = iter(b_bar)
    b_bar = [ad.Zero(a) if z else next(b_bar_) for a, z in zip(b_avals, ct_b_is_zeros)]
    assert next(b_bar_, None) is None
    primals = (
        ires + d_mut_bar +
        [ad.UndefinedPrimal(aval) for aval in [*d_pure_avals, *c_avals]] +
        a_mut_bar + [ad.UndefinedPrimal(aval) for aval in a_pure_avals] + eres)
    cts_out = ad.backward_pass(
        jaxpr.jaxpr, False, jaxpr.consts, primals, c_bar + b_bar)
    _, new_d_pure, new_c_bar, _, a_bar, _ = split_list(
        cts_out, [num_ires + num_d_mut, num_d_pure, num_c, num_a_mut, num_a_pure])
    d_pure = _map(ad.instantiate_zeros, _map(ad.add_tangents, d_pure, new_d_pure))
    new_c_bar = _map(ad.instantiate_zeros, new_c_bar)
    a_bar = _map(ad.instantiate_zeros, a_bar)
    return [*d_pure, *new_c_bar, *a_bar]

  transposed_wrapped = lu.wrap_init(transposed, debug_info=jaxpr.jaxpr.debug_info)
  trans_avals = *ires_avals, *d_mut_avals, *d_pure_avals, *c_avals, *a_mut_avals, *b_avals_nz, *eres_avals
  trans_jaxpr = _make_closed_jaxpr(transposed_wrapped, trans_avals)
  return trans_jaxpr


def _scan_batching_rule(axis_data, args,
                        dims, reverse, length,
                        jaxpr, num_consts, num_carry, linear, unroll,
                        _split_transpose):
  num_ys = len(jaxpr.out_avals) - num_carry
  orig_batched = [d is not batching.not_mapped for d in dims]
  const_batched, init_batched, xs_batched = split_list(orig_batched, [num_consts, num_carry])

  # Fixpoint computation of which carry are batched: either
  # batched from init, or the carry out is batched. Each iteration promotes
  # at least one carry to batched. We need at most len(carry) iterations,
  # but we need one last iteration to prepare the jaxpr based on the final
  # carry_batched.
  carry_batched = init_batched
  for _ in range(1 + len(carry_batched)):
    batched = const_batched + carry_batched + xs_batched
    jaxpr_batched, batched_out = batching.batch_jaxpr(
        jaxpr, axis_data, batched,
        instantiate=carry_batched + [False] * num_ys)
    carry_batched_out, ys_batched = batched_out[:num_carry], batched_out[num_carry:]
    if carry_batched_out == carry_batched:
      break
    else:
      carry_batched = _map(operator.or_, carry_batched, carry_batched_out)
  else:
    assert False, "Fixpoint not reached"

  consts, init, xs = split_list(args, [num_consts, num_carry])
  consts_bdims, init_bdims, xs_bdims = split_list(dims, [num_consts, num_carry])
  new_consts = [batching.moveaxis(x, d, 0) if d is not batching.not_mapped and d != 0
                else x for x, d in zip(consts, consts_bdims)]
  new_init = [batching.broadcast(x, axis_data.size, 0, axis_data.explicit_mesh_axis)
              if now_batched and not was_batched
              else batching.moveaxis(x, d, 0) if now_batched else x
              for x, d, was_batched, now_batched in
              zip(init, init_bdims, init_batched, carry_batched)]
  new_xs = [batching.moveaxis(x, d, 1) if d is not batching.not_mapped and d != 1
            else x for x, d in zip(xs, xs_bdims)]
  new_args = new_consts + new_init + new_xs

  outs = scan_p.bind(
      *new_args, reverse=reverse, length=length, jaxpr=jaxpr_batched,
      num_consts=num_consts, num_carry=num_carry, linear=linear, unroll=unroll,
      _split_transpose=_split_transpose)
  carry_bdims = [0 if b else batching.not_mapped for b in carry_batched]
  ys_bdims = [1 if b else batching.not_mapped for b in ys_batched]
  return outs, carry_bdims + ys_bdims

@weakref_lru_cache
def _cached_scan_pad_jaxpr(jaxpr):
  return ClosedJaxpr(*pe.pad_jaxpr(jaxpr.jaxpr, jaxpr.consts))

def _scan_padding_rule(in_avals, out_avals, *args, jaxpr, **params):
  return scan_p.bind(*args, jaxpr=_cached_scan_pad_jaxpr(jaxpr), **params)

def _scan_dce_rule(used_outputs: list[bool], eqn: core.JaxprEqn
                   ) -> tuple[list[bool], core.JaxprEqn | None]:
  if not any(used_outputs) and not pe.has_effects(eqn):
    return [False] * len(eqn.invars), None
  jaxpr = eqn.params['jaxpr']
  num_consts, num_carry = eqn.params['num_consts'], eqn.params['num_carry']
  num_xs = len(jaxpr.in_avals) - num_consts - num_carry
  used_carry_out, used_extensive_out = split_list(used_outputs, [num_carry])
  for i in range(1 + num_carry):
    used_outputs = used_carry_out + used_extensive_out
    jaxpr_dce, used_inputs = pe.dce_jaxpr(
        jaxpr.jaxpr, used_outputs,
        instantiate=[False] * num_consts + used_carry_out + [False] * num_xs)
    used_consts, used_carry_in, used_extensive_in = \
        split_list(used_inputs, [num_consts, num_carry])
    if list(used_carry_in) == list(used_carry_out):
      break
    else:
      used_carry_out = _map(operator.or_, used_carry_out, used_carry_in)
  else:
    assert False, "Fixpoint not reached"
  if config.enable_checks.value: core.check_jaxpr(jaxpr.jaxpr)

  new_linear = [l for l, u in zip(eqn.params['linear'], used_inputs) if u]
  new_params = dict(eqn.params, num_consts=sum(used_consts),
                    num_carry=sum(used_carry_in), linear=tuple(new_linear),
                    jaxpr=ClosedJaxpr(jaxpr_dce, jaxpr.consts))
  # TODO(mattjj,sharadmv): don't assume effects are never DCE'd?
  new_invars = [v for v, used in zip(eqn.invars, used_inputs) if used]
  new_outvars = [v for v, used in zip(eqn.outvars, used_outputs) if used]
  _, new_effects = eqn.primitive.abstract_eval(*[v.aval for v in new_invars],
                                               **new_params)
  new_eqn = pe.new_jaxpr_eqn(
      new_invars,
      new_outvars,
      eqn.primitive, new_params, new_effects, eqn.source_info, eqn.ctx)
  assert len(new_eqn.invars ) == len(new_params['jaxpr'].in_avals )
  assert len(new_eqn.outvars) == len(new_params['jaxpr'].out_avals)
  return used_inputs, new_eqn

# TODO(mattjj): de-duplicate code with _scan_partial_eval
def _scan_partial_eval_custom(saveable, unks_in, inst_in, eqn):
  jaxpr = eqn.params['jaxpr']
  num_consts, num_carry = eqn.params['num_consts'], eqn.params['num_carry']
  num_ys = len(jaxpr.out_avals) - num_carry

  # Fixpoint (trivial on 'inst_in', since we might as well make all inputs
  # available as DCE can subsequently prune any unused ones)
  const_uk, carry_uk, xs_uk = split_list(unks_in, [num_consts, num_carry])
  for _ in range(1 + len(carry_uk)):
    unks_in = const_uk   + carry_uk   + xs_uk
    jaxpr_known_, jaxpr_staged_, unks_out, inst_out, num_res = \
        pe.partial_eval_jaxpr_custom(
            jaxpr.jaxpr, in_unknowns=unks_in, in_inst=True,
            ensure_out_unknowns=carry_uk + [False] * num_ys,
            ensure_out_inst=True, saveable=saveable)
    carry_uk_out, ys_uk = split_list(unks_out, [num_carry])
    if carry_uk_out == carry_uk:
      break
    else:
      carry_uk = _map(operator.or_, carry_uk, carry_uk_out)
  else:
    assert False, "Fixpoint not reached"
  jaxpr_known  = ClosedJaxpr(jaxpr_known_ , jaxpr.consts)
  jaxpr_staged = ClosedJaxpr(jaxpr_staged_, jaxpr.consts)

  # Move all residual binders to the back of jaxpr_staged so they're extensive.
  # TODO(mattjj): make jaxpr_staged only take instantiated inputs
  res_avals = jaxpr_staged.in_avals[:num_res]
  jaxpr_staged = pe.move_binders_to_back(
      jaxpr_staged, [True] * num_res + [False] * len(jaxpr.in_avals))

  # Instantiate all inputs (b/c jaxpr_staged takes all inputs, corresponding to
  # passing in_inst argument to partial_eval_jaxpr_custom above).
  new_inst = [x for x, inst in zip(eqn.invars, inst_in)
              if type(x) is core.Var and not inst]
  inst_in = [True] * len(inst_in)

  # As an optimization, hoist loop-invariant residuals out of the loop rather
  # than using extensive outputs for them. See _scan_partial_eval for comments.
  num_const_known = len(const_uk) - sum(const_uk)
  num_carry_known = len(carry_uk) - sum(carry_uk)
  num_xs_known    = len(   xs_uk) - sum(   xs_uk)
  const_donthoist = [isinstance(a, state.AbstractRef)
                     for a in jaxpr_known.in_avals[:num_const_known]]
  jaxpr_known_hoist, jaxpr_known_loop, loop_dep, consts_known_lp_avals = \
      pe.partial_eval_jaxpr_nounits(
          jaxpr_known,
          const_donthoist + [True] * (num_carry_known + num_xs_known),
          [True] * (len(unks_out) - sum(unks_out)) + [False] * num_res)
  # jaxpr_known_hoist produces intensive residuals followed by the constants for
  # jaxpr_known_loop. We adjust jaxpr_staged to accept intensive res as consts.
  _, loop_dep_res = split_list(loop_dep, [len(loop_dep) - num_res])
  jaxpr_staged = pe.move_binders_to_front(
      jaxpr_staged, [False] * sum(inst_in) + _map(operator.not_, loop_dep_res))
  num_intensive_res = len(loop_dep_res) - sum(loop_dep_res)
  del loop_dep, num_carry_known, num_xs_known, const_uk

  # Create residual variables.
  intensive_avals, ext_avals_mapped = partition_list(loop_dep_res, res_avals)
  ext_avals = [core.unmapped_aval(eqn.params['length'], 0, a)
               for a in ext_avals_mapped]
  newvar = core.gensym()
  intensive_res = _map(newvar, intensive_avals)
  extensive_res = _map(newvar, ext_avals)

  # Create known eqn, which is a call_p combining evaluation of
  # jaxpr_known_hoist and a scan of jaxpr_known_loop.
  ins_known, _ = partition_list(unks_in, eqn.invars)
  out_binders_known, _ = partition_list(unks_out, eqn.outvars)
  # jaxpr_known_loop takes as input constants output as res by jaxpr_known_hoist
  # (corresponding to consts_known_lp_avals) followed by known carry and xs.
  linear_known_ = [l for l, uk in zip(eqn.params['linear'], unks_in) if not uk]
  _, linear_known_ = split_list(linear_known_, [num_const_known])
  linear_known = [False] * len(consts_known_lp_avals) + linear_known_
  params_known = dict(eqn.params, jaxpr=jaxpr_known_loop,
                      num_consts=len(consts_known_lp_avals),
                      num_carry=len(carry_uk)-sum(carry_uk),
                      linear=tuple(linear_known))

  def known(*ins_known):
    consts_known_maybehoist, ins_known_lp = split_list(ins_known, [num_const_known])
    consts_known_hoist, consts_known_donthoist = \
        partition_list(const_donthoist, consts_known_maybehoist)
    out_hoist = core.jaxpr_as_fun(jaxpr_known_hoist)(*consts_known_hoist)
    intensive_res, consts_known_lp = split_list(out_hoist, [num_intensive_res])
    out_loop = scan_p.bind(*consts_known_lp, *consts_known_donthoist,
                           *ins_known_lp, **params_known)
    return [*intensive_res, *out_loop]
  call_jaxpr_, _, call_jaxpr_consts = pe.trace_to_jaxpr_dynamic(
      lu.wrap_init(known, debug_info=jaxpr_known_hoist.jaxpr.debug_info),
      [v.aval for v in ins_known])
  call_jaxpr = ClosedJaxpr(call_jaxpr_, call_jaxpr_consts)
  eqn_known = pe.new_jaxpr_eqn(
      ins_known, [*intensive_res, *out_binders_known, *extensive_res],
      core.closed_call_p, dict(call_jaxpr=call_jaxpr), call_jaxpr.effects,
      eqn.source_info, eqn.ctx)

  # Create the staged eqn.
  _, out_binders_staged = partition_list(inst_out, eqn.outvars)
  linear_staged = ([False] * len(intensive_res) + list(eqn.params['linear']) +
                   [False] * len(extensive_res))
  params_staged = dict(eqn.params, jaxpr=jaxpr_staged,
                       num_consts=len(intensive_res) + eqn.params['num_consts'],
                       linear=tuple(linear_staged))
  eqn_staged = pe.new_jaxpr_eqn([*intensive_res, *eqn.invars, *extensive_res],
                                out_binders_staged, eqn.primitive,
                                params_staged, jaxpr_staged.effects,
                                eqn.source_info, eqn.ctx)

  new_vars = [*new_inst, *intensive_res, *extensive_res]
  return eqn_known, eqn_staged, unks_out, inst_out, new_vars

def _scan_typecheck(bind_time, *in_atoms, reverse, length, num_consts,
                    num_carry, jaxpr, linear, unroll, _split_transpose):
  del _split_transpose
  if not bind_time:
    _, *in_atoms = in_atoms
  avals = [x.aval for x in in_atoms]
  tc = partial(_typecheck_param, 'scan')
  tc(reverse, 'reverse', 'bool', type(reverse) is bool)
  tc(num_consts, 'num_consts', 'non-negative int',
     type(num_consts) is int and num_consts >= 0)
  tc(num_carry, 'num_carry', 'non-negative int',
     type(num_carry) is int and num_carry >= 0)
  tc(jaxpr, 'jaxpr', 'ClosedJaxpr', type(jaxpr) is ClosedJaxpr)
  tc(linear, 'linear', 'tuple of bool',
     type(linear) is tuple and all(type(x) is bool for x in linear))
  tc(unroll, 'unroll', 'positive int', type(unroll) is int and unroll > 0)

  tc(length, 'length', 'non-negative int', length >= 0)

  if len(linear) != len(avals):
    raise core.JaxprTypeError(
      f'scan param linear has length {len(linear)} for {len(avals)} operands')

  const_avals, init_avals, x_avals = split_list(avals, [num_consts, num_carry])
  const_avals_jaxpr, init_avals_jaxpr, x_avals_jaxpr = split_list(
      jaxpr.in_avals, [num_consts, num_carry])
  carry_avals_jaxpr, y_avals_mapped = split_list(jaxpr.out_avals, [num_carry])
  x_avals_mapped = _map(partial(core.mapped_aval, length, 0), x_avals)
  y_avals = [core.unmapped_aval(length, 0, a)
             for a in y_avals_mapped]

  if not all(_map(core.typematch, init_avals_jaxpr, carry_avals_jaxpr)):
    raise core.JaxprTypeError(
      f'scan input carry input and output types mismatch: '
      f'\n{_avals_short(init_avals_jaxpr)}\nvs\n{_avals_short(carry_avals_jaxpr)}')
  if not all(_map(core.typecompat, const_avals_jaxpr, const_avals)):
    raise core.JaxprTypeError(
      f'scan jaxpr takes input const types\n{_avals_short(const_avals_jaxpr)},\n'
      f'called with consts of type\n{_avals_short(const_avals)}')
  if not all(_map(core.typecompat, init_avals_jaxpr, init_avals)):
    raise core.JaxprTypeError(
      f'scan jaxpr takes input carry types\n{_avals_short(init_avals_jaxpr)},\n'
      f'called with initial carry of type\n{_avals_short(init_avals)}')
  if not all(_map(core.typecompat, x_avals_jaxpr, x_avals_mapped)):
    raise core.JaxprTypeError(
      f'scan jaxpr takes input sequence types\n{_avals_short(x_avals_jaxpr)},\n'
      f'called with sequence whose items have type\n{_avals_short(x_avals_mapped)}')
  return [*init_avals, *y_avals], jaxpr.effects

def _scan_state_partial_discharge_rule(should_discharge, in_avals, out_avals, *args, jaxpr, num_consts,
                               num_carry, linear, unroll, reverse, length,
                               _split_transpose):
  # We're shuffling parameters between three signatures for the scan body:
  #   jaxpr      : (n_consts, n_carry, n_xs) -> (n_carry, n_ys)
  #   discharged : (n_consts, n_carry, n_xs) -> (n_carry, n_ys, n_ref_consts, n_ref_xs)
  #   wrapped    : (n_val_consts, (n_ref_consts, n_carry), (n_val_xs, n_ref_xs))
  #                  -> ((n_ref_consts, n_carry), (n_ys, n_ref_xs))
  # where we partition consts and xs between ref and non-ref versions:
  #   n_carry = (n_val_consts, n_ref_consts)
  #   n_xs    = (n_val_xs,     n_ref_xs)

  # avals from jaxpr (i.e. rank-reduced) rather than from caller
  jaxpr, in_avals, out_avals, consts = jaxpr.jaxpr, jaxpr.in_avals, jaxpr.out_avals, jaxpr.consts
  if consts: raise NotImplementedError
  n_consts = num_consts
  n_carry = num_carry
  n_xs = len(in_avals) - n_consts - n_carry
  n_ys = len(out_avals) - n_carry
  consts_avals, carry_avals, xs_avals = split_list_checked(in_avals,
    [n_consts, n_carry, n_xs])
  consts_discharge, carry_discharge, xs_discharge = split_list_checked(should_discharge,
    [n_consts, n_carry, n_xs])

  is_ref_const = [s and isinstance(a, state.AbstractRef) for s, a in zip(consts_discharge, consts_avals)]
  assert not any(isinstance(a, state.AbstractRef) for a in carry_avals)
  assert not any(carry_discharge)
  is_ref_xs = [s and isinstance(a, state.AbstractRef) for s, a in zip(xs_discharge, xs_avals)]
  n_ref_consts = sum(is_ref_const)
  n_val_consts = n_consts - n_ref_consts
  n_ref_xs = sum(is_ref_xs)
  n_val_xs = n_xs - n_ref_xs
  discharged_jaxpr, discharged_consts = state_discharge.discharge_state(jaxpr, (), should_discharge=should_discharge)
  if discharged_consts:
    raise NotImplementedError("Discharged jaxpr has consts. If you see this, "
                              "please open an issue at "
                              "https://github.com/jax-ml/jax/issues")
  def wrapped(*wrapped_args):
    val_consts, carry_in, ref_consts_in, val_xs, ref_xs_in = split_list_checked(wrapped_args,
      [n_val_consts, n_carry, n_ref_consts, n_val_xs, n_ref_xs])
    consts = merge_lists(is_ref_const, val_consts, ref_consts_in)
    xs = merge_lists(is_ref_xs, val_xs, ref_xs_in)
    outs = core.eval_jaxpr(discharged_jaxpr, (), *consts, *carry_in, *xs)
    carry_out, ys, ref_consts_out, ref_xs_out = split_list_checked(outs,
      [n_carry, n_ys, n_ref_consts, n_ref_xs])
    return [*carry_out, *ref_consts_out, *ys, *ref_xs_out]

  def arrange_jaxpr_args_for_wrapped(args):
    consts, carry_in, xs = split_list_checked(args, [n_consts, n_carry, n_xs])
    val_consts, ref_consts_in = partition_list(is_ref_const, consts)
    val_xs, ref_xs_in = partition_list(is_ref_xs, xs)
    return *val_consts, *carry_in, *ref_consts_in, *val_xs, *ref_xs_in

  # Rearrange the arguments such that they are:
  #   val_consts, carry, ref_consts, val_xs, ref_xs
  #
  # It is important that carry is immediately after the val_consts
  # because pallas pattern matches the leading argument type to figure
  # out if a scan_p eqn is equivalent to a fori loop (see
  # `pallas.utils.pattern_match_scan_to_fori_loop()`).
  args_for_wrapped = arrange_jaxpr_args_for_wrapped(args)
  linear_for_wrapped = arrange_jaxpr_args_for_wrapped(linear)
  avals_for_wrapped = arrange_jaxpr_args_for_wrapped(in_avals)
  # Get the const avals that we need to discharge and leave the rest as-is.
  deref_const_avals = tuple(c.inner_aval for c in avals_for_wrapped[n_val_consts + n_carry:n_consts + n_carry])
  deref_xs_avals = tuple(x.inner_aval for x in avals_for_wrapped[n_consts + n_carry + n_val_xs:])
  avals_for_wrapped_no_refs = (
      avals_for_wrapped[: n_val_consts + n_carry]
      + deref_const_avals
      + avals_for_wrapped[n_consts + n_carry :n_consts + n_carry + n_val_xs]
      + deref_xs_avals
  )
  # TODO(cperivol): avoid tracing the jaxpr twice. When doing so don't
  # forget to manage the effects.
  new_jaxpr, _, () = pe.trace_to_jaxpr_dynamic(
      lu.wrap_init(wrapped, debug_info=discharged_jaxpr.debug_info),
      avals_for_wrapped_no_refs)
  all_out = scan_p.bind(*args_for_wrapped,
                        jaxpr=ClosedJaxpr(new_jaxpr, ()),
                        length=length,
                        num_consts=n_val_consts,
                        num_carry=n_ref_consts + n_carry,
                        unroll=unroll,
                        reverse=reverse,
                        linear=linear_for_wrapped, _split_transpose=_split_transpose)
  carry_out, ref_consts_out, ys, ref_xs_out = split_list_checked(all_out,
    [n_carry, n_ref_consts, n_ys, n_ref_xs])
  refs_out_matching_in_avals = [
    *merge_lists(is_ref_const, [None] * n_val_consts, ref_consts_out),
    *[None] * n_carry,
    *merge_lists(is_ref_xs, [None] * n_val_xs, ref_xs_out)]
  assert len(refs_out_matching_in_avals) == len(in_avals)
  return refs_out_matching_in_avals, [*carry_out, *ys]

scan_p = core.Primitive("scan")
scan_p.is_effectful = lambda params: bool(params['jaxpr'].effects)  # type: ignore
scan_p.multiple_results = True
scan_p.skip_canonicalization = True
scan_p.def_impl(partial(dispatch.apply_primitive, scan_p))
scan_p.def_effectful_abstract_eval(_scan_abstract_eval)
ad.primitive_jvps[scan_p] = _scan_jvp
ad.primitive_transposes[scan_p] = _scan_transpose
ad.primitive_linearizations[scan_p] = _scan_linearize
pe.custom_partial_eval_rules[scan_p] = _scan_partial_eval
xla.register_initial_style_primitive(scan_p)
mlir.register_lowering(scan_p,
                       mlir.lower_fun(_scan_impl, multiple_results=True))
batching.fancy_primitive_batchers[scan_p] = _scan_batching_rule
core.custom_typechecks[scan_p] = partial(_scan_typecheck, False)
pe.partial_eval_jaxpr_custom_rules[scan_p] = _scan_partial_eval_custom
pe.padding_rules[scan_p] = _scan_padding_rule
pe.dce_rules[scan_p] = _scan_dce_rule
state_discharge.register_partial_discharge_rule(scan_p)(_scan_state_partial_discharge_rule)

def _is_high(jaxpr, **_) -> bool:
  return jaxpr.jaxpr.is_high
scan_p.is_high = _is_high  # type: ignore

def _to_lojax(*hi_args, jaxpr, num_carry, num_consts, linear, **params):

  # move box binders and hi_args from consts slots to carry slots
  to_move = [t.has_qdd for t in jaxpr.in_aval_qdds[:num_consts]]
  jaxpr = pe.move_invars_right(jaxpr, to_move)
  hi_args = _move_right(hi_args, to_move)
  num_consts -= sum(to_move)
  num_carry += sum(to_move)

  # expand num_consts, num_carry, linear according to lo types
  const_in_avals, carry_in_avals, _ = split_list(jaxpr.in_aval_qdds, [num_consts, num_carry])
  num_consts = sum(len(aval.lo_ty()) for aval in const_in_avals)
  num_carry = sum(len(aval.lo_ty()) for aval in carry_in_avals)
  linear = [l for aval, l_ in zip(jaxpr.in_aval_qdds, linear)
            for l in (l_,) * len(aval.lo_ty())]
  lo_muts_out = sum(len(aval.lo_ty()) for aval in jaxpr.final_aval_qdds if aval.has_qdd)

  # collect lo input values
  lo_args = [lo_val for aval, x in zip(jaxpr.in_aval_qdds, hi_args)
             for lo_val in (aval.read_loval(x) if aval.has_qdd
                            else aval.lower_val(x))]

  # lower the jaxpr and bind it using lo input values
  lo_jaxpr = pe.lower_jaxpr(jaxpr)
  all_outs = scan_p.bind(*lo_args, jaxpr=lo_jaxpr, num_consts=num_consts,
                         num_carry=num_carry, linear=tuple(linear), **params)
  out_mut, lo_outs = split_list(all_outs, [lo_muts_out])

  # collect and apply mutations
  out_mut_ = iter(out_mut)
  in_idx = {v: i for i, v in enumerate(jaxpr.jaxpr.invars)}

  for v in jaxpr.jaxpr.invars:
    if v.final_qdd is not None:
      qdd = v.final_qdd
      lo_vals = it.islice(out_mut_, len(v.aval.lo_ty_qdd(qdd)))
      v.aval.update_from_loval(qdd, hi_args[in_idx[v]], *lo_vals)

  assert next(out_mut_, None) is None

  # collect output values into hi types
  lo_outs_ = iter(lo_outs)
  hi_outs = [t.raise_val(*it.islice(lo_outs_, len(t.lo_ty())))
             for t in jaxpr.out_avals]
  assert next(lo_outs_, None) is None

  return hi_outs
scan_p.to_lojax = _to_lojax

def _move_right(lst, to_move):
  lst, rest = split_list(lst, [len(to_move)])
  left, right = partition_list(to_move, lst)
  return [*left, *right, *rest]

### while_loop

@api_boundary
def while_loop(cond_fun: Callable[[T], BooleanNumeric],
               body_fun: Callable[[T], T],
               init_val: T) -> T:
  """Call ``body_fun`` repeatedly in a loop while ``cond_fun`` is True.

  The `Haskell-like type signature`_ in brief is

  .. code-block:: haskell

    while_loop :: (a -> Bool) -> (a -> a) -> a -> a

  The semantics of ``while_loop`` are given by this Python implementation::

    def while_loop(cond_fun, body_fun, init_val):
      val = init_val
      while cond_fun(val):
        val = body_fun(val)
      return val

  Unlike that Python version, ``while_loop`` is a JAX primitive and is lowered
  to a single WhileOp. That makes it useful for reducing compilation times
  for jit-compiled functions, since native Python loop constructs in an ``@jit``
  function are unrolled, leading to large XLA computations.

  Also unlike the Python analogue, the loop-carried value ``val`` must hold a
  fixed shape and dtype across all iterations (and not just be consistent up to
  NumPy rank/shape broadcasting and dtype promotion rules, for example). In
  other words, the type ``a`` in the type signature above represents an array
  with a fixed shape and dtype (or a nested tuple/list/dict container data
  structure with a fixed structure and arrays with fixed shape and dtype at the
  leaves).

  Another difference from using Python-native loop constructs is that
  ``while_loop`` is not reverse-mode differentiable because XLA computations
  require static bounds on memory requirements.

  .. note::
    :py:func:`while_loop` compiles ``cond_fun`` and ``body_fun``, so while it
    can be combined with :py:func:`jit`, it's usually unnecessary.

  Args:
    cond_fun: function of type ``a -> Bool``.
    body_fun: function of type ``a -> a``.
    init_val: value of type ``a``, a type that can be a scalar, array, or any
      pytree (nested Python tuple/list/dict) thereof, representing the initial
      loop carry value.

  Returns:
    The output from the final iteration of body_fun, of type ``a``.

  .. _Haskell-like type signature: https://wiki.haskell.org/Type_signature
  """
  if not (callable(body_fun) and callable(cond_fun)):
    raise TypeError("lax.while_loop: body_fun and cond_fun arguments should be callable.")
  if config.disable_jit.value:
    try:
      val = tree_map(lax.asarray, init_val)
      while cond_fun(val):
        val = tree_map(lax.asarray, body_fun(val))
      return val
    except core.ConcretizationTypeError:
      # Can't run this while_loop in Python (e.g. because there's a vmap
      # transformation on it), so we fall back to the primitive version.
      pass

  def _create_jaxpr(init_val):
    init_vals, in_tree = tree_flatten((init_val,))
    init_avals = tuple(_map(core.get_aval, init_vals))
    cond_dbg = api_util.debug_info("while_cond", cond_fun, (init_val,), {})
    cond_jaxpr, cond_consts, cond_tree = _initial_style_jaxpr(
        cond_fun, in_tree, init_avals, cond_dbg)
    body_dbg = api_util.debug_info("while_body", body_fun, (init_val,), {})
    body_jaxpr, body_consts, body_tree = _initial_style_jaxpr(
        body_fun, in_tree, init_avals, body_dbg)
    if not treedef_is_leaf(cond_tree) or len(cond_jaxpr.out_avals) != 1:
      msg = "cond_fun must return a boolean scalar, but got pytree {}."
      raise TypeError(msg.format(cond_tree))
    pred_aval = cond_jaxpr.out_avals[0]
    if (not isinstance(pred_aval, ShapedArray)
        or ShapedArray(pred_aval.shape, pred_aval.dtype) != ShapedArray((), np.bool_)):
      msg = "cond_fun must return a boolean scalar, but got output type(s) {}."
      raise TypeError(msg.format(cond_jaxpr.out_avals))
    return init_vals, init_avals, body_jaxpr, in_tree, cond_jaxpr, cond_consts, body_consts, body_tree

  # The body input and output avals must match exactly. However, we want to account for
  # the case when init contains weakly-typed values (e.g. Python scalars), with avals that
  # may not match the output despite being compatible by virtue of their weak type.
  # To do this, we compute the jaxpr in two passes: first with the raw inputs, and if
  # necessary, a second time with modified init values.
  init_vals, init_avals, body_jaxpr, in_tree, *rest = _create_jaxpr(init_val)
  new_init_vals, changed = _promote_weak_typed_inputs(init_vals, init_avals, body_jaxpr.out_avals)
  new_init_val, = tree_unflatten(in_tree, new_init_vals)
  if changed:
    init_vals, init_avals, body_jaxpr, in_tree, *rest = _create_jaxpr(new_init_val)
  cond_jaxpr, cond_consts, body_consts, body_tree = rest

  in_tree_children = in_tree.children()
  assert len(in_tree_children) == 1
  _check_carry_type('while_loop body', body_fun, new_init_val, body_tree,
                    body_jaxpr.out_avals)
  joined_effects = core.join_effects(cond_jaxpr.effects, body_jaxpr.effects)
  disallowed_effects = effects.control_flow_allowed_effects.filter_not_in(joined_effects)
  if disallowed_effects:
    raise NotImplementedError(
        f'Effects not supported in `while`: {disallowed_effects}')

  # If the body forwards an input carry to an output carry, *and* it's not used
  # by the cond fun, it can be moved to be a body const. Doing so can lead to
  # efficiency wins: if e.g. we vmap the loop with a batched predicate, we batch
  # the carry too, but not the body consts.
  body_fwd = pe._jaxpr_forwarding(body_jaxpr.jaxpr)
  carry_nofwd = [len(body_consts) + i != f for i, f in enumerate(body_fwd)]
  cond_jaxpr_, keep_cond = pe.dce_jaxpr(
      cond_jaxpr.jaxpr, [True], [True] * len(cond_consts) + carry_nofwd)
  _, keep_cond_carry = split_list(keep_cond, [len(cond_consts)])
  move_to_const = _map(operator.not_, keep_cond_carry)

  if any(move_to_const):
    cond_jaxpr = pe.close_jaxpr(cond_jaxpr_)
    body_jaxpr = pe.prune_closed_jaxpr_outputs(
        body_jaxpr, [not m for m in move_to_const])
    body_jaxpr = pe.move_binders_to_front(
        body_jaxpr, [False] * len(body_consts) + move_to_const)
    init_vals, new_body_consts = partition_list(move_to_const, init_vals)
    body_consts = [*new_body_consts, *body_consts]

  outs = while_p.bind(*cond_consts, *body_consts, *init_vals,
                      cond_nconsts=len(cond_consts), cond_jaxpr=cond_jaxpr,
                      body_nconsts=len(body_consts), body_jaxpr=body_jaxpr)

  if any(move_to_const):
    outs = pe.merge_lists(move_to_const, outs, new_body_consts)

  return tree_unflatten(body_tree, outs)


def _join_while_effects(body_jaxpr, cond_jaxpr, body_nconsts, cond_nconsts
                       ) -> effects.Effects:
  joined_effects = set()
  for eff in cond_jaxpr.effects:
    if isinstance(eff, effects.JaxprInputEffect):
      index = eff.input_index
      if index >= cond_nconsts:
        index += body_nconsts
      eff = eff.replace(input_index=index)
    joined_effects.add(eff)
  for eff in body_jaxpr.effects:
    if isinstance(eff, effects.JaxprInputEffect):
      index = eff.input_index + cond_nconsts
      eff = eff.replace(input_index=index)
    joined_effects.add(eff)
  return joined_effects

def _while_loop_abstract_eval(*avals, cond_jaxpr, body_jaxpr, body_nconsts,
                              cond_nconsts):
  cond_consts_avals, body_consts_avals, in_avals = \
      util.split_list(avals, [cond_nconsts, body_nconsts])

  if len(cond_jaxpr.in_avals) != len(cond_consts_avals) + len(in_avals):
    raise core.JaxprTypeError(
        f"while_loop {len(cond_jaxpr.in_avals)=} but {len(cond_consts_avals) + len(in_avals)=}")
  if len(body_jaxpr.in_avals) != len(body_consts_avals) + len(in_avals):
    raise core.JaxprTypeError(
        f"while_loop {len(body_jaxpr.in_avals)=} but {len(body_consts_avals) + len(in_avals)=}")
  # TODO(mattjj): check body carry type
  # TODO(mattjj): make these typecompat checks work with bints
  # if not all(_map(core.typecompat, [*cond_consts_avals, *in_avals], cond_jaxpr.in_avals)):  # type: ignore
  #   cond_avals = [*cond_consts_avals, *in_avals]
  #   a1, a2 = next((a1, a2) for a1, a2 in zip(cond_avals, cond_jaxpr.in_avals)
  #                 if not core.typecompat(a1, a2))
  #   raise core.JaxprTypeError(f"while_loop cond function input type error: {a1} != {a2}")
  # if not all(_map(core.typecompat, [*body_consts_avals, *in_avals], body_jaxpr.in_avals)):  # type: ignore
  #   body_avals = [*body_consts_avals, *in_avals]
  #   a1, a2 = next((a1, a2) for a1, a2 in zip(body_avals, body_jaxpr.in_avals)
  #                 if not core.typecompat(a1, a2))
  #   raise core.JaxprTypeError(f"while_loop body function input type error: {a1} != {a2}")


  joined_effects = _join_while_effects(body_jaxpr, cond_jaxpr, body_nconsts,
                                       cond_nconsts)
  disallowed_effects = effects.control_flow_allowed_effects.filter_not_in(joined_effects)
  if disallowed_effects:
    raise NotImplementedError(
        f'Effects not supported in `while`: {disallowed_effects}')
  return body_jaxpr.out_avals, joined_effects


def _while_loop_batching_rule(axis_data, args, dims, cond_nconsts, cond_jaxpr,
                              body_nconsts, body_jaxpr):
  from jax._src.callback import _IOEffect, _OrderedIOEffect
  if any(_OrderedIOEffect in fn.effects for fn in [body_jaxpr, cond_jaxpr]):
    raise Exception("Ordered IO effects not supported in vmap.")

  orig_batched = [d is not batching.not_mapped for d in dims]
  cconst_bat, bconst_bat, init_bat = split_list(orig_batched, [cond_nconsts, body_nconsts])
  cconsts, bconsts, init = split_list(args, [cond_nconsts, body_nconsts])
  cconst_dims, bconst_dims, init_dims = split_list(dims, [cond_nconsts, body_nconsts])

  carry_bat = init_bat
  # Fixpoint computation of which carry are batched: either
  # batched from init, or the carry out is batched. Each iteration promotes
  # at least one carry to batched. We need at most len(carry) iterations to
  # reach a fixpoint.
  for _ in range(1 + len(carry_bat)):
    _, carry_bat_out = batching.batch_jaxpr(
        body_jaxpr, axis_data, bconst_bat + carry_bat, instantiate=carry_bat)
    if carry_bat == carry_bat_out:
      break
    carry_bat = safe_map(operator.or_, carry_bat, carry_bat_out)
  else:
    assert False, "Fixpoint not reached"

  # Knowing how the carry is batched now, we can determine if the predicate is
  # batched.
  _, (pred_bat,) = batching.batch_jaxpr(
      cond_jaxpr, axis_data, cconst_bat + carry_bat, instantiate=False)

  if pred_bat:
    # If the predicate is batched, we have to batch *all* of the carry
    # regardless of if the body needs it.
    if any(_IOEffect in fn.effects for fn in [body_jaxpr, cond_jaxpr]):
      raise Exception("Unordered IO effects not supported in while_loop "
                      "with batched predicate")
    carry_bat = [True] * len(carry_bat)
    carry_dims = [0] * len(carry_bat)
    body_jaxpr_batched, _ = batching.batch_jaxpr_axes(
        body_jaxpr, axis_data, bconst_dims + carry_dims, carry_dims)
    cond_jaxpr_batched, _ = batching.batch_jaxpr_axes(
        cond_jaxpr, axis_data, cconst_dims + carry_dims, [0])
  else:
    # If the predicate is not batched, we can look at the `cond_jaxpr`'s out
    # shape to determine the rank of the predicate. From this rank we pick the
    # dims of the carry to be batched to ensure that the predicate shape is a
    # prefix of the carry in and out shapes. We can then batch the `body_jaxpr`
    # according to these new batch dims.
    cond_rank = len(cond_jaxpr.out_avals[0].shape)
    carry_dims = [cond_rank if b else None for b in carry_bat]
    body_jaxpr_batched, _ = batching.batch_jaxpr_axes(
        body_jaxpr, axis_data, bconst_dims + carry_dims, carry_dims)
    # Now we need to rebatch the `cond_jaxpr` according to the new dims of the
    # carry.
    cond_jaxpr_batched, _ = batching.batch_jaxpr_axes(
        cond_jaxpr, axis_data, cconst_dims + carry_dims, (None,))

  # To prepare the `init` to the `while_p`, we broadcast values if they are
  # unbatched and need to have an out axis. If their current batch axis does not
  # match the one it needs to be for the translation rule to work, we move it
  # into place.
  new_init = []
  for x, old_axis, new_axis in zip(init, init_dims, carry_dims):
    if old_axis is batching.not_mapped and new_axis is not batching.not_mapped:
      new_init.append(batching.broadcast(x, axis_data.size, new_axis,
                                         axis_data.explicit_mesh_axis))
    elif old_axis is batching.not_mapped and new_axis is batching.not_mapped:
      new_init.append(x)
    else:
      assert new_axis is not batching.not_mapped
      new_init.append(batching.moveaxis(x, old_axis, new_axis))

  outs = while_p.bind(*(cconsts + bconsts + new_init),
                      cond_nconsts=cond_nconsts, cond_jaxpr=cond_jaxpr_batched,
                      body_nconsts=body_nconsts, body_jaxpr=body_jaxpr_batched)
  return outs, carry_dims

def _while_loop_jvp(primals, tangents, cond_nconsts, cond_jaxpr, body_nconsts,
                    body_jaxpr):
  nonzeros = [type(t) is not ad_util.Zero for t in tangents]
  cconst_nz, bconst_nz, init_nz = split_list(nonzeros, [cond_nconsts, body_nconsts])

  carry_nz = init_nz
  for _ in range(1 + len(carry_nz)):
    body_nonzeros = bconst_nz + carry_nz
    body_jvp, nonzeros_out = ad.jvp_jaxpr(
        body_jaxpr, body_nonzeros, instantiate=carry_nz)
    if nonzeros_out == carry_nz:
      break
    carry_nz = _map(operator.or_, carry_nz, nonzeros_out)
  else:
    assert False, "Fixpoint not reached"

  nonzeros = cconst_nz + body_nonzeros
  tangents = [ad.instantiate_zeros(t) if nz else t
              for t, nz in zip(tangents, nonzeros)]

  cconst, bconst, init = split_list(primals, [cond_nconsts, body_nconsts])
  _, bconst_dot, init_dot = split_list(tangents, [cond_nconsts, body_nconsts])
  bconst_dot = _prune_zeros(bconst_dot)
  init_dot = _prune_zeros(init_dot)

  num_carry = len(primals) - cond_nconsts - body_nconsts

  body_jvp_rearranged = ad.rearrange_binders(
      body_jvp,
      [body_nconsts, num_carry], [len(bconst_dot), len(init_dot)],
      [num_carry], [len(init_dot)])

  newvar = core.gensym()
  invars_aug = (
      cond_jaxpr.jaxpr.invars + [newvar(core.get_aval(x)) for x in init_dot])
  cond_debug = cond_jaxpr.jaxpr.debug_info
  augmented_debug = cond_debug and (
      cond_debug._replace(
          arg_names=cond_debug.arg_names + ("",) * len(init_dot)
      )
  )
  cond_jaxpr_augmented = core.Jaxpr(cond_jaxpr.jaxpr.constvars,
                                    invars_aug,
                                    cond_jaxpr.jaxpr.outvars,
                                    cond_jaxpr.jaxpr.eqns,
                                    cond_jaxpr.jaxpr.effects,
                                    augmented_debug)
  cond_jaxpr_augmented = ClosedJaxpr(cond_jaxpr_augmented, cond_jaxpr.consts)

  out = while_p.bind(
      *(cconst + bconst + bconst_dot + init + init_dot),
      cond_nconsts=cond_nconsts,
      cond_jaxpr=cond_jaxpr_augmented,
      body_nconsts=len(bconst) + len(bconst_dot),
      body_jaxpr=body_jvp_rearranged)

  out_carry, out_carry_dot = split_list(out, [num_carry])
  out_tangents_iter = iter(out_carry_dot)
  out_tangents = [next(out_tangents_iter) if nz else ad_util.Zero.from_primal_value(p)
                  for p, nz in zip(out_carry, nonzeros_out)]
  return out_carry, out_tangents

def _while_partial_eval(trace: pe.JaxprTrace, *tracers: pe.Tracer, cond_nconsts: int,
                        cond_jaxpr: pe.ClosedJaxpr, body_nconsts: int,
                        body_jaxpr: pe.ClosedJaxpr) -> Sequence[pe.Tracer]:
  # As long as some carry (and hence output) are known and the output of
  # `cond_jaxpr` is known, we use a portion of the loop body to compute the
  # known outputs of the `while_loop`. For the unknown outputs we generate a
  # jaxpr to run the whole while, including recomputing the known parts,
  # basically like building in checkpointing/rematieralization. This means that
  # we don't actually save any computation by partial evaluation if there are
  # unknown outputs.
  #
  # What this achieves is twofold: jax.linearize works, and we can give a proper
  # error for reverse differentiation of `while`.

  unknowns = [not t.pval.is_known() for t in tracers]
  params = dict(cond_nconsts=cond_nconsts, cond_jaxpr=cond_jaxpr,
                body_nconsts=body_nconsts, body_jaxpr=body_jaxpr)

  cond_consts_uk, body_consts_uk, carry_init_uk = \
      split_list(unknowns, [cond_nconsts, body_nconsts])

  # Fixpoint computation of unknown carry. Each iteration promotes at least one
  # carry to unknown. We need one last iteration to prepare the jaxpr.
  carry_uk = carry_init_uk
  for _ in range(1 + len(carry_uk)):
    body_jaxpr_known, _, carry_out_uk, body_res_avals = pe.partial_eval_jaxpr_nounits(
        body_jaxpr, body_consts_uk + carry_uk, instantiate=carry_uk)
    if carry_out_uk == carry_uk:
      break
    else:
      carry_uk = _map(operator.or_, carry_uk, carry_out_uk)
  else:
    assert False, "Fixpoint not reached"

  cond_jaxpr_known, _, cond_uk, _ = pe.partial_eval_jaxpr_nounits(
      cond_jaxpr, cond_consts_uk + carry_uk, instantiate=False)

  if cond_uk[0] or all(not uk for uk in unknowns) or all(unknowns):
    # If conditional is unknown, or all inputs are known, or all are unknown,
    # just do the default processing.
    return trace.default_process_primitive(while_p, tracers, params)

  # Run the known part of the while.
  in_consts = [t.pval.get_known() for uk, t in
               zip(cond_consts_uk + body_consts_uk + carry_uk, tracers)
               if not uk]
  cond_nconsts_known = len(cond_consts_uk) - sum(cond_consts_uk)
  body_nconsts_known = len(body_consts_uk) - sum(body_consts_uk)
  num_known_outs = len(carry_uk) - sum(carry_uk)
  # TODO(mattjj): use pe.dce_jaxpr to drop res computations and not just outputs
  body_jaxpr_known = body_jaxpr_known.replace(
    jaxpr=body_jaxpr_known.jaxpr.replace(
      outvars=body_jaxpr_known.jaxpr.outvars[:num_known_outs]))
  out_known = while_p.bind(
      *in_consts, cond_nconsts=cond_nconsts_known, cond_jaxpr=cond_jaxpr_known,
      body_nconsts=body_nconsts_known, body_jaxpr=body_jaxpr_known)
  del body_jaxpr_known

  # Run the whole while_loop to get all the outputs, then merge with known ones
  out_tracers_ = trace.default_process_primitive(while_p, tracers, params)
  out_tracers = [t for t, uk in zip(out_tracers_, carry_uk) if uk]
  return util.merge_lists(carry_uk, out_known, out_tracers)

# TODO(mattjj): de-duplicate code with _while_partial_eval
def _while_partial_eval_custom(saveable, unks_in, inst_in, eqn):
  del saveable  # We can't save any residuals anyway (w/o dynamic shapes)!
  cond_jaxpr = eqn.params['cond_jaxpr']
  cond_nconsts = eqn.params['cond_nconsts']
  body_jaxpr = eqn.params['body_jaxpr']
  body_nconsts = eqn.params['body_nconsts']

  cond_consts_uk, body_consts_uk, carry_init_uk = \
      split_list(unks_in, [cond_nconsts, body_nconsts])

  # Fixpoint to compute known part of the body (trivial on 'inst_in', since we
  # make all inputs available as DCE can subsequently prune any unused ones)
  carry_uk = carry_init_uk
  for _ in range(1 + len(carry_uk)):
    body_unks_in = body_consts_uk + carry_uk
    jaxpr_known_, _, carry_uk_out, _, num_res = \
        pe.partial_eval_jaxpr_custom(
            body_jaxpr.jaxpr, in_unknowns=body_unks_in, in_inst=True,
            ensure_out_unknowns=carry_uk, ensure_out_inst=True,
            saveable=ad_checkpoint.nothing_saveable)
    if carry_uk_out == carry_uk:
      break
    else:
      carry_uk = _map(operator.or_, carry_uk, carry_uk_out)
  else:
    assert False, "Fixpoint not reached"
  assert not num_res
  body_jaxpr_known = ClosedJaxpr(jaxpr_known_, body_jaxpr.consts)
  del jaxpr_known_, carry_uk_out, num_res, unks_in

  # Instantiate all inputs (b/c jaxpr_staged will take all inputs).
  new_inst = [x for x, inst in zip(eqn.invars, inst_in)
              if type(x) is core.Var and not inst]

  # Compute the known part of cond_fun (basically pruning inputs on known side).
  cond_unks_in = cond_consts_uk + carry_uk
  cond_jaxpr_known_, _, [cond_uk], _, _ = \
      pe.partial_eval_jaxpr_custom(
          cond_jaxpr.jaxpr, cond_unks_in, in_inst=True,
          ensure_out_unknowns=False, ensure_out_inst=True,
          saveable=ad_checkpoint.nothing_saveable)
  # NOTE(mattjj): I think it should be impossible for the condition to be
  # unknown, but asserting that caused a test failure in diffrax. So
  # we handle it: if it is unknown, stage out the whole cond function.
  if cond_uk:
    return None, eqn, [True] * len(carry_uk), [True] * len(carry_uk), new_inst
  cond_jaxpr_known = ClosedJaxpr(cond_jaxpr_known_, cond_jaxpr.consts)
  del cond_uk

  # Build the known eqn.
  unks_in = [*cond_consts_uk, *body_consts_uk, *carry_uk]  # fixpoint carry_uk
  ins_known, _ = partition_list(unks_in, eqn.invars)
  out_binders_known, _ = partition_list(carry_uk, eqn.outvars)
  params_known = dict(cond_jaxpr=cond_jaxpr_known, body_jaxpr=body_jaxpr_known,
                      cond_nconsts=len(cond_consts_uk) - sum(cond_consts_uk),
                      body_nconsts=len(body_consts_uk) - sum(body_consts_uk))
  effects_known = core.join_effects(cond_jaxpr_known.effects,
                                    body_jaxpr_known.effects)
  eqn_known = pe.new_jaxpr_eqn(ins_known, out_binders_known, while_p,
                               params_known, effects_known, eqn.source_info,
                               eqn.ctx)
  # Typecheck known eqn.
  _while_loop_abstract_eval(
      *[v.aval for v in eqn_known.invars], cond_jaxpr=cond_jaxpr_known,
      body_jaxpr=body_jaxpr_known, body_nconsts=params_known['body_nconsts'],
      cond_nconsts=params_known['cond_nconsts'])

  # Staged eqn is same as input eqn.
  eqn_staged = eqn

  unks_out = carry_uk
  inst_out = [True] * len(unks_out)
  return eqn_known, eqn_staged, unks_out, inst_out, new_inst

def _while_transpose_error(*_, **kwargs):
  raise ValueError("Reverse-mode differentiation does not work for "
                   "lax.while_loop or lax.fori_loop with dynamic start/stop values. "
                   "Try using lax.scan, or using fori_loop with static start/stop.")

# For a while loop with ordered effects in the cond, we need a special
# lowering. Fundamentally, we'd like to rewrite a while loop that looks like
# this:
# ```
# while cond(x):
#   x = body(x)
# ```
# into something that looks like this:
# ```
# while True:
#   token, pred = cond(token, x)
#   if not pred:
#     break
#   token, x = body(token, x)
# ```
# Unfortunately, with a WhileOp we can't (1) return multiple values
# from a `cond` and (2) can't break a while loop. We thus adopt the
# following rewrite strategy:
# ```
# def new_cond(pred, token, x):
#   return pred
# token, pred = cond(token, x)
# while new_cond(pred, token, x):
#   token, x = body(token, x)
#   token, pred = cond(token, x)
# ```
def _while_lowering(ctx, *args, cond_jaxpr, body_jaxpr, cond_nconsts,
                    body_nconsts):
  pred_aval = cond_jaxpr.out_avals[0]
  batched = bool(pred_aval.shape)
  cond_ordered_effects = effects.ordered_effects.filter_in(cond_jaxpr.effects)
  if cond_ordered_effects:
    def cond(args):
      # Pred can be batched
      pred = core.eval_jaxpr(cond_jaxpr.jaxpr, cond_jaxpr.consts, *args)[0]
      if batched:
        pred = lax.reduce_or(pred, tuple(range(len(pred_aval.shape))))
      return pred
    def body(args):
      return core.eval_jaxpr(body_jaxpr.jaxpr, body_jaxpr.consts, *args)
    def new_cond(pred_args):
      pred, *_ = pred_args
      return pred
    def new_body(pred_args):
      _, cond_consts, body_consts, carry = pred_args
      carry = body((*body_consts, *carry))
      pred = cond((*cond_consts, *carry))
      return pred, cond_consts, body_consts, carry
    def fun(*args):
      cond_consts, body_consts, carry = split_list(args, [cond_nconsts, body_nconsts])
      pred = cond((*cond_consts, *carry))
      *_, out = while_loop(new_cond, new_body, (pred, cond_consts, body_consts, carry))
      return out
    return mlir.lower_fun(fun)(ctx, *args)

  loop_carry_types = _map(mlir.aval_to_ir_type, ctx.avals_in)
  body_effects = effects.ordered_effects.filter_in(body_jaxpr.effects)
  num_tokens = len(body_effects)
  tokens = [ctx.tokens_in.get(eff) for eff in body_effects]
  token_types = [mlir.token_type() for _ in tokens]
  loop_carry_types = [*token_types, *loop_carry_types]
  flat_loop_carry_types = mlir.flatten_ir_types(loop_carry_types)
  args = [*tokens, *args]

  flat_args = mlir.flatten_ir_values(args)
  while_op = hlo.WhileOp(flat_loop_carry_types, flat_args)

  # Loop condition
  cond_block = while_op.regions[0].blocks.append(*flat_loop_carry_types)
  name_stack = ctx.name_stack.extend('while')
  with ir.InsertionPoint(cond_block):
    flat_cond_args = [
        cond_block.arguments[i] for i in range(len(flat_loop_carry_types))
    ]
    cond_args = mlir.unflatten_ir_values_like_types(flat_cond_args, loop_carry_types)
    cond_args = cond_args[num_tokens:]  # Remove tokens from cond args
    x, _, z = util.split_list(cond_args, [cond_nconsts, body_nconsts])
    cond_consts = [
        mlir.ir_constant(xla.canonicalize_dtype(x)) for x in cond_jaxpr.consts
    ]
    cond_name_stack = name_stack.extend('cond')
    (pred,), _ = mlir.jaxpr_subcomp(
        ctx.module_context,
        cond_jaxpr.jaxpr,
        cond_name_stack,
        mlir.TokenSet(),
        cond_consts,
        *(x + z),
        dim_var_values=ctx.dim_var_values,
        const_lowering=ctx.const_lowering,
    )
    if batched:
      pred_ctx = mlir.LoweringRuleContext(
          module_context=ctx.module_context,
          name_stack=cond_name_stack,
          traceback=ctx.traceback,
          primitive=None,
          avals_in=[pred_aval],
          avals_out=[pred_aval.update(
              shape=(), sharding=pred_aval.sharding.update(spec=()))],
          tokens_in=mlir.TokenSet(),
          tokens_out=None,
          dim_var_values=ctx.dim_var_values,
          const_lowering=ctx.const_lowering)
      pred, = lax._unary_reduce_lower(
          hlo.OrOp,
          lambda dtype: np.array(False, dtype),
          pred_ctx,
          pred,
          axes=tuple(range(len(pred_aval.shape))))
    hlo.return_([pred])

  # Loop body
  body_block = while_op.regions[1].blocks.append(*flat_loop_carry_types)
  with ir.InsertionPoint(body_block):
    flat_body_args = [
        body_block.arguments[i] for i in range(len(flat_loop_carry_types))
    ]
    body_args = mlir.unflatten_ir_values_like_types(flat_body_args, loop_carry_types)
    # Tokens are at the front of the args list to the while loop
    token_args, body_args = util.split_list(body_args, [num_tokens])
    tokens_in = mlir.TokenSet(zip(body_effects, token_args))
    x, y, z = util.split_list(body_args, [cond_nconsts, body_nconsts])
    body_name_stack = name_stack.extend('body')
    body_consts = [mlir.ir_constant(xla.canonicalize_dtype(x))
                   for x in body_jaxpr.consts]
    new_z, tokens_out = mlir.jaxpr_subcomp(
        ctx.module_context, body_jaxpr.jaxpr, body_name_stack,
        tokens_in, body_consts, *(y + z),
        dim_var_values=ctx.dim_var_values, const_lowering=ctx.const_lowering)
    out_tokens = [tokens_out.get(eff) for eff in body_effects]
    if batched:
      body_pred_name_stack = name_stack.extend('body_pred')
      cond_consts = [mlir.ir_constant(xla.canonicalize_dtype(x))
                     for x in cond_jaxpr.consts]
      (body_pred,), _ = mlir.jaxpr_subcomp(
          ctx.module_context, cond_jaxpr.jaxpr, body_pred_name_stack,
          mlir.TokenSet(), cond_consts, *(x + z),
          dim_var_values=ctx.dim_var_values, const_lowering=ctx.const_lowering)
      new_z = _map(
          partial(_pred_bcast_select_hlo, ctx, pred_aval, body_pred), new_z, z,
          body_jaxpr.out_avals)

    hlo.return_([*mlir.flatten_ir_values(out_tokens),
                 *mlir.flatten_ir_values(x), *mlir.flatten_ir_values(y),
                 *mlir.flatten_ir_values(new_z)])

  outputs = mlir.unflatten_ir_values_like_types(while_op.results, loop_carry_types)
  tokens, _, _, z = util.split_list(outputs, [num_tokens, cond_nconsts, body_nconsts])
  if tokens:
    ctx.set_tokens_out(mlir.TokenSet(zip(body_effects, tokens)))
  return z

def _while_typecheck(_, *in_atoms, cond_jaxpr, body_jaxpr, cond_nconsts,
                     body_nconsts):
  # TODO(frostig,mattjj): check cond_jaxpr, body_jaxpr types
  joined_effects = _join_while_effects(body_jaxpr, cond_jaxpr, body_nconsts,
                                       cond_nconsts)
  disallowed_effects = effects.control_flow_allowed_effects.filter_not_in(joined_effects)
  if disallowed_effects:
    raise NotImplementedError(
        f'Effects not supported in `while`: {disallowed_effects}')
  return body_jaxpr.out_avals, joined_effects

def _while_partial_discharge_rule(should_discharge, in_avals, out_avals, *args, cond_jaxpr, body_jaxpr,
                          cond_nconsts, body_nconsts):
  # TODO(sharadmv): enable supporting state effects in the cond
  if any(isinstance(eff, state.RefEffect) for eff in cond_jaxpr.effects):
    raise NotImplementedError
  cond_consts_discharge, body_consts_discharge, carry_discharge = split_list(
      should_discharge, [cond_nconsts, body_nconsts])

  if any(cond_consts_discharge):
    raise NotImplementedError
  cond_consts, body_consts, carry = split_list(args, [cond_nconsts, body_nconsts])
  cond_consts_avals, body_consts_avals, carry_avals = split_list(in_avals,
                                                                 [cond_nconsts,
                                                                  body_nconsts])
  # There shouldn't be any `Ref`s in the `cond` (because of our check above).
  assert not any(isinstance(aval, state.AbstractRef) for aval in cond_consts_avals)
  is_ref = [
      isinstance(aval, state.AbstractRef) and should
      for aval, should in zip(body_consts_avals, body_consts_discharge)
  ]
  remaining_body_consts, refs = partition_list(is_ref, body_consts)
  remaining_body_const_avals, ref_avals = partition_list(is_ref,
                                                         body_consts_avals)
  num_refs = sum(is_ref)
  num_remaining_consts = body_nconsts - num_refs
  num_carry = len(in_avals) - body_nconsts - cond_nconsts
  body_jaxpr, body_jaxpr_consts = body_jaxpr.jaxpr, body_jaxpr.consts
  cond_jaxpr, cond_jaxpr_consts = cond_jaxpr.jaxpr, cond_jaxpr.consts
  if body_jaxpr_consts:
    raise NotImplementedError("Body jaxpr has consts. If you see this error, "
                              "please open an issue at "
                              "https://github.com/jax-ml/jax/issues")
  # body_jaxpr has the signature (*body_consts, *carry) -> carry.
  # Some of these body_consts are actually `Ref`s so when we discharge
  # them, they also turn into outputs, effectively turning those consts into
  # carries. However this doesn't fit the expected signature for the body_jaxpr.
  # Therefore we need to rewrite the jaxpr to shuffle around the `Ref`s so that
  # they are part of the carry.
  discharged_body_jaxpr, discharged_consts = state_discharge.discharge_state(
      body_jaxpr, (), should_discharge=[*body_consts_discharge, *carry_discharge])
  if discharged_consts: raise NotImplementedError

  def new_body(*consts_refs_carry):
    consts, refs, carry = split_list(
        consts_refs_carry, [num_remaining_consts, num_refs])
    consts_and_refs = merge_lists(is_ref, consts, refs)
    carry_refs = core.eval_jaxpr(discharged_body_jaxpr, (), *consts_and_refs,
                                 *carry)
    carry, refs_out = split_list(carry_refs, [num_carry])
    return [*refs_out, *carry]
  new_body_jaxpr, _, new_body_consts = pe.trace_to_jaxpr_dynamic(
      lu.wrap_init(new_body, debug_info=discharged_body_jaxpr.debug_info),
      [*remaining_body_const_avals, *[a.inner_aval for a in ref_avals],
      *carry_avals])
  if new_body_consts: raise NotImplementedError

  # Since some `Ref`s that were previously consts are now carries, we need to
  # deal with them (i.e. ignore them) in the `cond`, so we need to rewrite the
  # cond_jaxpr as well.
  def new_cond(*consts_refs_carry):
    consts, refs, carry = split_list(
        consts_refs_carry, [cond_nconsts, num_refs])
    del refs  # We don't use them here!
    return core.eval_jaxpr(cond_jaxpr, cond_jaxpr_consts, *consts, *carry)
  new_cond_jaxpr, _, new_cond_consts = pe.trace_to_jaxpr_dynamic(
      lu.wrap_init(new_cond, debug_info=cond_jaxpr.debug_info),
      [*cond_consts_avals, *[a.inner_aval for a in ref_avals], *carry_avals])
  if new_cond_consts: raise NotImplementedError

  out = while_p.bind(*cond_consts, *remaining_body_consts, *refs, *carry,
                     body_jaxpr=ClosedJaxpr(new_body_jaxpr, ()),
                     cond_jaxpr=ClosedJaxpr(new_cond_jaxpr, ()),
                     body_nconsts=num_remaining_consts,
                     cond_nconsts=cond_nconsts)
  refs_out, carry_out = split_list(out, [num_refs])
  updated_body_consts = merge_lists(is_ref, [None] * num_remaining_consts,
                                    refs_out)
  invals_out = [
      *[None] * cond_nconsts,
      *updated_body_consts,
      *[None] * num_carry]
  return invals_out, carry_out

while_p = core.Primitive('while')
while_p.multiple_results = True
while_p.skip_canonicalization = True
while_p.def_impl(partial(dispatch.apply_primitive, while_p))
while_p.def_effectful_abstract_eval(_while_loop_abstract_eval)
ad.primitive_jvps[while_p] = _while_loop_jvp
pe.custom_partial_eval_rules[while_p] = _while_partial_eval
xla.register_initial_style_primitive(while_p)
ad.primitive_transposes[while_p] = _while_transpose_error
batching.fancy_primitive_batchers[while_p] = _while_loop_batching_rule
pe.partial_eval_jaxpr_custom_rules[while_p] = _while_partial_eval_custom
core.custom_typechecks[while_p] = _while_typecheck
mlir.register_lowering(while_p, _while_lowering)
state_discharge.register_partial_discharge_rule(while_p)(_while_partial_discharge_rule)


def _pred_bcast_select_hlo(ctx,
    pred_aval: core.ShapedArray, pred: ir.Value, x: mlir.IrValues,
    y: mlir.IrValues, x_y_aval: core.AbstractValue) -> Sequence[ir.Value]:
  if x_y_aval is core.abstract_token:
    return [hlo.AfterAllOp([x, y]).result]
  else:
    assert isinstance(x, ir.Value), x
    assert isinstance(y, ir.Value), y
    assert isinstance(x_y_aval, core.ShapedArray), x_y_aval
    assert x.type == y.type, (x.type, y.type)
    assert (pred_aval.shape == x_y_aval.shape[:len(pred_aval.shape)]), (
            pred_aval.shape, x_y_aval)
    x_y_aval = core.physical_aval(x_y_aval)
    bcast_pred = mlir.broadcast_in_dim(
        ctx, pred, core.DShapedArray(x_y_aval.shape, np.dtype(np.bool_)),
        broadcast_dimensions=list(range(len(pred_aval.shape))))
    return hlo.SelectOp(bcast_pred, x, y).results

### fori_loop

def _fori_cond_fun(loop_carry):
  i, upper, _ = loop_carry
  return lax.lt(i, upper)

@weakref_lru_cache
def _fori_body_fun(body_fun: Callable, body_fun_dbg: core.DebugInfo) -> Callable:
  body_fun_ref = weakref.ref(body_fun)

  def while_body_fun(loop_carry):
    i, upper, x = loop_carry
    return lax.add(i, lax._const(i, 1)), upper, body_fun_ref()(i, x)
  api_util.save_wrapped_fun_debug_info(
      while_body_fun,
      body_fun_dbg._replace(arg_names=(body_fun_dbg.arg_names[0],
                                       "",  # upper,
                                       * body_fun_dbg.arg_names[1:])))
  return while_body_fun

@weakref_lru_cache
def _fori_scan_body_fun(body_fun: Callable, body_fun_dbg: core.DebugInfo) -> Callable:
  body_fun_ref = weakref.ref(body_fun)
  def scanned_fun(loop_carry, _):
    i, x = loop_carry
    return (i + 1, body_fun_ref()(i, x)), None
  api_util.save_wrapped_fun_debug_info(
      scanned_fun,
      body_fun_dbg._replace(arg_names=body_fun_dbg.arg_names + ("",)))
  return scanned_fun

@api_boundary
def fori_loop(lower, upper, body_fun, init_val,
              *, unroll: int | bool | None = None):
  """Loop from ``lower`` to ``upper`` by reduction to :func:`jax.lax.while_loop`.

  The `Haskell-like type signature`_ in brief is

  .. code-block:: haskell

    fori_loop :: Int -> Int -> ((Int, a) -> a) -> a -> a

  The semantics of ``fori_loop`` are given by this Python implementation::

    def fori_loop(lower, upper, body_fun, init_val):
      val = init_val
      for i in range(lower, upper):
        val = body_fun(i, val)
      return val

  As the Python version suggests, setting ``upper <= lower`` will produce no
  iterations. Negative or custom increments are not supported.

  Unlike that Python version, ``fori_loop`` is implemented in terms of either a
  call to :func:`jax.lax.while_loop` or a call to :func:`jax.lax.scan`. If the
  trip count is static (meaning known at tracing time, perhaps because ``lower``
  and ``upper`` are Python integer literals) then the ``fori_loop`` is
  implemented in terms of :func:`~scan` and reverse-mode autodiff is supported;
  otherwise, a ``while_loop`` is used and reverse-mode autodiff is not
  supported.  See those functions' docstrings for more information.

  Also unlike the Python analogue, the loop-carried value ``val`` must hold a
  fixed shape and dtype across all iterations (and not just be consistent up to
  NumPy rank/shape broadcasting and dtype promotion rules, for example). In
  other words, the type ``a`` in the type signature above represents an array
  with a fixed shape and dtype (or a nested tuple/list/dict container data
  structure with a fixed structure and arrays with fixed shape and dtype at the
  leaves).

  .. note::
    :py:func:`fori_loop` compiles ``body_fun``, so while it can be combined with
    :py:func:`jit`, it's usually unnecessary.

  Args:
    lower: an integer representing the loop index lower bound (inclusive)
    upper: an integer representing the loop index upper bound (exclusive)
    body_fun: function of type ``(int, a) -> a``.
    init_val: initial loop carry value of type ``a``.
    unroll: An optional integer or boolean that determines how much to unroll
      the loop. If an integer is provided, it determines how many unrolled
      loop iterations to run within a single rolled iteration of the loop. If a
      boolean is provided, it will determine if the loop is completely unrolled
      (i.e. `unroll=True`) or left completely unrolled (i.e. `unroll=False`).
      This argument is only applicable if the loop bounds are statically known.

  Returns:
    Loop value from the final iteration, of type ``a``.

  .. _Haskell-like type signature: https://wiki.haskell.org/Type_signature
  """
  if not callable(body_fun):
    raise TypeError("lax.fori_loop: body_fun argument should be callable.")

  # TODO(phawkins): perhaps do more type checking here, better error messages.
  lower_dtype = dtypes.canonicalize_dtype(lax.dtype(lower))
  upper_dtype = dtypes.canonicalize_dtype(lax.dtype(upper))
  if lower_dtype == upper_dtype:
    dtype = lower_dtype
  else:
    # As a special case: allow promotion of weak integers (e.g., Python scalars)
    # This improves the ergonomics if one but not both of the loop bounds is a
    # scalar.
    dtype = None
    if (np.issubdtype(lower_dtype, np.signedinteger) and
        np.issubdtype(upper_dtype, np.signedinteger)):
      lower_weak = dtypes.is_weakly_typed(lower)
      upper_weak = dtypes.is_weakly_typed(upper)
      if lower_weak and not upper_weak:
        dtype = upper_dtype
      elif not lower_weak and upper_weak:
        dtype = lower_dtype

    if dtype is None:
      raise TypeError("lower and upper arguments to fori_loop must have equal "
                      f"types, got {lower_dtype.name} and {upper_dtype.name}")

  # If we can specialize on the trip count, call scan instead of a while_loop
  # to enable efficient reverse-mode differentiation.
  if core.is_concrete(lower) and core.is_concrete(upper):
    try:
      lower_ = int(lower)
      upper_ = int(upper)
    except (TypeError, core.InconclusiveDimensionOperation):
      use_scan = False
    else:
      use_scan = True
  else:
    use_scan = False

  body_fun_dbg = api_util.debug_info("fori_loop", body_fun,
                                     (0, init_val), {})

  if use_scan:
    if unroll is None:
      unroll = False
    length = max(upper_ - lower_, 0)
    if config.disable_jit.value and length == 0:
      # non-jit implementation of scan does not support length=0
      return init_val
    scan_body = _fori_scan_body_fun(body_fun, body_fun_dbg)
    (_, result), _ = scan(
        scan_body,
        (lower_, init_val),
        None,
        length=length,
        unroll=unroll,
    )
    return result
  if unroll is not None and unroll is not False and unroll != 1:
    raise ValueError("Can only use `unroll` in `fori_loop` if the loop bounds "
                     "are statically known.")

  if lower_dtype != dtype:
    lower = lax.convert_element_type(lower, dtype)  # type: ignore
  if upper_dtype != dtype:
    upper = lax.convert_element_type(upper, dtype)  # type: ignore
  while_body_fun = _fori_body_fun(body_fun, body_fun_dbg)
  _, _, result = while_loop(_fori_cond_fun, while_body_fun,
                            (lower, upper, init_val))
  return result

### map and miscellaneous rules

def _scan_leaf(leaf, batch_elems, num_batches, batch_size):
  def f(l):
    return l[:batch_elems].reshape(num_batches, batch_size, *leaf.shape[1:])

  aval = core.typeof(leaf)
  if aval.sharding.spec[0] is not None:
    raise ValueError(
        '0th dimension of leaf passed to `jax.lax.map` should be replicated.'
        f' Got {aval.str_short(True, True)}')

  out_s = aval.sharding.update(spec=P(None, None, *aval.sharding.spec[1:]))
  out_s = canonicalize_sharding(out_s, 'lax.map')
  if out_s is not None and out_s.mesh._any_axis_explicit:
    return auto_axes(f, out_sharding=out_s, axes=out_s.mesh.explicit_axes)(leaf)
  return f(leaf)

def _remainder_leaf(leaf, batch_elems):
  def f(l):
    return l[batch_elems:]
  sharding = canonicalize_sharding(core.typeof(leaf).sharding, 'lax.map')
  if sharding is not None and sharding.mesh._any_axis_explicit:
    return auto_axes(
        f, out_sharding=sharding, axes=sharding.mesh.explicit_axes
    )(leaf)
  return f(leaf)

def _batch_and_remainder(x, batch_size: int):
  leaves, treedef = tree_flatten(x)
  if not leaves:
    return x, None
  num_batches, remainder = divmod(leaves[0].shape[0], batch_size)
  batch_elems = num_batches * batch_size
  if num_batches == 0:
    remainder_leaves = [_remainder_leaf(leaf, batch_elems) for leaf in leaves]
    return None, treedef.unflatten(remainder_leaves)
  elif remainder:
    scan_leaves, remainder_leaves = unzip2(  # type: ignore
        [(_scan_leaf(leaf, batch_elems, num_batches, batch_size),
          _remainder_leaf(leaf, batch_elems)) for leaf in leaves])
    return treedef.unflatten(scan_leaves), treedef.unflatten(remainder_leaves)
  else:
    scan_leaves = tuple(_scan_leaf(leaf, batch_elems, num_batches, batch_size)
                        for leaf in leaves)
    return treedef.unflatten(scan_leaves), None

@api_boundary
def map(f, xs, *, batch_size: int | None = None):
  """Map a function over leading array axes.

  Like Python's builtin map, except inputs and outputs are in the form of
  stacked arrays. Consider using the :func:`~jax.vmap` transform instead, unless you
  need to apply a function element by element for reduced memory usage or
  heterogeneous computation with other control flow primitives.

  When ``xs`` is an array type, the semantics of :func:`~map` are given by this
  Python implementation::

    def map(f, xs):
      return np.stack([f(x) for x in xs])

  Like :func:`~scan`, :func:`~map` is implemented in terms of JAX primitives so
  many of the same advantages over a Python loop apply: ``xs`` may be an
  arbitrary nested pytree type, and the mapped computation is compiled only
  once.

  If ``batch_size`` is provided, the computation is executed in batches of that size
  and parallelized using :func:`~jax.vmap`. This can be used as either a more performant
  version of ``map`` or as a memory-efficient version of ``vmap``. If the axis is not
  divisible by the batch size, the remainder is processed in a separate ``vmap`` and
  concatenated to the result.

    >>> x = jnp.ones((10, 3, 4))
    >>> def f(x):
    ...   print('inner shape:', x.shape)
    ...   return x + 1
    >>> y = lax.map(f, x, batch_size=3)
    inner shape: (3, 4)
    inner shape: (3, 4)
    >>> y.shape
    (10, 3, 4)

  In the example above, "inner shape" is printed twice, once while tracing the batched
  computation and once while tracing the remainder computation.

  Args:
    f: a Python function to apply element-wise over the first axis or axes of
      ``xs``.
    xs: values over which to map along the leading axis.
    batch_size: (optional) integer specifying the size of the batch for each step to execute
      in parallel.

  Returns:
    Mapped values.
  """
  if batch_size is not None:
    scan_xs, remainder_xs = _batch_and_remainder(xs, batch_size)
    g = lambda _, x: ((), api.vmap(f)(x))
    if scan_xs is not None:
      _, scan_ys = scan(g, (), scan_xs)
    else:
      scan_ys = None

    flatten = lambda x: x.reshape(-1, *x.shape[2:])
    if scan_ys is None:
      ys = api.vmap(f)(remainder_xs)
    elif remainder_xs is not None:
      remainder_ys = api.vmap(f)(remainder_xs)
      ys = tree_map(
        lambda x, y: lax.concatenate([flatten(x), y], dimension=0), scan_ys,
        remainder_ys)
    else:
      ys = tree_map(flatten, scan_ys)
  else:
    g = lambda _, x: ((), f(x))
    _, ys = scan(g, (), xs)
  return ys

def _rng_bit_generator_batching_rule(batched_args, batch_dims, *, shape, dtype,
                                     algorithm, out_sharding):
  keys, = batched_args
  bd, = batch_dims
  if bd is batching.not_mapped:
    return lax.rng_bit_generator_p.bind(
        keys, shape=shape, dtype=dtype, algorithm=algorithm,
        out_sharding=out_sharding), (None, None)
  keys = batching.moveaxis(keys, bd, 0)
  batch_size = keys.shape[0]
  out_s = (out_sharding.update(spec=(keys.aval.sharding.spec[0], *out_sharding.spec))
           if out_sharding is not None else None)
  key = keys[0]
  new_key, bits = lax.rng_bit_generator_p.bind(
      key, shape=(batch_size, *shape), dtype=dtype, algorithm=algorithm,
      out_sharding=out_s)
  new_keys = slicing.dynamic_update_index_in_dim(keys, new_key, 0, axis=0)
  return (new_keys, bits), (0, 0)

batching.primitive_batchers[lax.rng_bit_generator_p] = _rng_bit_generator_batching_rule

### associative_scan

@api_boundary
def associative_scan(fn: Callable, elems, reverse: bool = False, axis: int = 0):
  """Performs a scan with an associative binary operation, in parallel.

  For an introduction to associative scans, see [BLE1990]_.

  Args:
    fn: A Python callable implementing an associative binary operation with
      signature ``r = fn(a, b)``. Function `fn` must be associative, i.e., it
      must satisfy the equation
      ``fn(a, fn(b, c)) == fn(fn(a, b), c)``.

      The inputs and result are (possibly nested Python tree structures of)
      array(s) matching ``elems``. Each array has a dimension in place
      of the ``axis`` dimension. `fn` should be applied elementwise over
      the ``axis`` dimension (for example, by using :func:`jax.vmap` over the
      elementwise function.)

      The result ``r`` has the same shape (and structure) as the two inputs
      ``a`` and ``b``.
    elems: A (possibly nested Python tree structure of) array(s), each with
      an ``axis`` dimension of size ``num_elems``.
    reverse: A boolean stating if the scan should be reversed with respect to
      the ``axis`` dimension.
    axis: an integer identifying the axis over which the scan should occur.

  Returns:
    A (possibly nested Python tree structure of) array(s) of the same shape
    and structure as ``elems``, in which the ``k``'th element of ``axis`` is the
    result of recursively applying ``fn`` to combine the first ``k`` elements
    of ``elems`` along ``axis``. For example, given ``elems = [a, b, c, ...]``,
    the result would be ``[a, fn(a, b), fn(fn(a, b), c), ...]``.

    If ``elems = [..., x, y, z]`` and ``reverse`` is true, the result is
    ``[..., f(f(z, y), x), f(z, y), z]``.

  Example 1: partial sums of an array of numbers:

  >>> lax.associative_scan(jnp.add, jnp.arange(0, 4))
  Array([0, 1, 3, 6], dtype=int32)

  Example 2: partial products of an array of matrices

  >>> mats = jax.random.uniform(jax.random.key(0), (4, 2, 2))
  >>> partial_prods = lax.associative_scan(jnp.matmul, mats)
  >>> partial_prods.shape
  (4, 2, 2)

  Example 3: reversed partial sums of an array of numbers

  >>> lax.associative_scan(jnp.add, jnp.arange(0, 4), reverse=True)
  Array([6, 6, 5, 3], dtype=int32)

  .. [BLE1990] Blelloch, Guy E. 1990. "Prefix Sums and Their Applications.",
    Technical Report CMU-CS-90-190, School of Computer Science, Carnegie Mellon
    University.
  """
  if not callable(fn):
    raise TypeError("lax.associative_scan: fn argument should be callable.")
  elems_flat, tree = tree_flatten(elems)

  if reverse:
    elems_flat = [lax.rev(elem, [axis]) for elem in elems_flat]

  def combine(a_flat, b_flat):
    # Lower `fn` to operate on flattened sequences of elems.
    a = tree_unflatten(tree, a_flat)
    b = tree_unflatten(tree, b_flat)
    c = fn(a, b)
    c_flat, _ = tree_flatten(c)
    return c_flat

  # Check that all inputs have a consistent leading dimension `num_elems`.
  axis = util.canonicalize_axis(axis, elems_flat[0].ndim)

  if not core.is_constant_dim(elems_flat[0].shape[axis]):
    raise NotImplementedError("associative scan over axis "
        f"of non-constant size: {elems_flat[0].shape[axis]}. You may be "
        "able to avoid this on TPU. See b/274176030.")
  num_elems = int(elems_flat[0].shape[axis])
  if not all(int(elem.shape[axis]) == num_elems for elem in elems_flat[1:]):
    raise ValueError('Array inputs to associative_scan must have the same '
                     'first dimension. (saw: {})'
                     .format([elem.shape for elem in elems_flat]))


  # Summary of algorithm:
  #
  # Consider elements of `_scan(elems)` at odd indices. That's the same as first
  # summing successive pairs of elements of `elems` and performing a scan on
  # that half sized tensor. We perform the latter scan by recursion.
  #
  # Now consider the even elements of `_scan(elems)`. These can be computed
  # from the odd elements of `_scan(elems)` by adding each odd element of
  # `_scan(elems)` to the matching even element in the original `elems`.
  #
  # We return the odd and even elements interleaved.
  #
  # For the base case of the recursion we return the first element
  # of `elems` followed by the sum of the first two elements computed as
  # a (small two-down-to-one) reduction step.
  def _scan(elems):
    """Perform scan on `elems`."""

    num_elems = elems[0].shape[axis]

    if num_elems < 2:
      return elems

    # Combine adjacent pairs of elements.
    reduced_elems = combine(
      [slicing.slice_in_dim(elem, 0, -1, stride=2, axis=axis) for elem in elems],
      [slicing.slice_in_dim(elem, 1, None, stride=2, axis=axis)
       for elem in elems])

    # Recursively compute scan for partially reduced tensors.
    odd_elems = _scan(reduced_elems)

    if num_elems % 2 == 0:
      even_elems = combine(
        [slicing.slice_in_dim(e, 0, -1, axis=axis) for e in odd_elems],
        [slicing.slice_in_dim(e, 2, None, stride=2, axis=axis) for e in elems])
    else:
      even_elems = combine(
        odd_elems,
        [slicing.slice_in_dim(e, 2, None, stride=2, axis=axis) for e in elems])

    # The first element of a scan is the same as the first element
    # of the original `elems`.
    even_elems = [
      lax.concatenate([slicing.slice_in_dim(elem, 0, 1, axis=axis), result],
                      dimension=axis)
      for (elem, result) in zip(elems, even_elems)]
    return list(_map(partial(_interleave, axis=axis), even_elems, odd_elems))

  scans = _scan(elems_flat)

  if reverse:
    scans = [lax.rev(scanned, [axis]) for scanned in scans]

  return tree_unflatten(tree, scans)

def _interleave(a, b, axis):
  """Given two Tensors of static shape, interleave them along the first axis."""
  assert a.shape[axis] == b.shape[axis] or a.shape[axis] == b.shape[axis] + 1
  a_pad = [(0, 0, 0)] * a.ndim
  b_pad = [(0, 0, 0)] * b.ndim
  a_pad[axis] = (0, 1 if a.shape[axis] == b.shape[axis] else 0, 1)
  b_pad[axis] = (1, 0 if a.shape[axis] == b.shape[axis] else 1, 1)
  op = lax.bitwise_or if a.dtype == np.bool_ else lax.add
  return op(lax.pad(a, lax._const(a, 0), a_pad),
            lax.pad(b, lax._const(b, 0), b_pad))

### Cumulative reductions.

def cumsum(operand: Array, axis: int = 0, reverse: bool = False) -> Array:
  """Computes a cumulative sum along `axis`."""
  return cumsum_p.bind(operand, axis=int(axis), reverse=bool(reverse))

def cumprod(operand: Array, axis: int = 0, reverse: bool = False) -> Array:
  """Computes a cumulative product along `axis`."""
  return cumprod_p.bind(operand, axis=int(axis), reverse=bool(reverse))

def cummax(operand: Array, axis: int = 0, reverse: bool = False) -> Array:
  """Computes a cumulative maximum along `axis`."""
  return cummax_p.bind(operand, axis=int(axis), reverse=bool(reverse))

def cummin(operand: Array, axis: int = 0, reverse: bool = False) -> Array:
  """Computes a cumulative minimum along `axis`."""
  return cummin_p.bind(operand, axis=int(axis), reverse=bool(reverse))

def cumlogsumexp(operand: Array, axis: int = 0, reverse: bool = False) -> Array:
  """Computes a cumulative logsumexp along `axis`."""
  return cumlogsumexp_p.bind(operand, axis=int(axis), reverse=bool(reverse))

def _cumred_shape_rule(x, *, axis: int, reverse: bool):
  if axis < 0:
    raise ValueError("XLA operations do not allow negative axes")
  elif axis >= x.ndim:
    raise ValueError(
        f"axis {axis} is out of bounds for array of shape {x.shape}")
  return x.shape

def _cumred_sharding_rule(x, *, axis: int, reverse: bool):
  return x.sharding

def _cumsum_transpose_rule(t, operand, *, axis: int, reverse: bool):
  return [cumsum(t, axis=axis, reverse=not reverse)]


def cumred_reduce_window_impl(window_reduce: Callable, x, *, axis: int,
                              reverse: bool):
  n = x.shape[axis]
  if n == 0:
    return x
  padding = [(0, 0)] * x.ndim
  padding[axis] = (0, n - 1) if reverse else (n - 1, 0)
  strides = [1] * x.ndim
  window_dims = [1] * x.ndim
  window_dims[axis] = n
  return window_reduce(x, window_dims, strides, padding)


def cumred_gpu_impl(window_reduce: Callable, reduce_fn: Callable, x, *,
                    axis: int, reverse: bool):
  # On GPU, reduce_window is executed in a single fusion and associative_scan
  # is split into multiple to materialize intermediate calculations.
  # On small inputs reduce_window is faster being a single fusion,
  # but on larger ones is slower because of O(n^2) complexity.
  # This conservative value of the threshold was obtained via benchmarking.
  if not core.is_constant_dim(x.shape[axis]):
    raise NotImplementedError(
        "associative scan reductions not implemented with shape polymorphism "
        "and native serialization on GPU")
  if x.shape[axis] > 32:
    return associative_scan(reduce_fn, x, reverse=reverse, axis=axis)
  return cumred_reduce_window_impl(window_reduce, x, axis=axis, reverse=reverse)


def _cumred_batch_rule(prim, batched_args, batch_dims, *, axis: int,
                       reverse: bool):
  operand, = batched_args
  bdim, = batch_dims
  axis = axis if axis < bdim else axis + 1
  return prim.bind(operand, axis=axis, reverse=reverse), bdim

def _cumred_dtype_rule(name, operand, *args, **kw):
  if not dtypes.issubdtype(operand.dtype, np.number):
    raise TypeError("{} does not accept dtype {}. Accepted dtypes are subtypes "
                    "of number.".format(name, np.dtype(operand.dtype).name))
  return dtypes.canonicalize_dtype(operand.dtype)


def _cumulative_reduction_primitive(name, reduce_fn, reduce_window_fn):
  reducer_p = lax.standard_primitive(
    _cumred_shape_rule, partial(_cumred_dtype_rule, name),
    name, sharding_rule=_cumred_sharding_rule,
    vma_rule=partial(core.standard_vma_rule, name))
  batching.primitive_batchers[reducer_p] = partial(_cumred_batch_rule,
                                                   reducer_p)

  def register_lowering(fn, platform=None):
    mlir.register_lowering(
        reducer_p,
        mlir.lower_fun(fn, multiple_results=False),
        platform=platform,
        inline=False)

  # For jax-metal, until reduce_window legalization is better supported.
  register_lowering(partial(associative_scan, reduce_fn), 'METAL')
  # In XLA, there's a rewriter for an O(N^2) reduce-window implementation.
  register_lowering(
      partial(cumred_reduce_window_impl, reduce_window_fn)
  )

  return reducer_p

cumsum_p = _cumulative_reduction_primitive(
    "cumsum", lax.add, windowed_reductions._reduce_window_sum)
ad.deflinear2(cumsum_p, _cumsum_transpose_rule)

cumlogsumexp_p = _cumulative_reduction_primitive(
    "cumlogsumexp", logaddexp, windowed_reductions._reduce_window_logaddexp)
cumprod_p = _cumulative_reduction_primitive(
    "cumprod", lax.mul, windowed_reductions._reduce_window_prod)
cummax_p = _cumulative_reduction_primitive(
    "cummax", lax.max, windowed_reductions._reduce_window_max)
cummin_p = _cumulative_reduction_primitive(
    "cummin", lax.min, windowed_reductions._reduce_window_min)


def _cumulative_jvp_rule(primals, tangents, *, axis: int, reverse: bool,
                         combine_fn: Callable):
  # Irrespective of backend, we always use the parallel prefix scan
  # implementation when differentiating because reduce_window is not
  # arbitrarily differentiable.
  return api.jvp(partial(associative_scan, combine_fn, axis=axis,
                         reverse=reverse),
                 primals, tangents)

ad.primitive_jvps[cumlogsumexp_p] = partial(_cumulative_jvp_rule, combine_fn=logaddexp)
ad.primitive_jvps[cumprod_p] = partial(_cumulative_jvp_rule, combine_fn=lax.mul)
ad.primitive_jvps[cummin_p] = partial(_cumulative_jvp_rule, combine_fn=lax.min)
ad.primitive_jvps[cummax_p] = partial(_cumulative_jvp_rule, combine_fn=lax.max)
