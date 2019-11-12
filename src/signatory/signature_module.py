# Copyright 2019 Patrick Kidger. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# 
#    http://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# =========================================================================
"""Provides operations relating to the signature transform."""


import torch
from torch import nn
from torch import autograd
from torch.autograd import function as autograd_function
import warnings

from . import backend
from . import utility
# noinspection PyUnresolvedReferences
from . import _impl

# noinspection PyUnreachableCode
if False:
    from typing import Any, List, Union


def interpet_forward_args(ctx, path, basepoint, initial):
    ctx.basepoint_is_tensor = isinstance(basepoint, torch.Tensor)
    ctx.initial_is_tensor = isinstance(initial, torch.Tensor)
    basepoint, basepoint_value = backend.interpret_basepoint(basepoint, path.size(-2), path.size(-1), path.dtype,
                                                             path.device)
    initial, initial_value = backend.interpret_initial(initial)
    return basepoint, basepoint_value, initial, initial_value


def interpret_backward_grad(ctx, grad_basepoint, grad_initial):
    if not ctx.basepoint_is_tensor:
        grad_basepoint = None
    if not ctx.initial_is_tensor:
        grad_initial = None
    return grad_basepoint, grad_initial


class _SignatureFunction(autograd.Function):
    @staticmethod
    def forward(ctx, path, depth, stream, basepoint, inverse, initial):

        basepoint, basepoint_value, initial, initial_value = interpet_forward_args(ctx, path, basepoint, initial)

        signature_, path_increments = _impl.signature_forward(path, depth, stream, basepoint, basepoint_value, inverse,
                                                              initial, initial_value)
        ctx.save_for_backward(signature_, path_increments)
        ctx.depth = depth
        ctx.stream = stream
        ctx.basepoint = basepoint
        ctx.inverse = inverse
        ctx.initial = initial

        return signature_

    @staticmethod
    @autograd_function.once_differentiable  # Our backward function uses in-place operations for memory efficiency
    def backward(ctx, grad_result):
        signature_, path_increments = ctx.saved_tensors

        grad_path, grad_basepoint, grad_initial = _impl.signature_backward(grad_result, signature_, path_increments,
                                                                           ctx.depth, ctx.stream, ctx.basepoint,
                                                                           ctx.inverse, ctx.initial)

        grad_basepoint, grad_initial = interpret_backward_grad(ctx, grad_basepoint, grad_initial)

        return grad_path, None, None, grad_basepoint, None, grad_initial, None


def _signature_checkargs(path, depth, basepoint, initial):
    path = path.transpose(0, 1)  # (batch, stream, channel) to (stream, batch, channel)
    basepoint, basepoint_value = backend.interpret_basepoint(basepoint, path.size(-2), path.size(-1), path.dtype,
                                                             path.device)
    initial, initial_value = backend.interpret_initial(initial)
    _impl.signature_checkargs(path, depth, basepoint, basepoint_value, initial, initial_value)


def _signature_batch_trick(path, depth, stream, basepoint, inverse, initial):
    if stream:
        # We can't use this trick in this case
        return

    if path.is_cuda:
        # TODO: find a better way to choose this value when on the GPU.
        #       Note that there are two implications of changing this value.
        #       First of all, there will be greater potential parallelisation, so the main computation will go faster.
        #       However we don't yet parallelise the bit where we combine the results of our parallel computations,
        #       so setting this value too large will cause a slowdown as we serially perform this many tensor
        #       multplications.
        threshold = 512
    else:
        if not path.requires_grad:
            # If we're on the CPU then parallelisation will automatically occur more efficiently than this trick allows.
            # However that parallelisation will not generate certain intermediate tensors needed to perform an efficient
            # backward pass (whilst the batch trick does), so we don't use it if we're going to perform a backward
            # operation.
            return
        threshold = _impl.hardware_concurrency()
        if threshold == 0:
            # Indicates that we can't get the amount of hardware concurrency, which is a bit weird.
            # In this case let's not try to be clever.
            return

    batch_size, stream_size, channel_size = path.shape

    # Number of chunks to split the stream in to
    mult = int(round(float(threshold) / batch_size))
    mult = min(mult, int(stream_size / 3), utility.max_parallelism())

    # If the problem isn't large enough to be worth parallelising
    if mult < 2:
        return

    remainder = stream_size % mult                 # How much of the stream is left over as a remainder
    reduced_bulk_length = int(stream_size / mult)  # Size of each chunk of the stream
    bulk_length = stream_size - remainder          # Size of all of the chunks of the stream put together,
    # excluding the remainder
    path_bulk = path[:, :bulk_length]
    path_remainder = path[:, bulk_length:]

    # Need to set basepoints to the end of each previous chunk
    path_bulk = path_bulk.view(batch_size, mult, reduced_bulk_length, channel_size)
    ends = path_bulk[:, :, -1].roll(shifts=1, dims=-2)
    if remainder != 0:
        basepoint_remainder = ends[:, 0].clone()
    if isinstance(basepoint, torch.Tensor):
        # noinspection PyUnresolvedReferences
        ends[:, 0].copy_(basepoint)
    elif basepoint is True:
        ends[:, 0].zero_()
    else:
        # noinspection PyUnresolvedReferences
        ends[:, 0].copy_(path_bulk[:, 0, 0])
    path_bulk = path_bulk.reshape(batch_size * mult, reduced_bulk_length, channel_size)
    basepoint = ends.view(batch_size * mult, channel_size)

    # noinspection PyUnresolvedReferences
    result_bulk = _SignatureFunction.apply(path_bulk.transpose(0, 1), depth, stream, basepoint, inverse, None)
    result_bulk = result_bulk.view(batch_size, mult, result_bulk.size(-1))
    chunks = []
    if isinstance(initial, torch.Tensor):
        chunks.append(initial)
    chunks.extend(result_bulk.unbind(dim=-2))
    if remainder != 0:
        # transpose to go from Python convention of (batch, stream, channel) to autograd/C++ convention of
        # (stream, batch, channel)
        # noinspection PyUnresolvedReferences
        result_remainder = _SignatureFunction.apply(path_remainder.transpose(0, 1), depth, stream, basepoint_remainder,
                                                    inverse, None)
        chunks.append(result_remainder)

    return multi_signature_combine(chunks, channel_size, depth, inverse)


