# Copyright (c) 2024 PaddlePaddle Authors. All Rights Reserved.
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

import atexit
import copy
import hashlib
import json
import multiprocessing
import os
import time
from collections import OrderedDict
from enum import Enum

import paddle
import paddle.autograd as imperative_base
import paddle.distributed as dist
from paddle.base import core
from paddle.incubate.tensor.manipulation import (
    async_offload_with_offset,
    create_async_load,
)
from paddle.optimizer.fusion_utils import FusionStorageHelper

from paddlenlp.trainer.trainer_callback import TrainerCallback
from paddlenlp.transformers.model_utils import (
    _add_variant,
    get_parameter_dtype,
    unwrap_model,
)
from paddlenlp.transformers.utils import device_guard
from paddlenlp.utils.env import (
    CONFIG_NAME,
    MODEL_META_NAME,
    PADDLE_OPTIMIZER_NAME,
    PADDLE_WEIGHTS_NAME,
    PREFIX_CHECKPOINT_DIR,
    SCHEDULER_NAME,
    TRAINER_STATE_NAME,
    TRAINING_ARGS_NAME,
)
from paddlenlp.utils.fault_tolerance import FC_DUMP_ERROR, PC_DUMP_ERROR
from paddlenlp.utils.log import logger
from paddlenlp.utils.pdc_sdk import FLASH_DEVICE


def md5(tensor):
    """debug use"""
    numpy_array = tensor.numpy()
    array_bytes = numpy_array.tobytes()
    return hashlib.md5(array_bytes).hexdigest()


class FCTaskType(Enum):
    """
    TaskType defines the type of tasks that can be executed by the FlashCheckpointWorker.
    """

    UPDATE = 0
    PREPARE = 1
    OFFLOAD = 2
    FINISH = 3
    SET_EMA_STATE_DICT = 5


class FCWorkerStatus(Enum):
    IDLE = 0
    OFFLOADING = 1
    DUMPING = 2
    ERROR = 3


def get_fused_param_mappings(optimizer, manipulated_state_dict):
    param_mappings = {}
    ipc_meta_mappings = {}
    index = 0
    sharding_comm_buffers = optimizer._comm_buffer_list
    for buffer in sharding_comm_buffers:
        ipc_meta_mappings[str(index)] = buffer.param_buffer_ipc_meta
        for k, v in manipulated_state_dict.items():
            logger.info(
                f"check vname: {v.name}; buffer._sharding_param_grad_view: {buffer._sharding_param_grad_view.keys()}"
            )
            if v.name in buffer._sharding_param_grad_view:
                assert k not in param_mappings, f"{k} has already been mapped, which is unexpected."
                param_meta = {}
                param_meta["buffer_index"] = str(index)
                param_meta["shape"] = v.shape
                param_meta["name"] = v.name
                param_meta["start"] = buffer._sharding_param_grad_view[v.name]._index
                param_meta["end"] = param_meta["start"] + v._numel()
                param_mappings[k] = param_meta
        index += 1
    assert len(manipulated_state_dict) == len(
        param_mappings
    ), f"manipulated state dict is not fully covered in param mappings, manipulated_state_dict:{manipulated_state_dict.keys()}, param_mappings:{param_mappings.keys()}"
    return param_mappings, ipc_meta_mappings


