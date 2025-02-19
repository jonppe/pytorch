import os
import warnings
from concurrent.futures import Future, ThreadPoolExecutor
from typing import cast, Optional, Union

import torch
import torch.distributed as dist
from torch.distributed._state_dict_utils import _offload_state_dict_to_cpu
from torch.distributed.checkpoint.stateful import Stateful
from torch.distributed.distributed_c10d import _get_default_group

from ._storage_utils import _storage_setup
from .default_planner import DefaultSavePlanner
from .metadata import Metadata, STATE_DICT_TYPE
from .planner import SavePlanner
from .storage import StorageWriter
from .utils import _api_bc_check, _DistWrapper, _profile


__all__ = ["save_state_dict", "save"]


def save_state_dict(
    state_dict: STATE_DICT_TYPE,
    storage_writer: StorageWriter,
    process_group: Optional[dist.ProcessGroup] = None,
    coordinator_rank: int = 0,
    no_dist: bool = False,
    planner: Optional[SavePlanner] = None,
) -> Metadata:
    """This method is deprecated. Please switch to 'save'."""
    warnings.warn(
        "'save_state_dict' is deprecated and will be removed in future versions."
        "Please use 'save' instead."
    )

    storage_writer.reset()

    # TODO: test returning `save` here instead.
    with _profile():
        return _save_state_dict(
            state_dict,
            storage_writer,
            process_group,
            coordinator_rank,
            no_dist,
            planner,
        )


@_api_bc_check
def save(
    state_dict: STATE_DICT_TYPE,
    *,
    checkpoint_id: Union[str, os.PathLike, None] = None,
    storage_writer: Optional[StorageWriter] = None,
    planner: Optional[SavePlanner] = None,
    process_group: Optional[dist.ProcessGroup] = None,
    coordinator_rank: int = 0,
    no_dist: Optional[bool] = None,
) -> Metadata:
    """
    Save a distributed model in SPMD style.

    This function is different from ``torch.save()`` as it handles
    ``ShardedTensor`` , and ``DTensor`` by having each rank only save their local shards.

    For each ``Stateful`` object (having both a ``state_dict`` and a ``load_state_dict``),
    save will call ``state_dict`` before serialization.

    .. warning::
        There is no guarantees of Backwards Compatibility across PyTorch versions
        for saved state_dicts.

    .. warning::
        If using the `process_group` argument, make sure that only its ranks
        call `save_state_dict` and that all data in state_dict belong to it.

    .. note::
        When saving checkpoint for FSDP's `ShardingStrategy.HYBRID_SHARD`, only one of
        the shard_group should be calling `save_state_dict` and the corresponding process
        group needs to be passed in.

    .. note::
        This function can be used to save a state_dict without having a process group
        initialized by passing ``no_dist=True``.
        (Default: ``False`` when torch.distributed is available and initialized)


    Args:
        state_dict (Dict[str, Any]): The state_dict to save.
        checkpoint_id (Union[str, os.PathLike, None]):
            The ID of this checkpoint instance. The meaning of the checkpoint_id
            depends on the storage. It can be a path to a folder or to a file.
            It can also be a key if the storage is a key-value store.
            (Default: ``None``)
        storage_writer (Optional[StorageWriter]):
            Instance of StorageWriter used to perform writes. If this is not
            specified, DCP will automatically infer the writer based on the
            checkpoint_id. If checkpoint_id is also None, an exception will
            be raised. (Default: ``None``)
        planner (Optional[SavePlanner]):
            Instance of SavePlanner. If this is not specificed, the default
            planner will be used. (Default: ``None``)
        process_group (Optional[ProcessGroup]):
            ProcessGroup to be used for cross-rank synchronization.
            (Default: ``None``)
        coordinator_rank (int): Rank to use to coordinate the checkpoint.
            rank0 is used by default. (Default: ``0``)
        no_dist (bool): If ``True``, distributed checkpoint will not save
            in SPMD style. (Default: ``False``)

    Returns:
        Metadata: Metadata object for the saved checkpoint.

    Example:
        >>> # xdoctest: +SKIP
        >>> my_model = MyModule()

        >>> model_state_dict = my_model.state_dict()

        >>> fs_storage_writer = torch.distributed.checkpoint.FileSystemWriter("/checkpoint/1")
        >>> torch.distributed.checkpoint.save_state_dict(
        >>>     state_dict=model_state_dict,
        >>>     storage_writer=fs_storage_writer,
        >>> )

    .. note::
        save_state_dict uses collectives to coordinate writes across ranks.
        For NCCL-based process groups, internal tensor representations of
        objects must be moved to the GPU device before communication takes place.
        In this case, the device used is given by ``torch.cuda.current_device()``
        and it is the user's responsibility to ensure that this is set so that
        each rank has an individual GPU, via ``torch.cuda.set_device()``.
    """
    torch._C._log_api_usage_once("torch.distributed.checkpoint.save")

    if no_dist is None:
        no_dist = not (dist.is_available() and dist.is_initialized())
        if no_dist:
            warnings.warn(
                "Saving with `no_dist` set to True because torch.distributed"
                " is unavailable or uninitialized."
            )

    with _profile():
        storage_writer = cast(
            StorageWriter, _storage_setup(storage_writer, checkpoint_id, reader=False)
        )
        return _save_state_dict(
            _stateful_to_state_dict(state_dict),
            storage_writer,
            process_group,
            coordinator_rank,
            no_dist,
            planner,
        )