def signature(path, depth, stream=False, basepoint=False, inverse=False, initial=None):
    # type: (torch.Tensor, int, bool, Union[bool, torch.Tensor], bool, Union[None, torch.Tensor]) -> torch.Tensor

    r"""Applies the signature transform to a stream of data.

    The input :attr:`path` is expected to be a three-dimensional tensor, with dimensions :math:`(N, L, C)`, where
    :math:`N` is the batch size, :math:`L` is the length of the input sequence, and :math:`C` denotes the number of
    channels. Thus each batch element is interpreted as a stream of data :math:`(x_1, \ldots, x_L)`, where each
    :math:`x_i \in \mathbb{R}^C`.

    Let :math:`f = (f_1, \ldots, f_C) \colon [0, 1] \to \mathbb{R}^C`, be the unique continuous piecewise linear path
    such that :math:`f(\tfrac{i - 1}{N - 1}) = x_i`. Then and the signature transform of depth :attr:`depth` is
    computed, defined by

    .. math::
        \mathrm{Sig}(\text{path}) = \left(\left( \,\underset{0 < t_1 < \cdots < t_k < 1}{\int\cdots\int} \prod_{j = 1}^k \frac{\mathrm d f_{i_j}}{\mathrm dt}(t_j) \mathrm dt_1 \cdots \mathrm dt_k \right)_{\!\!1 \leq i_1, \ldots, i_k \leq C}\right)_{\!\!1\leq k \leq \text{depth}}.

    This gives a tensor of shape

    .. math::
        (N, C + C^2 + \cdots + C^\text{depth}).

    Arguments:
        path (:class:`torch.Tensor`): The batch of input paths to apply the signature transform to.

        depth (int): The depth to truncate the signature at.

        stream (bool, optional): Defaults to False. If False then the usual signature transform of the whole path is
            computed. If True then the signatures of all paths :math:`(x_1, \ldots, x_j)`, for :math:`j=2, \ldots, L`,
            are returned. (Or :math:`j=1, \ldots, L` is :attr:`basepoint` is passed, see below.)

        basepoint (bool or :class:`torch.Tensor`, optional): Defaults to False. If :attr:`basepoint` is True then an
            additional point :math:`x_0 = 0 \in \mathbb{R}^C` is prepended to the path before the signature transform is
            applied. (If this is False then the signature transform is invariant to translations of the path, which may
            or may not be desirable. Setting this to True removes this invariance.)
            Alternatively it may be a :class:`torch.Tensor` specifying the value of :math:`x_0`, in which case it should
            have shape :math:`(N, C)`.

        inverse (bool, optional): Defaults to False. If True then it is in fact the inverse signature that is computed.
            That is, we flip the input path along its stream dimension before computing the signature.
            If :attr:`stream` is True then each sub-path is the same as before, and are each individually flipped along
            their stream dimensions, and kept in the same order with respect to each other.
            (But without the extra computational overhead of actually doing all of these flips.)
            From a machine learning perspective it does not particularly matter whether the signature or the inverse
            signature is computed - both represent essentially the same information as each other.

        initial (None or :class:`torch.Tensor`, optional): Defaults to None. If it is a :class:`torch.Tensor` then it
            must be of size :math:`(N, C + C^2 + ... + C^\text{depth})`, corresponding to the signature of another path.
            Then this signature is pre-tensor-multiplied on to the signature of :attr:`path`. For a more thorough
            explanation, see :ref:`this example<examples-online>`.
            (The appropriate modifications are made if :attr:`inverse=True` or if :attr:`basepoint`.)

    Returns:
        A :class:`torch.Tensor`. Given an input :class:`torch.Tensor` of shape :math:`(N, L, C)`, and input arguments
        :attr:`depth`, :attr:`basepoint`, :attr:`stream`, then the return value is, in pseudocode:

        .. code-block:: python

            if stream:
                if basepoint is True or isinstance(basepoint, torch.Tensor):
                    return torch.Tensor of shape (N, L, C + C^2 + ... + C^depth)
                else:
                    return torch.Tensor of shape (N, L - 1, C + C^2 + ... + C^depth)
            else:
                return torch.Tensor of shape (N, C + C^2 + ... + C^depth)

        Note that the number of output channels may be calculated via the convenience function
        :func:`signatory.signature_channels`.
    """

    if initial is not None and basepoint is False:
        warnings.warn("Argument 'initial' has been set but argument 'basepoint' has not. This is almost certainly a "
                      "mistake. Argument 'basepoint' should be set to the final value of the path whose signature is "
                      "'initial'. See the documentation at\n"
                      "    https://signatory.readthedocs.io/en/latest/pages/examples/online.html\n"
                      "for more information.")

    _signature_checkargs(path, depth, basepoint, initial)

    result = _signature_batch_trick(path, depth, stream, basepoint, inverse, initial)
    if result is None:  # Either because we disabled use of the batch trick, or because the batch trick doesn't apply
        result = _SignatureFunction.apply(path.transpose(0, 1), depth, stream, basepoint, inverse, initial)

    # We have to do the transpose outside of autograd.Function.apply to avoid PyTorch bug 24413
    if stream:
        # NOT .transpose_ - the underlying TensorImpl (in C++) is used elsewhere and we don't want to change it.
        result = result.transpose(0, 1)
    return result