class FlashEMAProcessor:
    """
    生活在 FC worker 里面的 EMA 处理模块.
    通过 `optimizer_fusion_storage_helper` 以及 `param_fusion_storage_helper` 获取主模型的参数
    """

    def __init__(self, optimizer_fusion_storage_helper, param_fusion_storage_helper, ema_coef):
        self.optimizer_fusion_storage_helper = optimizer_fusion_storage_helper
        self.param_fusion_storage_helper = param_fusion_storage_helper
        self.ema_coef = ema_coef
        (
            self.ema_buffer,
            self.ema_buffer_model_params,
            self.master_min_offset,
            self.master_max_offset,
        ) = self.build_ema_buffer()

    def status(self):
        if self.ema_buffer is None:
            return "[EMA buffer] not initizied"
        opt_md = md5(self.ema_buffer)
        param_md = {k: md5(v) for k, v in self.ema_buffer_model_params.items()}
        return f"[EMA buffer] opt:{opt_md}, param:{param_md}"

    @imperative_base.no_grad()
    def build_ema_buffer(self):
        logger.info("[FC EMA] build ema buffer")
        master_max_offset = max(
            self.optimizer_fusion_storage_helper.master_weights_meta.values(), key=lambda i: i["end"]
        )["end"]
        master_min_offset = min(
            self.optimizer_fusion_storage_helper.master_weights_meta.values(), key=lambda i: i["start"]
        )["start"]
        ema_buffer = paddle.Tensor(
            self.optimizer_fusion_storage_helper.cpu_buffer._slice(master_min_offset, master_max_offset),
            place=paddle.CPUPlace(),
        )
        # ema model params, only works on float32 model weights (aka, moe gates)
        ema_buffer_model_params = {
            k: cpu_buf.clone()
            for k, (cuda_buf, cpu_buf) in self.param_fusion_storage_helper.inited_buffers.items()
            if cuda_buf.dtype == paddle.float32
        }
        return ema_buffer, ema_buffer_model_params, master_min_offset, master_max_offset

    def ema_reset(self):
        self.ema_buffer = None
        self.ema_buffer_modele_params = None

    @imperative_base.no_grad()
    def ema_accumulate(self):
        """
        perform ema update : ` \alpha * EMA + (1-\alpha) + model`
        buid `self.ema_buffer` if necessary
        """
        # logger.info(f'[FC EMA] wait all done, doing EMA w/ coef: {self.ema_coef}, status:{self.status()}')
        # do update: ema = alpha * ema + (1-alpha) * model
        logger.info("[FC EMA] start")
        cpu_master_weights = self.optimizer_fusion_storage_helper.cpu_buffer._slice(
            self.master_min_offset, self.master_max_offset
        ).cpu()
        self.ema_buffer = self.ema_coef * self.ema_buffer + (1 - self.ema_coef) * cpu_master_weights
        # logger.info(f'[FC EMA2] wait all done, doing EMA w/ coef: {self.ema_coef}, status:{self.status()}')
        for index, ema_buf in self.ema_buffer_model_params.items():
            _, cpu_buf = self.param_fusion_storage_helper.inited_buffers[index]
            updated_ema = self.ema_coef * ema_buf + (1 - self.ema_coef) * cpu_buf
            self.ema_buffer_model_params[index] = updated_ema
        logger.info(f"[FC EMA] done, buffer type:{self.ema_buffer.dtype}")

    @imperative_base.no_grad()
    def ema_state_dict(self):
        assert self.optimizer_fusion_storage_helper is not None
        logger.info("[FC EMA] convert ema master weights state dict")
        ema_state_dict = {}
        for k, tensor_meta in self.param_fusion_storage_helper.model_weights_metas.items():
            shape = tensor_meta["shape"]
            name = tensor_meta["name"]
            start = tensor_meta["start"]
            end = tensor_meta["end"]
            if tensor_meta["buffer_index"] not in self.ema_buffer_model_params:
                continue  # non fp32 has no `self.ema_buffer_model_params`
            cpu_buffer = self.ema_buffer_model_params[tensor_meta["buffer_index"]]
            tensor = cpu_buffer._slice(start, end)
            tensor.get_tensor()._set_dims(shape)
            tensor.name = name
            ema_state_dict[k] = tensor
        ema_state_dict_master_weights = {}
        for k, meta in self.optimizer_fusion_storage_helper.master_weights_meta.items():
            t = self.ema_buffer._slice(meta["start"] - self.master_min_offset, meta["end"] - self.master_min_offset)
            t.get_tensor()._set_dims(meta["shape"])
            t.name = meta["name"]
            ema_state_dict_master_weights[k] = t
        ema_state_dict["master_weights"] = ema_state_dict_master_weights
        logger.info("[FC EMA] done covert")
        return ema_state_dict

    def load_ema_state_dict(self, path):
        with device_guard():
            logger.info(f"[FC EMA] load state dict from {path}")
            state_dict = paddle.load(path)
            for k, tensor_meta in self.param_fusion_storage_helper.model_weights_metas.items():
                logger.info(f"[FC EMA] load model weight key={k}")
                start = tensor_meta["start"]
                end = tensor_meta["end"]
                if tensor_meta["buffer_index"] not in self.ema_buffer_model_params:
                    continue  # non fp32 has no `self.ema_buffer_model_params`
                cpu_buffer = self.ema_buffer_model_params[tensor_meta["buffer_index"]]
                tensor = state_dict[k].flatten()
                cpu_buffer[start:end] = tensor

            ema_master = state_dict["master_weights"]
            for k, meta in self.optimizer_fusion_storage_helper.master_weights_meta.items():
                logger.info(f"[FC EMA] load optimizer weight key={k}")
                s = meta["start"] - self.master_min_offset
                e = meta["end"] - self.master_min_offset
                self.ema_buffer[s:e] = ema_master[k]
            logger.info("[FC EMA] done loading")


