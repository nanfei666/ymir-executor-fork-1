"""
utils function for ymir and yolov5
"""
import glob
import os
import os.path as osp
import shutil
from typing import Any, List

import numpy as np
import torch
import yaml
from easydict import EasyDict as edict
from nptyping import NDArray, Shape, UInt8
from packaging.version import Version
from ymir_exc import monitor
from ymir_exc import result_writer as rw
from ymir_exc.util import YmirStage, get_bool, get_weight_files, get_ymir_process

from models.common import DetectMultiBackend
from utils.augmentations import letterbox
from utils.general import check_img_size, non_max_suppression, scale_coords
from utils.torch_utils import select_device

BBOX = NDArray[Shape['*,4'], Any]
CV_IMAGE = NDArray[Shape['*,*,3'], UInt8]


def get_weight_file(cfg: edict) -> str:
    """
    return the weight file path by priority
    find weight file in cfg.param.model_params_path or cfg.param.model_params_path
    """
    weight_files = get_weight_files(cfg, suffix=('.pt'))
    # choose weight file by priority, best.pt > xxx.pt
    for p in weight_files:
        if p.endswith('best.pt'):
            return p

    if len(weight_files) > 0:
        return max(weight_files, key=osp.getctime)

    return ""


class YmirYolov5(torch.nn.Module):
    """
    used for mining and inference to init detector and predict.
    """
    def __init__(self, cfg: edict, task='infer'):
        super().__init__()
        self.cfg = cfg
        if cfg.ymir.run_mining and cfg.ymir.run_infer:
            # multiple task, run mining first, infer later
            if task == 'infer':
                self.task_idx = 1
            elif task == 'mining':
                self.task_idx = 0
            else:
                raise Exception(f'unknown task {task}')

            self.task_num = 2
        else:
            self.task_idx = 0
            self.task_num = 1

        self.gpu_id: str = str(cfg.param.get('gpu_id', '0'))
        device = select_device(self.gpu_id)
        self.gpu_count: int = len(self.gpu_id.split(',')) if self.gpu_id else 0
        self.batch_size_per_gpu: int = int(cfg.param.get('batch_size_per_gpu', 4))
        self.num_workers_per_gpu: int = int(cfg.param.get('num_workers_per_gpu', 4))
        self.pin_memory: bool = get_bool(cfg, 'pin_memory', False)
        self.batch_size: int = self.batch_size_per_gpu * self.gpu_count
        self.model = self.init_detector(device)
        self.model.eval()
        self.device = device
        self.class_names: List[str] = cfg.param.class_names
        self.stride = self.model.stride
        self.conf_thres: float = float(cfg.param.conf_thres)
        self.iou_thres: float = float(cfg.param.iou_thres)

        img_size = int(cfg.param.img_size)
        imgsz = [img_size, img_size]
        imgsz = check_img_size(imgsz, s=self.stride)

        self.model.warmup(imgsz=(1, 3, *imgsz), half=False)  # warmup
        self.img_size: List[int] = imgsz

    def forward(self, x, nms=False):
        pred = self.model(x)
        if not nms:
            return pred

        pred = non_max_suppression(pred,
                                   conf_thres=self.conf_thres,
                                   iou_thres=self.iou_thres,
                                   classes=None,  # not filter class_idx
                                   agnostic=False,
                                   max_det=100)
        return pred

    def init_detector(self, device: torch.device) -> DetectMultiBackend:
        weights = get_weight_file(self.cfg)

        if not weights:
            raise Exception("no weights file specified!")

        data_yaml = osp.join(self.cfg.ymir.output.root_dir, 'data.yaml')
        model = DetectMultiBackend(
            weights=weights,
            device=device,
            dnn=False,  # not use opencv dnn for onnx inference
            data=data_yaml)  # dataset.yaml path

        return model

    def predict(self, img: CV_IMAGE,nms=True) -> NDArray:
        """
        predict single image and return bbox information
        img: opencv BGR, uint8 format
        """
        # preprocess: padded resize
        img1 = letterbox(img, self.img_size, stride=self.stride, auto=True)[0]

        # preprocess: convert data format
        img1 = img1.transpose((2, 0, 1))[::-1]  # HWC to CHW, BGR to RGB
        img1 = np.ascontiguousarray(img1)
        img1 = torch.from_numpy(img1).to(self.device)

        img1 = img1 / 255  # 0 - 255 to 0.0 - 1.0
        img1.unsqueeze_(dim=0)  # expand for batch dim
        pred = self.forward(img1, nms)

        result = []
        for det in pred:
            if len(det):
                # Rescale boxes from img_size to img size
                det[:, :4] = scale_coords(img1.shape[2:], det[:, :4], img.shape).round()
                result.append(det)

        # xyxy, conf, cls
        if len(result) > 0:
            tensor_result = torch.cat(result, dim=0)
            numpy_result = tensor_result.data.cpu().numpy()
        else:
            numpy_result = np.zeros(shape=(0, 6), dtype=np.float32)

        return numpy_result

    def infer(self, img: CV_IMAGE) -> List[rw.Annotation]:
        anns = []
        result = self.predict(img)

        for i in range(result.shape[0]):
            xmin, ymin, xmax, ymax, conf, cls = result[i, :6].tolist()
            ann = rw.Annotation(class_name=self.class_names[int(cls)],
                                score=conf,
                                box=rw.Box(x=int(xmin), y=int(ymin), w=int(xmax - xmin), h=int(ymax - ymin)))

            anns.append(ann)

        return anns

    def write_monitor_logger(self, stage: YmirStage, p: float):
        monitor.write_monitor_logger(
            percent=get_ymir_process(stage=stage, p=p, task_idx=self.task_idx, task_num=self.task_num))


