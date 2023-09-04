import argparse
import os
import sys
import shutil
from pathlib import Path

FILE = Path(__file__).resolve()
ROOT = FILE.parents[0]  # YOLOv5 root directory
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))  # add ROOT to PATH
ROOT = Path(os.path.relpath(ROOT, Path.cwd()))  # relative

import cv2
import yaml
import torch
from utils.datasets import LoadImages
from models.yolo import Model
from utils.general import (LOGGER, check_file, check_img_size, check_imshow, check_requirements, colorstr,
                           increment_path, non_max_suppression, print_args, scale_coords, strip_optimizer, xyxy2xywh)
from utils.plots import Annotator, colors, save_one_box
from utils.torch_utils import time_sync

try:
    import torch_mlu.core.mlu_model as ct
    import torch_mlu.core.mlu_quantize as mlu_quantize
except:
    print('\033[0;31mimport torch_mlu failed in {}!!!\033[0m'.format(__file__))

@torch.no_grad()
def run_detect(opt):
    if opt.device == 'mlu':
        ct.set_cnml_enabled(False)

    if not os.path.exists(opt.save_dir):
        os.mkdir(opt.save_dir)

    if opt.device != 'cpu' and opt.device != 'mlu':
        if torch.cuda.is_available():
            device = torch.device('cuda:0')
        else:
            device = torch.device('cpu')
    else:
        device = torch.device(opt.device)
    
    with open(opt.data, errors='ignore') as f:
        ydata = yaml.safe_load(f)
    nc = ydata['nc']
    names = ydata['names']

    model = Model(opt.cfg, ch=3, nc=nc).eval()#.to(device)  # create
    if device.type == 'mlu':
        model = mlu_quantize.adaptive_quantize(model=model, steps_per_epoch=2, bitwidth=16)
    ckpt = torch.load(opt.weights)
    # ckpt = torch.load(opt.weights, map_location = device)
    # torch.save(ckpt, opt.weights, _use_new_zipfile_serialization = False)
    state_dict = ckpt['model'].float().state_dict()
    model.load_state_dict(state_dict, strict=False)
    model.to(device)

    # model.fuse()

    stride = int(model.stride.max())
    dataset = LoadImages(opt.source, img_size=opt.imgsz, stride=stride, auto=False)

    # Run inference
    dt, seen = [0.0, 0.0, 0.0], 0
    empty_box = 0
    for path, im, im0s, vid_cap, s in dataset:
        # label_path = path.replace('tif', 'txt')

        t1 = time_sync()
        im = torch.from_numpy(im).to(device)
        im = im.half() if opt.half else im.float()  # uint8 to fp16/32
        im /= 255  # 0 - 255 to 0.0 - 1.0
        if len(im.shape) == 3:
            im = im[None]  # expand for batch dim
        t2 = time_sync()
        dt[0] += t2 - t1

        # Inference
        visualize = increment_path(opt.save_dir / Path(path).stem, mkdir=True) if opt.visualize else False
        
        with torch.no_grad():
            pred, _ = model(im, augment=opt.augment, visualize=visualize)
        t3 = time_sync()
        dt[1] += t3 - t2

        if device.type == 'mlu':
            pred = pred.cpu()
        # NMS
        pred = non_max_suppression(pred, opt.conf_thres, opt.iou_thres, opt.classes, opt.agnostic_nms, max_det=opt.max_det)
        dt[2] += time_sync() - t3

        # Process predictions
        for i, det in enumerate(pred):  # per image
            seen += 1
            p, im0, frame = path, im0s.copy(), getattr(dataset, 'frame', 0)
            
            p = Path(p)  # to Path
            
            save_path = str(opt.save_dir / p.name)  # im.jpg
            txt_path = str(opt.save_dir / 'labels' / p.stem) + ('' if dataset.mode == 'image' else f'_{frame}')  # im.txt
            s += '%gx%g ' % im.shape[2:]  # print string
            gn = torch.tensor(im0.shape)[[1, 0, 1, 0]]  # normalization gain whwh
            imc = im0.copy() if opt.save_crop else im0  # for save_crop
            annotator = Annotator(im0, line_width=3, example=str(names))
            if len(det):
                # Rescale boxes from img_size to im0 size
                det[:, :4] = scale_coords(im.shape[2:], det[:, :4], im0.shape).round()

                # Print results
                for c in det[:, -1].unique():
                    n = (det[:, -1] == c).sum()  # detections per class
                    s += f"{n} {names[int(c)]}{'s' * (n > 1)}, "  # add to string

                # Write results
                for *xyxy, conf, cls in reversed(det):
                    c = int(cls)  # integer class
                    label = f'{names[c]} {conf:.2f}'
                    annotator.box_label(xyxy, label, color=colors(c, True))

            # Print time (inference-only)
            LOGGER.info(f'{s}Done. ({t3 - t2:.3f}s)')

            # Stream results
            im0 = annotator.result()
            
            # Save results (image with detections)
            if len(det) == 0:
                empty_box += 1
                save_path = os.path.splitext(save_path)[0] + '_###.png'
                cv2.imwrite(save_path, im0)
            else:
                save_path = os.path.splitext(save_path)[0] + '.png'
                cv2.imwrite(save_path, im0)

            # Copy error data
            # reinforce_dir = os.path.join(opt.source, '../reinforce')
            # with open(label_path) as fp:
            #     lines = fp.readlines()
            # if (len(lines) == 0 and len(det) > 0) or (len(lines) > 0 and len(det) == 0):
            #     file_name = label_path.split('.')[-2].split('/')[-1]
            #     shutil.copy(label_path, os.path.join(reinforce_dir, '{}.txt'.format(file_name)))
            #     shutil.copy(label_path.replace('txt', 'tif'), os.path.join(reinforce_dir, '{}.tif'.format(file_name)))
                


    # Print results
    t = tuple(x / seen * 1E3 for x in dt)  # speeds per image
    LOGGER.info(f'Speed: %.1fms pre-process, %.1fms inference, %.1fms NMS per image at shape {(1, 3, *opt.imgsz)}' % t)

    LOGGER.info(f"Results saved to {colorstr('bold', opt.save_dir)}")

    print("empty box num: ", empty_box)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--weights', type=str, default=ROOT / 'weights/yolov5s.pt', help='model path(s)')
    parser.add_argument('--cfg', type=str, default='', help='model.yaml path')
    parser.add_argument('--data', type=str, default=ROOT / 'data/coco128.yaml', help='dataset.yaml path')
    parser.add_argument('--source', type=str, default=ROOT / 'data/images', help='file/dir/URL/glob, 0 for webcam')
    parser.add_argument('--imgsz', '--img', '--img-size', nargs='+', type=int, default=[640], help='inference size h,w')
    parser.add_argument('--conf-thres', type=float, default=0.25, help='confidence threshold')
    parser.add_argument('--iou-thres', type=float, default=0.45, help='NMS IoU threshold')
    parser.add_argument('--max-det', type=int, default=1000, help='maximum detections per image')
    parser.add_argument('--device', default='', help='cuda device, i.e. 0 or 0,1,2,3 or cpu')
    parser.add_argument('--save_dir', type=str, default=ROOT / 'output', help='image save dir')
    parser.add_argument('--half', action='store_true', help='use FP16 half-precision inference')
    parser.add_argument('--classes', nargs='+', type=int, help='filter by class: --classes 0, or --classes 0 2 3')
    parser.add_argument('--save-crop', action='store_true', help='save cropped prediction boxes')
    parser.add_argument('--agnostic-nms', action='store_true', help='class-agnostic NMS')
    parser.add_argument('--augment', action='store_true', help='augmented inference')
    parser.add_argument('--visualize', action='store_true', help='visualize features')
    opt = parser.parse_args()
    opt.imgsz *= 2 if len(opt.imgsz) == 1 else 1  # expand

    run_detect(opt)