class ParamFusionStorageHelper:
    def __init__(
        self,
        model_weights_metas,
        buffer_ipc_metas,
    ):
        self.async_loader = create_async_load()
        self.inited_buffers = {}
        self.all_param_numel = 0
        self.model_weights_metas = OrderedDict()
        self.current_offloaded_numel = 0
        self.reset_meta(
            model_weights_metas,
            buffer_ipc_metas,
        )
        self.tasks = []

    @imperative_base.no_grad()
    def reset_meta(
        self,
        model_weights_metas,
        buffer_ipc_metas,
    ):
        self.inited_buffers = {}
        self.all_param_numel = 0
        self.model_weights_metas = OrderedDict()
        if len(model_weights_metas) == 0:
            logger.info("No model states need to save in current worker")
            return

        for k, v in model_weights_metas.items():
            assert isinstance(v, dict), "model_weights_metas must be a dict"
            buffer_index = v["buffer_index"]
            if buffer_index not in self.inited_buffers.keys():
                buffer_tuple = self.init_buffer(buffer_ipc_metas[buffer_index])
                self.inited_buffers[buffer_index] = buffer_tuple
            v["start"] = int(v["start"])
            v["end"] = int(v["end"])
            v["logical_start"] = self.all_param_numel
            self.all_param_numel += v["end"] - v["start"]
            v["logical_end"] = self.all_param_numel
            self.model_weights_metas[k] = v

    def init_buffer(self, meta):
        cuda_buffer = paddle.to_tensor(paddle.base.core.LoDTensor._new_shared_cuda(meta))
        cpu_buffer = cuda_buffer.pin_memory()
        return (cuda_buffer, cpu_buffer)

    @imperative_base.no_grad()
    def sync_partial_param(self, numel_to_sync):
        assert (
            self.current_offloaded_numel + numel_to_sync <= self.all_param_numel
        ), f"numel_to_sync: {numel_to_sync}, current_offloaded_numel: {self.current_offloaded_numel}, all_param_numel: {self.all_param_numel}"
        next_offload_index = 0
        meta_keys_in_order = list(self.model_weights_metas.keys())
        for i, k in enumerate(meta_keys_in_order):
            if self.current_offloaded_numel >= self.model_weights_metas[k]["logical_end"]:
                continue
            next_offload_index = i
            break

        while numel_to_sync > 0:
            offloading_param_key = meta_keys_in_order[next_offload_index]
            offloading_param_meta = self.model_weights_metas[offloading_param_key]
            logical_offload_param_start = self.current_offloaded_numel
            logical_offload_param_end = min(
                offloading_param_meta["logical_end"], logical_offload_param_start + numel_to_sync
            )
            actual_offload_start = (
                logical_offload_param_start - offloading_param_meta["logical_start"]
            ) + offloading_param_meta["start"]
            actual_offload_end = (
                logical_offload_param_end - offloading_param_meta["logical_end"]
            ) + offloading_param_meta["end"]
            actual_offload_size = actual_offload_end - actual_offload_start
            current_param_buffer = self.inited_buffers[offloading_param_meta["buffer_index"]][0]
            current_param_cpu_buffer = self.inited_buffers[offloading_param_meta["buffer_index"]][1]
            task = async_offload_with_offset(
                src_tensor=current_param_buffer,
                dst_tensor=current_param_cpu_buffer,
                src_offset=actual_offload_start,
                dst_offset=actual_offload_start,
                offload_size=actual_offload_size,
                async_loader=self.async_loader,
            )
            self.tasks.append(task)
            self.current_offloaded_numel += actual_offload_size
            numel_to_sync -= actual_offload_size
            next_offload_index += 1

    def wait_all(self):
        if len(self.tasks) == 0:
            return
        last_task = self.tasks.pop(-1)
        while len(self.tasks) > 0:
            task = self.tasks.pop(0)
            task.cuda_wait()
        last_task.cpu_wait()
        self.current_offloaded_numel = 0

    def state_dict(self):
        state_dict = {}
        for k, v in self.model_weights_metas.items():
            state_dict[k] = self.restore_tensor_from_meta(v)
        return state_dict

    @imperative_base.no_grad()
    def restore_tensor_from_meta(self, tensor_meta):
        shape = tensor_meta["shape"]
        name = tensor_meta["name"]
        start = tensor_meta["start"]
        end = tensor_meta["end"]
        cpu_buffer = self.inited_buffers[tensor_meta["buffer_index"]][1]
        tensor = cpu_buffer._slice(start, end)
        tensor.get_tensor()._set_dims(shape)
        tensor.name = name
        return tensor