def convert_ymir_to_yolov5(cfg: edict):
    """
    convert ymir format dataset to yolov5 format
    generate data.yaml for training/mining/infer
    """

    data = dict(path=cfg.ymir.output.root_dir, nc=len(cfg.param.class_names), names=cfg.param.class_names)
    for split, prefix in zip(['train', 'val', 'test'], ['training', 'val', 'candidate']):
        src_file = getattr(cfg.ymir.input, f'{prefix}_index_file')
        if osp.exists(src_file):
            shutil.copy(src_file, f'{cfg.ymir.output.root_dir}/{split}.tsv')

        data[split] = f'{split}.tsv'

    with open(osp.join(cfg.ymir.output.root_dir, 'data.yaml'), 'w') as fw:
        fw.write(yaml.safe_dump(data))


def write_ymir_training_result(cfg: edict, map50: float = 0.0, epoch: int = 0, weight_file: str = ""):
    YMIR_VERSION = os.getenv('YMIR_VERSION', '1.2.0')
    if Version(YMIR_VERSION) >= Version('1.2.0'):
        _write_latest_ymir_training_result(cfg, float(map50), epoch, weight_file)
    else:
        _write_ancient_ymir_training_result(cfg, float(map50))


def _write_latest_ymir_training_result(cfg: edict, map50: float, epoch: int, weight_file: str) -> int:
    """
    for ymir>=1.2.0
    cfg: ymir config
    map50: map50
    epoch: stage
    weight_file: saved weight files, empty weight_file will save all files

    1. save weight file for each epoch.
    2. save weight file for last.pt, best.pt and other config file
    3. save weight file for best.onnx, no valid map50, attach to stage f"{model}_last_and_best"
    """
    model = cfg.param.model
    # use `rw.write_training_result` to save training result
    if weight_file:
        rw.write_model_stage(stage_name=f"{model}_{epoch}", files=[osp.basename(weight_file)], mAP=float(map50))
    else:
        # save other files with
        files = [
            osp.basename(f) for f in glob.glob(osp.join(cfg.ymir.output.models_dir, '*')) if not f.endswith('.pt')
        ] + ['last.pt', 'best.pt']

        training_result_file = cfg.ymir.output.training_result_file
        if osp.exists(training_result_file):
            with open(training_result_file, 'r') as f:
                training_result = yaml.safe_load(stream=f)

            map50 = max(training_result.get('map', 0.0), map50)
        rw.write_model_stage(stage_name=f"{model}_last_and_best", files=files, mAP=float(map50))
    return 0


def _write_ancient_ymir_training_result(cfg: edict, map50: float) -> None:
    """
    for 1.0.0 <= ymir <=1.1.0
    """

    files = [osp.basename(f) for f in glob.glob(osp.join(cfg.ymir.output.models_dir, '*'))]
    training_result_file = cfg.ymir.output.training_result_file
    if osp.exists(training_result_file):
        with open(training_result_file, 'r') as f:
            training_result = yaml.safe_load(stream=f)

        training_result['model'] = files
        training_result['map'] = max(float(training_result.get('map', 0)), map50)
    else:
        training_result = {'model': files, 'map': float(map50), 'stage_name': cfg.param.model}

    with open(training_result_file, 'w') as f:
        yaml.safe_dump(training_result, f)