class Signature(nn.Module):
    """:class:`torch.nn.Module` wrapper around the :func:`signatory.signature` function.

    Arguments:
        depth (int): as :func:`signatory.signature`.

        stream (bool, optional): as :func:`signatory.signature`.

        inverse (bool, optional): as :func:`signatory.signature`.
    """

    def __init__(self, depth, stream=False, inverse=False, **kwargs):
        # type: (int, bool, bool, **Any) -> None
        super(Signature, self).__init__(**kwargs)
        self.depth = depth
        self.stream = stream
        self.inverse = inverse

    def forward(self, path, basepoint=False, initial=None):
        # type: (torch.Tensor, Union[bool, torch.Tensor], Union[None, torch.Tensor]) -> torch.Tensor
        """The forward operation.

        Arguments:
            path (:class:`torch.Tensor`): As :func:`signatory.signature`.

            basepoint (bool or :class:`torch.Tensor`, optional): As :func:`signatory.signature`.

            initial (None or :class:`torch.Tensor`, optional): As :func:`signatory.signature`.

        Returns:
            As :func:`signatory.signature`.
        """
        return signature(path, self.depth, stream=self.stream, basepoint=basepoint, inverse=self.inverse,
                         initial=initial)

    def extra_repr(self):
        return 'depth={depth}, stream={stream}, inverse={inverse}'.format(depth=self.depth, stream=self.stream,
                                                                          inverse=self.inverse)


# A wrapper for the sake of consistent documentation
def signature_channels(channels, depth):
    # type: (int, int) -> int
    r"""Computes the number of output channels from a signature call. Specifically, it computes

    .. math::
        \text{channels} + \text{channels}^2 + \cdots + \text{channels}^\text{depth}.

    Arguments:
        channels (int): The number of channels in the input; that is, the dimension of the space that the input path
            resides in.

        depth (int): The depth of the signature that is being computed.

    Returns:
        An int specifying the number of channels in the signature of the path.
    """

    return _impl.signature_channels(channels, depth)