class FlashCheckpointCallback(TrainerCallback):
    """
    call FlashCheckpointManager during training in following order:

    on_step_end:
        *  call get_idle_worker_for_saving, set manager.current_worker
        *  call maybe_update_flash_checkpoint_worker

    * on_substep_end(call `gradient_accumulate` times): call flash_checkpoint_pipeline_hook (in non-pp model)
    * (when offload done, dump model)
    on_optimizer_begin: call sync_offload_status, unset set manager.current_worker
    """

    def __init__(self, args, flash_checkpoint_manager, timer, sharding_io):
        self.manager = flash_checkpoint_manager
        self.runtime_timer = timer
        self.user_file_list = []
        self.manipulated_state_dict = None
        self.manipulated_config_to_save = None
        self.manipulated_weight_suffix = None
        self.model_meta = None
        self.sharding_io = sharding_io
        assert (
            args.flash_save_steps % args.flash_ema_interval == 0
        ), f"flash_save_steps:{args.flash_save_steps} must be divisible by flash_ema_interval:{args.flash_ema_interval}"
        assert (
            args.save_steps % args.flash_ema_interval == 0
        ), f"save_steps:{args.save_steps} must be divisible by flash_ema_interval:{args.flash_ema_interval}"
        self.flash_ema_interval = args.flash_ema_interval
        if args.flash_save_ema_coef:
            assert args.flash_workers_num == 1, "[FC EMA] not support #worker > 1"

    def on_substep_end(self, args, state, control, **kwargs):
        self.manager.flash_checkpoint_pipeline_hook(0)

    def on_optimizer_begin(self, args, state, control, **kwargs):
        if args.enable_flash_save_mode and self.manager.current_worker is not None:
            logger.info("Start syncing flash checkpoints")
            self.manager.sync_offload_status()
            logger.info("Synced flash checkpoints.")

    def on_step_end(self, args, state, control, model, lr_scheduler, optimizer, **kwargs):
        self.manager.flash_checkpoint_pipeline_hook(0)
        logger.info(
            f"check coef: {args.flash_save_ema_coef} {control.should_save}, {state.global_step}, {self.flash_ema_interval}"
        )
        if not control.should_save:
            if args.flash_save_ema_coef and state.global_step % self.flash_ema_interval == 0:
                self.maybe_update_flash_checkpoint_worker(args, model, optimizer)
                self.manager.get_idle_worker_for_saving()  # prepare for dumping
        else:
            self.runtime_timer.start("checkpoint saving time")
            self.maybe_update_flash_checkpoint_worker(args, model, optimizer)
            checkpoint_folder = f"{PREFIX_CHECKPOINT_DIR}-{state.global_step}"
            save_infos = self._get_save_infos_based_on_steps(state, args, checkpoint_folder)
            non_cached_objects = (lr_scheduler.state_dict(), state)
            self.manager.get_idle_worker_for_saving((save_infos, non_cached_objects))
            self.runtime_timer.stop()
            control.should_save = False  # avoid regular saving

    def _get_save_infos_based_on_steps(self, state, args, checkpoint_folder):
        flash_checkpoint_dir = None
        persistent_checkpoint_dir = None
        if args.flash_save_steps > 0 and state.global_step % args.flash_save_steps == 0:
            flash_checkpoint_dir = os.path.join(FLASH_DEVICE, checkpoint_folder)
        if args.save_steps > 0 and state.global_step % args.save_steps == 0:
            persistent_checkpoint_dir = os.path.join(args.output_dir, checkpoint_folder)
        return (flash_checkpoint_dir, persistent_checkpoint_dir)

    def maybe_update_flash_checkpoint_worker(self, args, model, optimizer):
        if optimizer.fused_buffer_version == self.manager.cache_version:
            return

        logger.info("Flash checkpoint workers need upgrade.")
        self._cache_meta_for_sharded_save(model)
        param_mappings, ipc_meta_mappings = get_fused_param_mappings(optimizer, self.manipulated_state_dict)
        optimizer_states_meta = (
            optimizer.fused_states_accumulators_meta,
            optimizer.fused_states_master_weights_meta,
            None,
            optimizer.fused_states_buffer_ipc_meta,
        )
        model_states_meta = (param_mappings, ipc_meta_mappings)
        optimizer_states_name_path = _add_variant(PADDLE_OPTIMIZER_NAME, args.optimizer_name_suffix)
        model_states_name_path = _add_variant(PADDLE_WEIGHTS_NAME, self.manipulated_weight_suffix)

        dynamic_objecs = {}
        dynamic_objecs["optimizer_states_meta"] = optimizer_states_meta
        dynamic_objecs["model_states_meta"] = model_states_meta
        dynamic_objecs["optimizer_states_name_path"] = optimizer_states_name_path
        dynamic_objecs["model_states_name_path"] = model_states_name_path

        static_objects = {}
        static_objects["model_config"] = self.manipulated_config_to_save
        static_objects["training_args"] = args
        static_objects["model_meta"] = self.model_meta
        static_objects["user_file"] = self.user_file_list

        self.manager.update_flash_workers(optimizer.fused_buffer_version, dynamic_objecs, static_objects)

    def _cache_meta_for_sharded_save(self, model):
        logger.info("Start caching metas for sharded save...")
        (
            self.manipulated_state_dict,
            self.manipulated_config_to_save,
            self.manipulated_weight_suffix,
        ) = self.sharding_io.manipulate_state_dict_and_config(model, merge_tensor_parallel=False)
        logger.info("Cache manipulated static dict done.")
        if self.manipulated_config_to_save is None:
            model_to_save = unwrap_model(model)
            dtype = get_parameter_dtype(model_to_save)
            model_to_save.config.dtype = str(dtype).split(".")[1]
            self.manipulated_config_to_save = copy.deepcopy(model_to_save.config)
            self.manipulated_config_to_save.architectures = [model_to_save.__class__.__name__]
            self.manipulated_config_to_save = self.manipulated_config_to_save.to_json_string(use_diff=True)
            logger.info("Cache manipulated model config done")
        self.model_meta = self.sharding_io.gather_distributed_model_meta()
        logger.info("Cache distributed model meta done.")