def _async_save(
    state_dict: STATE_DICT_TYPE,
    *,
    checkpoint_id: Union[str, os.PathLike, None] = None,
    storage_writer: Optional[StorageWriter] = None,
    planner: Optional[SavePlanner] = None,
    process_group: Optional[dist.ProcessGroup] = None,
    coordinator_rank: int = 0,
    no_dist: bool = False,
) -> Future:
    """Asynchronous version of ``save_state_dict``. This code first de-stages the state_dict on CPU, and then calls
    `save` in a separate thread.

    .. warning::
        This feature is experimental and subject to removal/change.

    Args:
        state_dict (Dict[str, Any]): The state_dict to save.
        checkpoint_id (Union[str, os.PathLike, None]):
            The ID of this checkpoint instance. The meaning of the checkpoint_id
            depends on the storage. It can be a path to a folder or to a file.
            It can also be a key if the storage is a key-value store.
            (Default: ``None``)
        storage_writer (Optional[StorageWriter]):
            Instance of StorageWriter used to perform writes. If this is not
            specified, DCP will automatically infer the writer based on the
            checkpoint_id. If checkpoint_id is also None, an exception will
            be raised. (Default: ``None``)
        planner (Optional[SavePlanner]):
            Instance of SavePlanner. If this is not specificed, the default
            planner will be used. (Default: ``None``)
        process_group (Optional[ProcessGroup]):
            ProcessGroup to be used for cross-rank synchronization.
            (Default: ``None``)
        coordinator_rank (int): Rank to use to coordinate the checkpoint.
            rank0 is used by default. (Default: ``0``)
        no_dist (bool): If ``True``, distributed checkpoint will not save
            in SPMD style. (Default: ``False``)

    Returns:
        Future: A future holding the resultant Metadata object from `save`.

    """
    torch._C._log_api_usage_once("torch.distributed.checkpoint._async_save")

    pg = process_group or _get_default_group()
    assert (
        torch.device("cpu") in pg._device_types  # type: ignore[attr-defined]
    ), "A CPU backend must be enabled for async save; try initializing process group with 'cpu:gloo,cuda:ncc'"

    cpu_state_dict = _offload_state_dict_to_cpu(_stateful_to_state_dict(state_dict))

    executor = ThreadPoolExecutor(max_workers=1)
    f = executor.submit(
        save,
        cpu_state_dict,
        checkpoint_id=checkpoint_id,
        storage_writer=storage_writer,
        planner=planner,
        process_group=process_group,
        coordinator_rank=coordinator_rank,
        no_dist=no_dist,
    )
    f.add_done_callback(lambda f: executor.shutdown(wait=False))

    return f


def _stateful_to_state_dict(state_dict: STATE_DICT_TYPE) -> STATE_DICT_TYPE:
    """Creates a shallow copy of `state_dict` where `state_dict` is called for each Stateful object."""
    stateful_state_dict = {}
    for key, elem in state_dict.items():
        stateful_state_dict[key] = (
            elem.state_dict() if isinstance(elem, Stateful) else elem
        )
    return stateful_state_dict


def _save_state_dict(
    state_dict: STATE_DICT_TYPE,
    storage_writer: StorageWriter,
    process_group: Optional[dist.ProcessGroup] = None,
    coordinator_rank: int = 0,
    no_dist: bool = False,
    planner: Optional[SavePlanner] = None,
) -> Metadata:
    torch._C._log_api_usage_once("torch.distributed.checkpoint.save_state_dict")

    distW = _DistWrapper(process_group, not no_dist, coordinator_rank)
    if planner is None:
        planner = DefaultSavePlanner()
    assert planner is not None

    global_metatadata = None

    def local_step():
        assert planner is not None
        planner.set_up_planner(state_dict, distW.is_coordinator)
        storage_writer.set_up_storage_writer(distW.is_coordinator)
        local_plan = planner.create_local_plan()
        local_plan = storage_writer.prepare_local_plan(local_plan)
        return local_plan

    def global_step(all_local_plans):
        nonlocal global_metatadata

        assert planner is not None
        all_local_plans, global_metatadata = planner.create_global_plan(all_local_plans)
        all_local_plans = storage_writer.prepare_global_plan(all_local_plans)
        return all_local_plans

    central_plan = distW.reduce_scatter("plan", local_step, global_step)

    def write_data():
        assert planner is not None
        final_local_plan = planner.finish_plan(central_plan)
        all_writes = storage_writer.write_data(final_local_plan, planner)

        all_writes.wait()
        return all_writes.value()

    def finish_checkpoint(all_results):
        assert global_metatadata is not None
        storage_writer.finish(metadata=global_metatadata, results=all_results)
        return global_metatadata

    return distW.all_reduce("write", write_data, finish_checkpoint)