def extract_signature_term(sigtensor, channels, depth):
    # type: (torch.Tensor, int, int) -> torch.Tensor
    r"""Extracts a particular term from a signature.

    The signature to depth :math:`d` of a batch of paths in :math:`\mathbb{R}^\text{C}` is a tensor with
    :math:`C + C^2 + \cdots + C^d` channels. (See :func:`signatory.signature`.) This function extracts the :attr:`depth`
    term of that, returning a tensor with just :math:`C^\text{depth}` channels.

    Arguments:
        sigtensor (:class:`torch.Tensor`): The signature to extract the term from. Should be a result from the
            :func:`signatory.signature` function.

        channels (int): The number of input channels :math:`C`.

        depth (int): The depth of the term to be extracted from the signature.

    Returns:
        The :class:`torch.Tensor` corresponding to the :attr:`depth` term of the signature.
    """

    if channels < 1:
        raise ValueError("in_channels must be at least 1")

    if depth == 1:
        start = 0
    else:
        start = signature_channels(channels, depth - 1)
    return sigtensor.narrow(dim=-1, start=start, length=channels ** depth)


class _SignatureCombineFunction(autograd.Function):
    @staticmethod
    def forward(ctx, input_channels, depth, *sigtensors):
        ctx.input_channels = input_channels
        ctx.depth = depth
        ctx.save_for_backward(*sigtensors)
        return _impl.signature_combine_forward(list(sigtensors), input_channels, depth)

    @staticmethod
    def backward(ctx, grad):
        sigtensors = ctx.saved_tensors
        grad = _impl.signature_combine_backward(grad, list(sigtensors), ctx.input_channels, ctx.depth)
        return (None, None) + tuple(grad)


def signature_combine(sigtensor1, sigtensor2, input_channels, depth, inverse=False):
    # type: (torch.Tensor, torch.Tensor, int, int, bool) -> torch.Tensor
    r"""Combines two signatures into a single signature.

    Usage is most clear by example. See :ref:`examples-combine`.

    See also :func:`signatory.multi_signature_combine` for a more general version.

    Arguments:
        sigtensor1 (:class:`torch.Tensor`): The signature of a path, as returned by :func:`signatory.signature`. This
            should be a two-dimensional tensor.

        sigtensor2 (:class:`torch.Tensor`): The signature of a second path, as returned by :func:`signatory.signature`,
            with the same shape as :attr:`sigtensor1`. Note that when the signature of the second path was created, it
            should have been called with :attr:`basepoint` set to the final value of the path that created
            :attr:`sigtensor1`. (See :ref:`examples-combine`.)

        input_channels (int): The number of channels in the two paths that were used to compute :attr:`sigtensor1` and
            :attr:`sigtensor2`. This must be the same for both :attr:`sigtensor1` and :attr:`sigtensor2`.

        depth (int): The depth that :attr:`sigtensor1` and :attr:`sigtensor2` have been calculated to. This must be
            the same for both :attr:`sigtensor1` and :attr:`sigtensor2`.

        inverse (bool, optional): Defaults to False. Whether :attr:`sigtensor1` and :attr:`sigtensor2` were created
            with :attr:`inverse=True`. This must be the same for both :attr:`sigtensor1` and :attr:`sigtensor2`.

    Returns:
        Let :attr:`path1` be the path whose signature is :attr:`sigtensor1`. Let :attr:`path2` be the path whose
        signature is :attr:`sigtensor2`. Then this function returns the signature of the concatenation of :attr:`path1`
        and :attr:`path2` along their stream dimension.

    .. danger::

        There is a subtle bug which can occur when using this function incautiously. Make sure that :attr:`sigtensor2`
        is created with an appropriate :attr:`basepoint`, see :ref:`examples-combine`.

        If this is not done then the return value of this function will be essentially meaningless numbers.
    """
    return multi_signature_combine([sigtensor1, sigtensor2], input_channels, depth, inverse)


def multi_signature_combine(sigtensors, input_channels, depth, inverse=False):
    # type: (List[torch.Tensor], int, int, bool) -> torch.Tensor
    r"""Combines multiple signatures into a single signature.

    See also :func:`signatory.signature_combine` for a simpler version.

    Arguments:
        sigtensors (list of :class:`torch.Tensor`): Signature of multiple paths, all of the same shape. They should all
            be two-dimensional tensors.

        input_channels (int): As :func:`signatory.signature_combine`.

        depth (int): As :func:`signatory.signature_combine`.

        inverse (bool, optional): As :func:`signatory.signature_combine`.

    Returns:
        Let :attr:`sigtensors` be a list of tensors, call them :math:`\text{sigtensor}_i` for
        :math:`i = 0, 1, \ldots, k`. Let :math:`\text{path}_i` be the path whose signature is
        :math:`\text{sigtensor}_i`. Then this function returns the signature of the concatenation of
        :math:`\text{path}_i` along their stream dimension.

    .. danger::

        Make sure that each element of :attr:`sigtensors` is created with an appropriate :attr:`basepoint`, as with
        :func:`signatory.signature_combine`.
    """
    if inverse:
        sigtensors = reversed(sigtensors)
    return _SignatureCombineFunction.apply(input_channels, depth, *sigtensors)