class FlashCheckpointManager:
    def __init__(self, worker_num, pipeline_hooks_capacity, capacity_usage, ema_coef=None):
        assert worker_num > 0, "worker_num must be greater than 0"
        assert capacity_usage <= 1.0, "capacity_usage must be less than or equal to 1.0"
        self.cache_version = 0
        self.worker_num = worker_num
        self.workers = []
        self.processes = []
        self.current_worker = None
        self.device_id = int(os.getenv("FLAGS_selected_gpus"))
        self.pipeline_hooks_steps = max(int(pipeline_hooks_capacity * capacity_usage), 1)
        logger.info(
            f"[FC manager] pipeline hooks capacity: {pipeline_hooks_capacity}; "
            f"pipeline hooks steps for offloading: {self.pipeline_hooks_steps} "
            f"ema coefficient: {ema_coef} "
        )
        self.current_pipeline_hook_step = 0
        ctx = multiprocessing.get_context("spawn")
        for i in range(worker_num):
            worker_task_queue = ctx.Queue()
            worker_status = ctx.Value("i", FCWorkerStatus.IDLE.value)
            worker_version = ctx.Value("i", 0)
            worker = FlashCheckpointWorker(
                i,
                self.device_id,
                dist.get_rank(),
                self.pipeline_hooks_steps,
                worker_task_queue,
                worker_status,
                worker_version,
                ema_coef,
            )
            p = ctx.Process(target=worker_loop, args=(worker,))
            p.start()
            self.workers.append(worker)
            self.processes.append(p)
        self.ready_to_save = False
        atexit.register(self.terminate_workers)

    def set_ema_state_dict(self, path):
        logger.info(f"[FC manager] setting EMA state dict: {path}")
        for worker in self.workers:
            assert worker.status.value == FCWorkerStatus.IDLE.value, "[FC manager] worker should be idle, when "
            worker.task_queue.put((FCTaskType.SET_EMA_STATE_DICT, path))
        logger.info("[FC manager] done setting EMA state dict")

    def update_flash_workers(self, new_version, dynamic_objecs, static_objects):
        self.report_error_worker()
        self.cache_version = new_version
        assert self.current_worker is None, "[FC manager] current_worker must be None"
        task = (FCTaskType.UPDATE, [self.cache_version, dynamic_objecs, static_objects])
        logger.info(f"[FC manager] updating flash workers, verison: {self.cache_version}")
        for worker in self.workers:
            worker.task_queue.put(task)
        logger.info("[FC manager] waiting workers update done")
        for worker in self.workers:
            while worker.version.value != self.cache_version:
                logger.info(
                    f"[FC manager] waiting worker{worker.worker_id} update. worker version: {worker.version.value}, expected version: {self.cache_version}"
                )
                time.sleep(1)
            logger.info(
                f"[FC manager] worker{worker.worker_id} updated. worker version: {worker.version.value}, expected version: {self.cache_version}"
            )
        logger.info("[FC manager] update all flash workers done")
        self.ready_to_save = True

    def get_idle_worker_for_saving(self, save_infos_and_non_cached_objects=None):
        """
        if `save_infos_and_non_cached_objects` is None, do offload and dumping.
        """
        self.report_error_worker()
        assert self.current_worker is None, "[FC manager] current_worker must be None"
        found_worker = False
        while True:
            for worker in self.workers:
                if worker.status.value == FCWorkerStatus.IDLE.value:
                    self.current_worker = worker
                    found_worker = True
                    break
            if found_worker:
                break
            logger.info("[FC manager] Waiting for idle worker..., consider increse `save-step` or `global-batch-size`")
            time.sleep(1)
        task = (FCTaskType.PREPARE, save_infos_and_non_cached_objects)
        logger.info(
            f"[FC manager] before putting task for prepare, dumping={save_infos_and_non_cached_objects is not None}"
        )
        self.current_worker.task_queue.put(task)
        logger.info(
            f"[FC manager] after putting task for prepare, dumping={save_infos_and_non_cached_objects is not None}"
        )

    def sync_offload_status(self):
        self.report_error_worker()
        assert self.current_worker is not None, "[FC manager] current_worker must not be None"
        while True:
            logger.info("[FC manager] Waiting current worker offloading done.")
            if self.current_worker.status.value == FCWorkerStatus.OFFLOADING.value:
                time.sleep(1)
            else:
                logger.info("[FC manager] Current worker offloading done")
                break
        self.current_pipeline_hook_step = 0
        self.current_worker = None

    def report_error_worker(self):
        for worker in self.workers:
            if worker.status.value == FCWorkerStatus.ERROR.value:
                logger.error(f"[FC manager] Worker{worker.worker_id} encountered error.")
                raise RuntimeError(f"{PC_DUMP_ERROR}")

    def flash_checkpoint_pipeline_hook(self, hook_id):
        if self.current_worker is None:
            return
        if self.current_pipeline_hook_step == self.pipeline_hooks_steps:
            return
        if not self.ready_to_save:
            return
        task = (FCTaskType.OFFLOAD, None)
        self.current_worker.task_queue.put(task)
        self.current_pipeline_hook_step += 1

    def finalize(self):
        # clean up if the final step need to save
        if self.current_worker is not None:
            logger.info("[FC manager] clean up last step saving")
            # trigger offload
            for i in range(self.pipeline_hooks_steps):
                self.flash_checkpoint_pipeline_hook(i)
            self.sync_offload_status()
        self.ready_to_save = False
        self.terminate_workers()

    def terminate_workers(self):
        for worker in self.workers:
            task = (FCTaskType.FINISH, None)
            worker.task_queue.put(task)
        for p in self.processes:
            p.join()


def worker_loop(worker):
    worker.run()


class FlashCheckpointWorker:
    def __init__(self, worker_id, device_id, global_rank, offload_chunks, task_queue, status, version, ema_coef=None):
        super().__init__()
        self.worker_id = worker_id
        self.device_id = device_id
        self.global_rank = global_rank
        self.offload_chunks = offload_chunks
        self.task_queue = task_queue
        self.status = status
        self.version = version
        self.ema_coef = ema_coef

        # for dynamic objects saving
        self.optimizer_fusion_storage_helper = None
        self.param_fusion_storage_helper = None
        self.all_numel = 0
        self.chunk_size_in_numel = 0
        self.offloaded_numels = 0
        self.optimizer_states_name_path = None
        self.model_states_name_path = None

        # for static objects saving
        self.model_config_content = None
        self.training_args_content = None
        self.model_meta_content = None
        self.user_file_list = None

        # for non cached objects saving
        # TODO(@gexiao): remove lr scheduler saves
        self.lr_scheduler = None
        self.trainer_state = None

        # for dumping
        self.flash_save_dir = None
        self.persistent_save_dir = None
        self.flash_ema_processor = None

    def process_update_task(self, updates):
        """
        sync operation, main process should wait
        """
        version, dynamic_objecs, static_objects = updates
        optimizer_states_meta = dynamic_objecs["optimizer_states_meta"]
        model_states_meta = dynamic_objecs["model_states_meta"]
        self.optimizer_states_name_path = dynamic_objecs["optimizer_states_name_path"]
        self.model_states_name_path = dynamic_objecs["model_states_name_path"]
        self.build_fusion_storage_helper(optimizer_states_meta, model_states_meta)

        self.model_config_content = static_objects["model_config"]
        self.training_args_content = static_objects["training_args"]
        self.model_meta_content = static_objects["model_meta"]
        self.user_file_list = static_objects["user_file"]

        self.manage_offload_chunk()

        self.version.value = version

    def process_prepare_task(self, prepares):
        self.offloaded_numels = 0
        self.status.value = FCWorkerStatus.OFFLOADING.value
        if prepares is None:  # when `prepares` is None, not dumping
            return
        save_infos, non_cached_objects = prepares
        self.flash_save_dir, self.persistent_save_dir = save_infos
        self.lr_scheduler, self.trainer_state = non_cached_objects

    def process_offload_task(self, dump):
        actual_offload_size = (
            min(self.offloaded_numels + self.chunk_size_in_numel, self.all_numel) - self.offloaded_numels
        )
        # Scene1: offload optimizer only
        if self.offloaded_numels + actual_offload_size <= self.optimizer_fusion_storage_helper.buffer_length:
            self.optimizer_fusion_storage_helper.sync_partial_param(
                start=self.offloaded_numels, end=self.offloaded_numels + actual_offload_size
            )
        # Scene2: offload optimizer and param
        elif self.offloaded_numels < self.optimizer_fusion_storage_helper.buffer_length:
            self.optimizer_fusion_storage_helper.sync_partial_param(
                start=self.offloaded_numels, end=self.optimizer_fusion_storage_helper.buffer_length
            )
            self.param_fusion_storage_helper.sync_partial_param(
                numel_to_sync=(
                    actual_offload_size - (self.optimizer_fusion_storage_helper.buffer_length - self.offloaded_numels)
                )
            )
        # Scene3: offload param only
        else:
            self.param_fusion_storage_helper.sync_partial_param(numel_to_sync=actual_offload_size)
        self.offloaded_numels += actual_offload_size

        # wait tasks done and change status to DUMPING at the last chunk
        if self.offloaded_numels == self.all_numel:
            self.optimizer_fusion_storage_helper.wait_all()
            self.param_fusion_storage_helper.wait_all()
            if self.ema_coef:
                self.flash_ema_processor.ema_accumulate()
            self.status.value = FCWorkerStatus.DUMPING.value

        # continue to process dumping task at the last chunk
        if self.offloaded_numels == self.all_numel:
            if dump:
                need_report_error = self.process_dump_task()
            else:
                need_report_error = False
            self.offloaded_numels = 0
            self.status.value = FCWorkerStatus.ERROR.value if need_report_error else FCWorkerStatus.IDLE.value

    def process_dump_task(self):
        """
        dump saved objects to either flash device or persistent device
        Notice:
        1. If dumping to flash device failed, the process will move on for other task
        2. If dumping to persistent device failed, the process will change status to fail, and the main process will raise Error.
        """
        need_report_error = False
        if self.flash_save_dir:
            try:
                self.process_dump_task_impl(self.flash_save_dir)
                logger.info(f"[FC worker{self.worker_id}] Dumping to flash device done: {self.flash_save_dir}")
            except Exception as e:
                logger.error(f"{FC_DUMP_ERROR} [FC worker{self.worker_id}] Failed to dump to flash device: {e}")
        if self.persistent_save_dir:
            try:
                self.process_dump_task_impl(self.persistent_save_dir)
                logger.info(
                    f"[FC worker{self.worker_id}] Dumping to persistent device done: {self.persistent_save_dir}"
                )
            except Exception as e:
                logger.error(f"[FC worker{self.worker_id}] Failed to dump to persistent device: {e}")
                need_report_error = True
        return need_report_error

    def process_dump_task_impl(self, output_dir):
        os.makedirs(output_dir, exist_ok=True)
        # Step1: save static objects
        if self.device_id == 0:
            # Step1.1: save model config
            json_file_path = os.path.join(output_dir, CONFIG_NAME)
            with open(json_file_path, "w", encoding="utf-8") as writer:
                writer.write(self.model_config_content)

            # Step1.2: save training args
            args_file_path = os.path.join(output_dir, TRAINING_ARGS_NAME)
            paddle.save(self.training_args_content, args_file_path)

            # Step1.3: save model meta
            model_meta_path = os.path.join(output_dir, MODEL_META_NAME)
            with open(model_meta_path, "w") as f:
                json.dump(self.model_meta_content, f)

            # Step1.4: save user files
            for (file_name, file_content) in self.user_file_list:
                file_path = os.path.join(output_dir, file_name)
                with open(file_path, "w") as f:
                    f.write(file_content)

        # Step2: save dynamic objects
        # Step2.1: save model states
        model_states_name_path = os.path.join(output_dir, self.model_states_name_path)
        paddle.save(self.param_fusion_storage_helper.state_dict(), model_states_name_path)

        # Step2.2: save optimizer states
        optimizer_state_name_path = os.path.join(output_dir, self.optimizer_states_name_path)
        paddle.save(self.optimizer_fusion_storage_helper.state_dict(), optimizer_state_name_path)
        if self.ema_coef:
            ema_name_path = os.path.join(output_dir, self.optimizer_states_name_path).replace("optimizer", "ema")
            paddle.save(self.flash_ema_processor.ema_state_dict(), ema_name_path)
        # Step2.3: save LR Scheduler (To be removed)
        lr_state_name_path = os.path.join(output_dir, SCHEDULER_NAME)
        if self.device_id == 0:
            paddle.save(self.lr_scheduler, lr_state_name_path)

        # Step2.4: save TrainerState
        trainer_state_name_path = os.path.join(output_dir, TRAINER_STATE_NAME)
        if self.device_id == 0:
            self.trainer_state.save_to_json(trainer_state_name_path)

        # Step3: dump save signals
        saved_signal_path = os.path.join(output_dir, f"saved_signal_{self.global_rank}")
        with open(saved_signal_path, mode="w+") as f:
            f.write("1")

    def run(self):
        core.set_cuda_current_device_id(self.device_id)
        paddle.set_device(f"gpu:{self.device_id}")
        logger.info(f"[FC worker{self.worker_id}] Worker{self.worker_id} started.")
        ema_ckpt_path = None
        save_info_tuple = None  # save dir...
        try:
            while True:
                task = self.task_queue.get()
                task_type, task_body = task
                # logger.info(f'[FC worker{self.worker_id}] Received a new task of type {task_type}., ema:{self.flash_ema_processor.status() if self.flash_ema_processor is not None else None}')
                if task_type == FCTaskType.FINISH:
                    logger.info(f"[FC worker{self.worker_id}] Flash checkpoint worker{self.worker_id} exit")
                    break
                elif task_type == FCTaskType.UPDATE:
                    self.process_update_task(task_body)
                    self.flash_ema_processor = FlashEMAProcessor(  # 在 updte task 后刷新 EMA buffer
                        self.optimizer_fusion_storage_helper, self.param_fusion_storage_helper, self.ema_coef
                    )
                    if ema_ckpt_path is not None:  # update ema if needed
                        self.flash_ema_processor.load_ema_state_dict(ema_ckpt_path)
                    ema_ckpt_path = None
                elif task_type == FCTaskType.PREPARE:
                    save_info_tuple = task_body
                    self.process_prepare_task(task_body)
                elif task_type == FCTaskType.OFFLOAD:
                    self.process_offload_task(dump=save_info_tuple is not None)
                elif task_type == FCTaskType.SET_EMA_STATE_DICT:
                    ema_ckpt_path = task_body  # mark ema state dict path
                else:
                    raise ValueError(f"[FC worker{self.worker_id}] Unknown task type: {task_type}")
        except Exception as e:
            import traceback

            logger.info(f"[FC worker{self.worker_id}] failed!!, Exception:{e}\n Traceback:{traceback.format_exc()}\n")
            raise e

    def build_fusion_storage_helper(self, optimizer_states_meta, model_states_meta):
        (
            accumulators_meta,
            master_weights_meta,
            merged_model_params_meta,
            buffer_ipc_meta,
        ) = optimizer_states_meta
        if self.optimizer_fusion_storage_helper is None:
            self.optimizer_fusion_storage_helper = FusionStorageHelper(
                accumulators_meta,
                master_weights_meta,
                merged_model_params_meta,
                buffer_ipc_meta,
            )
        else:
            self.optimizer_fusion_storage_helper.reset_meta(
                accumulators_meta,
                master_weights_meta,
                merged_model_params_meta,
                buffer_ipc_meta,
            )
        model_param_mappings, model_ipc_meta_mappings = model_states_meta
        if self.param_fusion_storage_helper is None:
            self.param_fusion_storage_helper = ParamFusionStorageHelper(model_param_mappings, model_ipc_meta_mappings)
        else:
            self.param_fusion_storage_helper.reset_meta(model_param_mappings, model_ipc_meta_mappings)

    def manage_offload_chunk(self):
        # TODO(@gexiao): more precise slice for different dtype
        optimizer_offload_numel = self.optimizer_fusion_storage_helper.buffer_length
        param_offload_numel = self.param_fusion_storage_helper.all_param_numel
        self.all_numel = optimizer_offload_numel + param_offload_numel
        self.chunk_size_in_numel = (self.all_numel - 1) // self.offload_chunks + 1
        logger.info(
            f"[FC worker{self.worker_id}] All numel: {self.all_numel}, Offload chunks: {self.offload_chunks}, Chunk size: {self.chunk_size_in_numel}]"
        )