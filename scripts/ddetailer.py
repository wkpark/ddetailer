import os
import re
import sys
import cv2
from PIL import Image
import math
import numpy as np
import gradio as gr
import json
import shutil
import torch
from fastapi import FastAPI
from pathlib import Path

from scripts.mediapipe import mediapipe_detector_face as mp_detector_face
from scripts.mediapipe import mediapipe_detector_facemesh as mp_detector_facemesh
from scripts.ultralytics import ultralytics_inference as ultra_inference

from copy import copy, deepcopy
from modules import processing, images
from modules import safe
from modules import scripts, script_callbacks, shared, devices, modelloader, sd_models, sd_samplers_common, sd_vae, sd_samplers
from modules.call_queue import wrap_gradio_gpu_call
from modules.generation_parameters_copypaste import ParamBinding, register_paste_params_button
from modules.processing import Processed, StableDiffusionProcessingImg2Img
from modules.shared import opts, cmd_opts, state
from modules.sd_models import model_hash
from modules.paths import models_path, data_path
from modules.ui import create_refresh_button, plaintext_to_html
from basicsr.utils.download_util import load_file_from_url

dd_models_path = os.path.join(models_path, "mmdet")
dd_yolo_path = os.path.join(models_path, "yolo")

scriptdir = scripts.basedir()

models_list = {}
models_alias = {}
def list_models(real=True):
        model_list = modelloader.load_models(model_path=dd_models_path, ext_filter=[".pth"])
        model_list += modelloader.load_models(model_path=dd_yolo_path, ext_filter=[".pt", ".onnx"])

        def modeltitle(path, shorthash=None):
            abspath = os.path.abspath(path)

            if abspath.startswith(dd_models_path):
                name = abspath.replace(dd_models_path, '')
            elif abspath.startswith(dd_yolo_path):
                name = abspath.replace(models_path, '')
            else:
                name = os.path.basename(path)

            # fix path separator
            name = name.replace('\\', '/')
            if name.startswith("/"):
                name = name[1:]

            shortname = os.path.splitext(name.replace("/", "_"))[0]

            if shorthash is not None:
                return f'{name} [{shorthash}]', shortname
            return name

        models = []
        for filename in model_list:
            config = filename.rsplit(".", 1)[0] + ".py"
            name = modeltitle(filename)
            # check if config.py file exists or not
            if (filename.startswith("bbox/") or filename.startswith("segm/")) and not os.path.exists(config):
                continue

            if filename not in models_list:
                h = model_hash(filename)
                if "bbox" in filename or "segm" in filename:
                    mtime = os.path.getmtime(os.path.join(dd_models_path, filename))
                else: # yolo
                    mtime = os.path.getmtime(os.path.join(models_path, filename))
                models_list[filename] = { "hash": h, "mtime": mtime }
            else:
                h = models_list[filename]["hash"]
                if "bbox" in filename or "segm" in filename:
                    mtime = os.path.getmtime(os.path.join(dd_models_path, filename))
                else: # yolo
                    mtime = os.path.getmtime(os.path.join(models_path, filename))
                old_mtime = models_list[filename]["mtime"]
                if mtime > old_mtime:
                    # update hash, mtime
                    h = model_hash(filename)
                    models_list[filename] = { "hash": h, "mtime": mtime }

            title, short_model_name = modeltitle(filename, h)
            models.append(title)
            models_alias[title] = filename

        def sortkey(name):
            order2 = [ "bbox", "mediapipe", "yolo/", "segm" ]
            order = [ "face", "hand", "person", "pose" ]
            for j, cat2 in enumerate(order2):
                if cat2 in name:
                    for k, cat in enumerate(order):
                        if cat in name:
                            return j * 100  + k*10
                    return j * 100 + 4*10
            # not reach
            return 1000

        if real is False:
            models = models + ["mediapipe_face_short", "mediapipe_face_full", "mediapipe_face_mesh"]
            models = sorted(models, key=sortkey)
        return models

def startup():
    from launch import is_installed, run
    legacy = torch.__version__.split(".")[0] < "2"
    if not is_installed("mmdet"):
        python = sys.executable
        run(f'"{python}" -m pip install -U openmim', desc="Installing openmim", errdesc="Couldn't install openmim")
        if legacy:
            run(f'"{python}" -m mim install mmcv-full', desc="Installing mmcv-full", errdesc="Couldn't install mmcv-full")
            run(f'"{python}" -m pip install mmdet==2.28.2', desc="Installing mmdet", errdesc="Couldn't install mmdet")
        else:
            run(f'"{python}" -m mim install mmcv>==2.0.0', desc="Installing mmcv", errdesc="Couldn't install mmcv")
            run(f'"{python}" -m pip install mmdet>=3', desc="Installing mmdet", errdesc="Couldn't install mmdet")

    if not legacy and not is_installed("mmyolo"):
        run(f'"{python}" -m mim install mmyolo', desc="Installing mmyolo", errdesc="Couldn't install mmyolo")

    if not is_installed("mediapipe"):
        run(f'"{python}" -m pip install protobuf>=3.20', desc="Installing protobuf", errdesc="Couldn't install protobuf")
        run(f'"{python}" -m pip install mediapipe>=0.10.3', desc="Installing mediapipe", errdesc="Couldn't install mediapipe")

    bbox_path = os.path.join(dd_models_path, "bbox")
    segm_path = os.path.join(dd_models_path, "segm")
    list_model = list_models()

    required = [
        (bbox_path, "mmdet_anime-face_yolov3.pth"),
        (segm_path, "mmdet_dd-person_mask2former.pth"),
        (segm_path, "yolov5_ins_s.pth"),
        (segm_path, "yolov5_ins_n.pth"),
    ]

    optional = [
        (bbox_path, "face_yolov8n.pth"),
        (bbox_path, "face_yolov8s.pth"),
        (bbox_path, "hand_yolov8n.pth"),
        (bbox_path, "hand_yolov8s.pth"),
    ]

    need_download = False
    for path, model in required:
        if not os.path.exists(os.path.join(path, model)):
            need_download = True
            break

    if not legacy:
        for path, model in optional:
            if not os.path.exists(os.path.join(path, model)):
                need_download = True
                break

    while need_download:
        if len(list_model) == 0:
            print("No detection models found, downloading...")
        else:
            print("Check detection models and downloading...")

        if not os.path.exists(os.path.join(bbox_path, "mmdet_anime-face_yolov3.pth")):
            load_file_from_url("https://huggingface.co/dustysys/ddetailer/resolve/main/mmdet/bbox/mmdet_anime-face_yolov3.pth", bbox_path)
        if not os.path.exists(os.path.join(segm_path, "mmdet_dd-person_mask2former.pth")):
            if legacy:
                load_file_from_url("https://huggingface.co/dustysys/ddetailer/resolve/main/mmdet/segm/mmdet_dd-person_mask2former.pth", segm_path)
            else:
                load_file_from_url(
                    #"https://download.openmmlab.com/mmdetection/v3.0/mask2former/mask2former_r50_8xb2-lsj-50e_coco/mask2former_r50_8xb2-lsj-50e_coco_20220506_191028-41b088b6.pth",
                    "https://huggingface.co/wkpark/muddetailer/resolve/main/mmdet/segm/mmdet_dd-person_mask2former.pth", # the same copy
                    segm_path,
                    file_name="mmdet_dd-person_mask2former.pth")
        if not legacy and not os.path.exists(os.path.join(segm_path, "yolov5_ins_n.pth")):
                load_file_from_url(
                    "https://download.openmmlab.com/mmyolo/v0/yolov5/ins_seg/yolov5_ins_n-v61_syncbn_fast_8xb16-300e_coco_instance/yolov5_ins_n-v61_syncbn_fast_8xb16-300e_coco_instance_20230424_104807-84cc9240.pth",
                    segm_path,
                    file_name="yolov5_ins_n.pth")
        if not legacy and not os.path.exists(os.path.join(segm_path, "yolov5_ins_s.pth")):
                load_file_from_url(
                    "https://download.openmmlab.com/mmyolo/v0/yolov5/ins_seg/yolov5_ins_s-v61_syncbn_fast_8xb16-300e_coco_instance/yolov5_ins_s-v61_syncbn_fast_8xb16-300e_coco_instance_20230426_012542-3e570436.pth",
                    segm_path,
                    file_name="yolov5_ins_s.pth")

        if legacy:
            break

        # optional models
        huggingface_src_path = "https://huggingface.co/wkpark/mmyolo-yolov8/resolve/main"
        for path, model in optional:
            if not os.path.exists(os.path.join(path, model)):
                load_file_from_url(f"{huggingface_src_path}/{model}", path)

        break

    print("Check config files...")
    config_dir = os.path.join(scripts.basedir(), "config")
    if legacy:
        configs = [ "mmdet_anime-face_yolov3.py", "mmdet_dd-person_mask2former.py" ]
    else:
        configs = [ "mmdet_anime-face_yolov3-v3.py", "mmdet_dd-person_mask2former-v3.py", "default_runtime.py", "mask2former_r50_8xb2-lsj-50e_coco.py", "mask2former_r50_8xb2-lsj-50e_coco-panoptic.py", "coco_panoptic.py" ]

    destdir = bbox_path
    for confpy in configs:
        conf = os.path.join(config_dir, confpy)
        if not legacy:
            confpy = confpy.replace("-v3.py", ".py")
        dest = os.path.join(destdir, confpy)
        if not os.path.exists(dest):
            print(f"Copy config file: {confpy}..")
            shutil.copy(conf, dest)
        destdir = segm_path

    if legacy:
        print("Done")
        return

    configs = [
        (bbox_path, ["face_yolov8n.py", "face_yolov8s.py", "hand_yolov8n.py", "hand_yolov8s.py", "default_runtime.py", "yolov8_s_syncbn_fast_8xb16-500e_coco.py"]),
        (segm_path, ["yolov5_ins_s.py", "yolov5_ins_n.py",
            "yolov5_ins_s-v61_syncbn_fast_8xb16-300e_coco_instance.py", "yolov5_s-v61_syncbn_8xb16-300e_coco.py", "yolov5_s-v61_syncbn_fast_8xb16-300e_coco.py"]),
    ]
    for destdir, files in configs:
        for file in files:
            destconf = os.path.join(destdir, file)
            confpy = os.path.join(config_dir, file)
            if not os.path.exists(destconf) or os.path.getmtime(confpy) > os.path.getmtime(destconf):
                print(f"Copy config file: {confpy}..")
                shutil.copy(confpy, destdir)

    print("Done")

startup()

def gr_show(visible=True):
    return {"visible": visible, "__type__": "update"}

def gr_enable(interactive=True):
    return {"interactive": interactive, "__type__": "update"}

def gr_open(open=True):
    return {"open": open, "__type__": "update"}

def ddetailer_extra_params(
    use_prompt_edit,
    use_prompt_edit_2,
    dd_model_a, dd_classes_a,
    dd_conf_a, dd_max_per_img_a,
    dd_detect_order_a, dd_select_masks_a,
    dd_dilation_factor_a,
    dd_offset_x_a, dd_offset_y_a,
    dd_prompt, dd_neg_prompt,
    dd_preprocess_b, dd_bitwise_op,
    dd_model_b, dd_classes_b,
    dd_conf_b, dd_max_per_img_b,
    dd_detect_order_b, dd_select_masks_b,
    dd_dilation_factor_b,
    dd_offset_x_b, dd_offset_y_b,
    dd_prompt_2, dd_neg_prompt_2,
    dd_mask_blur, dd_denoising_strength,
    dd_inpaint_full_res, dd_inpaint_full_res_padding,
    dd_cfg_scale, dd_steps, dd_noise_multiplier,
    dd_sampler, dd_checkpoint, dd_vae, dd_clipskip,
):
    params = {
        "MuDDetailer use prompt edit": use_prompt_edit,
        "MuDDetailer use prompt edit b": use_prompt_edit_2,
        "MuDDetailer prompt": dd_prompt,
        "MuDDetailer neg prompt": dd_neg_prompt,
        "MuDDetailer prompt b": dd_prompt_2,
        "MuDDetailer neg prompt b": dd_neg_prompt_2,
        "MuDDetailer model a": dd_model_a,
        "MuDDetailer conf a": dd_conf_a,
        "MuDDetailer max detection a": dd_max_per_img_a,
        "MuDDetailer dilation a": dd_dilation_factor_a,
        "MuDDetailer offset x a": dd_offset_x_a,
        "MuDDetailer offset y a": dd_offset_y_a,
        "MuDDetailer mask blur": dd_mask_blur,
        "MuDDetailer denoising": dd_denoising_strength,
        "MuDDetailer inpaint full": dd_inpaint_full_res,
        "MuDDetailer inpaint padding": dd_inpaint_full_res_padding,
        # DDtailer extension
        "MuDDetailer CFG scale": dd_cfg_scale,
        "MuDDetailer steps": dd_steps,
        "MuDDetailer noise multiplier": dd_noise_multiplier,
        "MuDDetailer sampler": dd_sampler,
        "MuDDetailer checkpoint": dd_checkpoint,
        "MuDDetailer VAE": dd_vae,
        "MuDDetailer CLIP skip": dd_clipskip,
    }
    if dd_classes_a is not None and len(dd_classes_a) > 0:
        params["MuDDetailer classes a"] = ",".join(dd_classes_a)
    if dd_detect_order_a is not None and len(dd_detect_order_a) > 0:
        params["MuDDetailer detect order a"] = ",".join(dd_detect_order_a)
    if dd_select_masks_a is not None and dd_select_masks_a != "":
        params["MuDDetailer select masks a"] = dd_select_masks_a

    if dd_model_b != "None":
        params["MuDDetailer model b"] = dd_model_b
        if dd_classes_b is not None and len(dd_classes_b) > 0:
            params["MuDDetailer classes b"] = ",".join(dd_classes_b)
        if dd_detect_order_b is not None and len(dd_detect_order_b) > 0:
            params["MuDDetailer detect order b"] = ",".join(dd_detect_order_b)
        if dd_select_masks_b is not None and dd_select_masks_b != "":
            params["MuDDetailer select masks b"] = dd_select_masks_b
        params["MuDDetailer preprocess b"] = dd_preprocess_b
        params["MuDDetailer bitwise"] = dd_bitwise_op
        params["MuDDetailer conf b"] = dd_conf_b
        params["MuDDetailer max detection b"] = dd_max_per_img_b
        params["MuDDetailer dilation b"] = dd_dilation_factor_b
        params["MuDDetailer offset x b"] = dd_offset_x_b
        params["MuDDetailer offset y b"] = dd_offset_y_b

    if not dd_prompt:
        params.pop("MuDDetailer prompt")
    if not dd_neg_prompt:
        params.pop("MuDDetailer neg prompt")
    if not dd_prompt_2:
        params.pop("MuDDetailer prompt b")
    if not dd_neg_prompt_2:
        params.pop("MuDDetailer neg prompt b")

    if dd_clipskip == 0:
        params.pop("MuDDetailer CLIP skip")
    if dd_checkpoint in [ "Use same checkpoint", "Default", "None" ]:
        params.pop("MuDDetailer checkpoint")
    if dd_vae in [ "Use same VAE", "Default", "None" ]:
        params.pop("MuDDetailer VAE")
    if dd_sampler in [ "Use same sampler", "Default", "None" ]:
        params.pop("MuDDetailer sampler")

    return params

def dd_list_models():
    # save current checkpoint_info and call register() again to restore
    checkpoint_info = shared.sd_model.sd_checkpoint_info if shared.sd_model is not None else None
    sd_models.list_models()
    if checkpoint_info is not None:
        # register saved checkpoint_info again
        checkpoint_info.register()

class MuDetectionDetailerScript(scripts.Script):

    init_on_after_callback = False
    init_on_app_started = False

    img2img_components = {}
    txt2img_components = {}
    components = {}

    txt2img_ids = ["txt2img_prompt", "txt2img_neg_prompt", "txt2img_styles", "txt2img_steps", "txt2img_sampling", "txt2img_batch_count", "txt2img_batch_size",
                "txt2img_cfg_scale", "txt2img_width", "txt2img_height", "txt2img_seed", "txt2img_denoising_strength" ]

    img2img_ids = ["img2img_prompt", "img2img_neg_prompt", "img2img_styles", "img2img_steps", "img2img_sampling", "img2img_batch_count", "img2img_batch_size",
                "img2img_cfg_scale", "img2img_width", "img2img_height", "img2img_seed", "img2img_denoising_strength" ]

    def __init__(self):
        super().__init__()

    def title(self):
        return "Mu Detection Detailer"

    def show(self, is_img2img):
        return scripts.AlwaysVisible

    def after_component(self, component, **_kwargs):
        DD = MuDetectionDetailerScript

        elem_id = getattr(component, "elem_id", None)
        if elem_id is None:
            return

        if elem_id in [ "txt2img_generate", "img2img_generate", "img2img_image" ]:
            DD.components[elem_id] = component

        if elem_id in DD.txt2img_ids:
            DD.txt2img_components[elem_id] = component
        elif elem_id in DD.img2img_ids:
            DD.img2img_components[elem_id] = component

        if elem_id in [ "img2img_gallery", "html_info_img2img", "generation_info_img2img", "txt2img_gallery", "html_info_txt2img", "generation_info_txt2img" ]:
            DD.components[elem_id] = component

    def show_classes(self, modelname, classes):
        if modelname == "None" or "mediapipe_" in modelname:
            return gr.update(visible=False, choices=[], value=[])

        if "yolo/" in modelname:
            from ultralytics import YOLO

            model_path = modelpath(modelname)

            # given text class names
            classes_path = model_path.rsplit(".", 1)[0] + ".json"
            _classes = None
            if os.path.exists(classes_path):
                with open(classes_path) as f:
                    _classes = json.load(f)
            else:
                safe_torch_load = torch.load
                try:
                    torch.load = safe.unsafe_torch_load
                    model = YOLO(model_path)
                finally:
                    torch.load = safe_torch_load

                if model.names is not None:
                    _classes = list(model.names.values())

            if _classes is not None:
                default = [_classes[0]] if _classes[0].lower() in ["person", "face", "hand", "human"] else []
                return gr.update(visible=True, choices=["None"] + _classes, value=default)

            return gr.update(visible=False, choices=[], value=[])

        dataset = modeldataset(modelname)
        if dataset == "coco":
            path = modelpath(modelname)
            all_classes = None
            if os.path.exists(path):
                model = torch.load(path, map_location="cpu")
                if "meta" in model and "CLASSES" in model["meta"]:
                    all_classes = list(model["meta"].get("CLASSES", ("None",)))
                    print("meta classes =", all_classes)
                del model

            if all_classes is None:
                all_classes = get_classes(dataset)

            # check duplicates
            if len(classes) > 0:
                cls = list(set(classes) & set(all_classes))
                if set(cls) == set(classes):
                    return gr.update(visible=True, choices=["None"] + all_classes)
                else:
                    return gr.update(visible=True, choices=["None"] + all_classes, value=cls)
            return gr.update(visible=True, choices=["None"] + all_classes, value=[all_classes[0]])
        else:
            return gr.update(visible=False, choices=[], value=[])

    def ui(self, is_img2img):

        with gr.Accordion("µ Detection Detailer", open=False, elem_id="mudd_main_" + ("txt2img" if not is_img2img else "img2img")):
            with gr.Row():
                enabled = gr.Checkbox(label="Enable", value=False, visible=True, elem_classes=["mudd-enabled"])

            model_list = list_models(False)
            default_model = match_modelname("face_yolov8n.pth")
            if is_img2img:
                gr.HTML("<p style=\"margin-bottom:0.75em\">Recommended settings: Use from inpaint tab, inpaint only masked ON, denoise &lt; 0.5</p>")
            else:
                gr.HTML("")
            with gr.Group(), gr.Tabs():
                with gr.Tab("Primary"):
                    with gr.Row():
                        dd_model_a = gr.Dropdown(label="Primary detection model (A):", choices=["None"] + model_list, value=default_model, visible=True, type="value")
                        create_refresh_button(dd_model_a, lambda: None, lambda: {"choices": list_models(False) + ["None"]},"mudd_refresh_model_a")
                        dd_classes_a = gr.Dropdown(label="Object classes", choices=[], value=[], visible=False, interactive=True, multiselect=True)
                    with gr.Row():
                        use_prompt_edit = gr.Checkbox(label="Use Prompt edit", elem_classes="prompt_edit_checkbox", value=False, interactive=True, visible=True)

                    enabled.input(
                        fn=lambda enable, model: gr.update(value=default_model if enable and model == "None" else model),
                        inputs=[enabled, dd_model_a],
                        outputs=[dd_model_a],
                        show_progress=False,
                    )

                    with gr.Group():
                        with gr.Group(visible=False) as prompt_1:
                            with gr.Row(elem_id="mudd_" + ("img2img" if is_img2img else "txt2img") + "_toprow_prompt"):
                                dd_prompt = gr.Textbox(
                                    label="prompt_1",
                                    show_label=False,
                                    lines=3,
                                    placeholder="Prompt"
                                    + "\nIf blank, the main prompt is used.",
                                    elem_id="mudd_" + ("img2img" if is_img2img else "txt2img") + "_prompt",
                                    elem_classes=["prompt"],
                                )

                                dd_neg_prompt = gr.Textbox(
                                    label="negative_prompt_1",
                                    show_label=False,
                                    lines=3,
                                    placeholder="Negative prompt"
                                    + "\nIf blank, the main negative prompt is used.",
                                    elem_id="mudd_" + ("img2img" if is_img2img else "txt2img") + "_neg_prompt",
                                    elem_classes=["prompt"],
                                )
                        with gr.Group(visible=False) as model_a_options:
                            with gr.Row():
                                with gr.Column():
                                    with gr.Row():
                                        dd_conf_a = gr.Slider(label='Confidence %', minimum=0, maximum=100, step=1, value=30, min_width=140)
                                        dd_dilation_factor_a = gr.Slider(label='Dilation', minimum=0, maximum=255, step=1, value=4, min_width=140)
                                with gr.Column():
                                    with gr.Row():
                                        dd_offset_x_a = gr.Slider(label='X offset', minimum=-200, maximum=200, step=1, value=0, min_width=140)
                                        dd_offset_y_a = gr.Slider(label='Y offset', minimum=-200, maximum=200, step=1, value=0, min_width=140)
                            with gr.Row():
                                dd_max_per_img_a = gr.Slider(label='Max detections (0: use default)', minimum=0, maximum=100, step=1, value=0, min_width=140)
                                dd_detect_order_a = gr.CheckboxGroup(label="Detect order", choices=["area", "position"], interactive=True, value=[], min_width=140)
                            with gr.Row():
                                dd_select_masks_a = gr.Textbox(label='Select detections to process', value='', placeholder='input detection numbers to process e.g) 1,2,3-5..', interactive=True)

                    dd_model_a.change(
                        fn=self.show_classes,
                        inputs=[dd_model_a, dd_classes_a],
                        outputs=[dd_classes_a],
                    )

                with gr.Tab("Secondary"):
                    with gr.Row():
                        dd_model_b = gr.Dropdown(label="Secondary detection model (B) (optional):", choices=["None"] + model_list, value="None", visible=True, type="value")
                        create_refresh_button(dd_model_b, lambda: None, lambda: {"choices": ["None"] + list_models(False)},"mudd_refresh_model_b")
                        dd_classes_b = gr.Dropdown(label="Object classes", choices=[], value=[], visible=False, interactive=True, multiselect=True)
                    with gr.Row():
                        use_prompt_edit_2 = gr.Checkbox(label="Use Prompt edit", elem_classes="prompt_edit_checkbox", value=False, interactive=False, visible=True)

                    with gr.Group():
                        with gr.Group(visible=False) as prompt_2:
                            with gr.Row(elem_id="mudd_" + ("img2img" if is_img2img else "txt2img") + "_2_toprow_prompt"):
                                dd_prompt_2 = gr.Textbox(
                                    label="prompt_2",
                                    show_label=False,
                                    lines=3,
                                    placeholder="Prompt"
                                    + "\nIf blank, the main prompt is used.",
                                    elem_id="mudd_" + ("img2img" if is_img2img else "txt2img") + "_prompt_2",
                                    elem_classes=["prompt"],
                                )

                                dd_neg_prompt_2 = gr.Textbox(
                                    label="negative_prompt_2",
                                    show_label=False,
                                    lines=3,
                                    placeholder="Negative prompt"
                                    + "\nIf blank, the main negative prompt is used.",
                                    elem_id="mudd_" + ("img2img" if is_img2img else "txt2img") + "_neg_prompt_2",
                                    elem_classes=["prompt"],
                                )

                        with gr.Group(visible=False) as model_b_options:
                            with gr.Row():
                                with gr.Column():
                                    with gr.Row():
                                        dd_conf_b = gr.Slider(label='Confidence % (B)', minimum=0, maximum=100, step=1, value=30, min_width=140)
                                        dd_dilation_factor_b = gr.Slider(label='Dilation (B)', minimum=0, maximum=255, step=1, value=4, min_width=140)
                                with gr.Column():
                                    with gr.Row():
                                        dd_offset_x_b = gr.Slider(label='X offset (B)', minimum=-200, maximum=200, step=1, value=0, min_width=140)
                                        dd_offset_y_b = gr.Slider(label='Y offset (B)', minimum=-200, maximum=200, step=1, value=0, min_width=140)
                            with gr.Row():
                                dd_max_per_img_b = gr.Slider(label='Max detections (B) (0: use default)', minimum=0, maximum=100, step=1, value=0, min_width=140)
                                dd_detect_order_b = gr.CheckboxGroup(label="Detect order (B)", choices=["area", "position"], interactive=True, value=[], min_width=140)
                            with gr.Row():
                                dd_select_masks_b = gr.Textbox(label='Select detections to process (B)', value='', placeholder='input detection numbers to process e.g) 1,2,3-5..', interactive=True)
                    dd_model_b.change(
                        fn=self.show_classes,
                        inputs=[dd_model_b, dd_classes_b],
                        outputs=[dd_classes_b],
                    )

            with gr.Group(visible=False) as options:
                with gr.Accordion("Inpainting options"):
                    with gr.Column(variant="compact"):
                        with gr.Row():
                            dd_mask_blur = gr.Slider(label='Mask blur', minimum=0, maximum=64, step=1, value=4, min_width=200)
                            dd_denoising_strength = gr.Slider(label='Denoising strength', minimum=0.0, maximum=1.0, step=0.01, value=0.4, min_width=200)

                        sampler_names = [sampler.name for sampler in sd_samplers.all_samplers]
                    with gr.Column(variant="compact"):
                        dd_inpaint_full_res = gr.Checkbox(label='Inpaint mask only', value=True, min_width=140)
                        dd_inpaint_full_res_padding = gr.Slider(label='Inpaint only masked padding, pixels', minimum=0, maximum=256, step=4, value=32, min_width=140)
                    with gr.Group(visible=False) as model_b_options_2:
                        with gr.Row():
                            dd_preprocess_b = gr.Checkbox(label='Inpaint B detections before inpainting A')

                    with gr.Group(visible=False) as operation:
                        with gr.Row():
                            dd_bitwise_op = gr.Radio(label='Bitwise Mask operation', info="Mask operation A and B", choices=['None', 'A&B', 'A-B'], value="None")

                with gr.Accordion("Advanced options", open=False) as advanced:
                    gr.HTML(value="<p>Low level options ('0' or 'Use same..' means use the same setting value)</p>")
                    with gr.Row():
                        with gr.Column(variant="compact"):
                            with gr.Row():
                                dd_sampler = gr.Dropdown(label='Sampling method', choices=["Use same sampler"] + sampler_names, value="Use same sampler", min_width=200)
                                dd_steps = gr.Slider(label='Sampling steps', minimum=0, maximum=120, step=1, value=0, min_width=200)
                        with gr.Column(variant="compact"):
                            with gr.Row():
                                dd_noise_multiplier = gr.Slider(label='Noise multiplier', minimum=0, maximum=1.5, step=0.01, value=0, min_with=200)
                                dd_cfg_scale = gr.Slider(label='CFG Scale', minimum=0, maximum=30, step=0.5, value=0, min_width=200)
                    with gr.Column(variant="compact"):
                        with gr.Row():
                            dd_checkpoint = gr.Dropdown(label='Use Checkpoint', choices=["Use same checkpoint"] + sd_models.checkpoint_tiles(), value="Use same checkpoint", min_width=155)
                            create_refresh_button(dd_checkpoint, dd_list_models, lambda: {"choices": ["Use same checkpoint"] + sd_models.checkpoint_tiles()},"dd_refresh_checkpoint")

                            dd_vae = gr.Dropdown(choices=["Use same VAE"] + list(sd_vae.vae_dict), value="Use same VAE", label="Use VAE", elem_id="dd_vae", min_width=155)
                            create_refresh_button(dd_vae, sd_vae.refresh_vae_list, lambda: {"choices": ["Use same VAE"] + list(sd_vae.vae_dict)}, "dd_refresh_vae")

                    with gr.Column(variant="compact"):
                        dd_clipskip = gr.Slider(label='Use Clip skip', minimum=0, maximum=12, step=1, value=0, min_width=140)
                    with gr.Column():
                        with gr.Row():
                            advanced_reset = gr.Checkbox(label="Reset advanced options", value=False, elem_id="dd_advanced_reset")
                        advanced_reset.select(
                            lambda: {
                                dd_noise_multiplier: 0,
                                dd_cfg_scale: 0,
                                dd_steps: 0,
                                dd_clipskip: 0,
                                dd_sampler: "Use same sampler",
                                dd_checkpoint: "Use same checkpoint",
                                dd_vae: "Use same VAE",
                                advanced_reset: False,
                            },
                            inputs=[],
                            outputs=[advanced_reset, dd_noise_multiplier, dd_cfg_scale, dd_sampler, dd_steps, dd_checkpoint, dd_vae, dd_clipskip],
                            show_progress=False,
                        )

                with gr.Accordion("Inpainting Helper", open=False):
                    gr.HTML(value="<p>If you already have images in the gallery, you can click one of them to select and click the Inpaint button.</p>")
                    tabname = "txt2img" if not is_img2img else "img2img"
                    with gr.Column(variant="compact"):
                        with gr.Row():
                            if not is_img2img:
                                dd_image = gr.Image(label='Image', type="pil", elem_id="mudd_inpainting_image")

                        with gr.Group(visible=True) as select_group_a:
                            with gr.Row():
                                labels_a = gr.Dropdown(label="Detected masks", choices=[], values=[], multiselect=True, interactive=True)
                            with gr.Row():
                                select_options_a = gr.CheckboxGroup(choices=["sync", "detect only"], value=["sync"], label="", show_label=False, interactive=True)
                                masks_a = gr.Textbox(label="Detected masks", value="", visible=False, elem_id="mudd_masks_a_" + tabname)
                                # for {tabname}_gallery used by js
                                masks_a_galley = gr.Textbox(label="Detected masks", value="", visible=False, elem_id="mudd_masks_a_gallery_" + tabname)
                                # used by js
                                masks_a_change = gr.Button(visible=False, elem_id="mudd_masks_a_change_" + tabname)
                        with gr.Group(visible=False) as select_group_b:
                            with gr.Row():
                                labels_b = gr.Dropdown(label="Detected masks (B)", choices=[], values=[], multiselect=True, interactive=True)
                            with gr.Row():
                                select_options_b = gr.CheckboxGroup(choices=["sync", "detect only"], value=["sync"], label="", show_label=False, interactive=True)
                                masks_b = gr.Textbox(label="Detected masks (B)", value="", visible=False, elem_id="mudd_masks_b_" + tabname)
                                # for {tabname}_gallery used by js
                                masks_b_galley = gr.Textbox(label="Detected masks", value="", visible=False, elem_id="mudd_masks_b_gallery_" + tabname)
                                # used by js
                                masks_b_change = gr.Button(visible=False, elem_id="mudd_masks_b_change_" + tabname)

                        with gr.Row():
                            if not is_img2img:
                                dd_import_prompt = gr.Button(value='Import prompt', interactive=False, variant="secondary")
                            dd_run_inpaint = gr.Button(value='Inpaint', interactive=True, variant="primary")
                        generation_info = gr.Textbox(visible=False, elem_id="muddetailer_image_generation_info")
                        dummy_label_a = gr.Textbox(value="A", visible=False)
                        dummy_label_b = gr.Textbox(value="B", visible=False)
                        dummy_true =  gr.Checkbox(value=True, visible=False)
                        dummy_false =  gr.Checkbox(value=False, visible=False)

                    if not is_img2img:
                        register_paste_params_button(ParamBinding(
                            paste_button=dd_import_prompt, tabname="txt2img" if not is_img2img else "img2img",
                                source_text_component=generation_info, source_image_component=dd_image,
                        ))

                    def get_pnginfo(image):
                        if image is None:
                            return '', gr.update(interactive=False, variant="secondary"), "", ""

                        geninfo, _ = images.read_info_from_image(image)
                        if geninfo is None or geninfo.strip() == "":
                            return '', gr.update(interactive=False, variant="secondary"), "", ""

                        params = parse_prompt(geninfo)
                        # auto update masks info.
                        masks_a_json = params.get("MuDDetailer detection a", gr.update())
                        masks_b_json = params.get("MuDDetailer detection a", gr.update())

                        return geninfo, gr.update(interactive=True, variant="primary"), masks_a_json, masks_b_json


                    if not is_img2img:
                        dd_image.change(
                            fn=get_pnginfo,
                            inputs=[dd_image],
                            outputs=[generation_info, dd_import_prompt, masks_a, masks_b],
                        )

                    # setup labels_a, labels_b events
                    def select_masks(select, selected, options, label="A", is_img2img=False):
                        select = ",".join(select)
                        sync = False
                        detectonly = False
                        if type(options) is list:
                            if "sync" in options:
                                sync = True
                            if "detect only" in options:
                                detectonly = True
                        else:
                            sync = options

                        if detectonly:
                            return "0"

                        if sync:
                            selected = select
                        else:
                            selected += "," + select
                        selected = parse_select_masks(selected, label)
                        selected = selected[label]
                        selected = ",".join(zip_ranges(selected))

                        return selected


                    def update_labels(masks, dummy_label, is_img2img):
                        """show selectable mask labels in the labels dropdown components"""
                        if masks:
                            try:
                                loaded = json.loads(masks)
                            except Exception:
                                tmp = masks.split(",")
                                if len(tmp) == 5:
                                    # only one detection case.
                                    labs = [f"{tmp[0]}:1"]
                                    return gr.update(choices=labs, value=labs)
                                return gr.update(choices=[], value=[])
                        else:
                            return gr.update(choices=[], value=[])

                        bboxes = loaded.get("bboxes", None)
                        if bboxes is not None:
                            scores = loaded["scores"]
                            labs = [f"{lab}{scores[i]>0 and f' {round(scores[i],2)}' or ''}:{i+1}" for i, lab in enumerate(loaded["labels"])]
                            if len(labs) == 1:
                                return gr.update(choices=labs, value=labs)
                            return gr.update(choices=labs, value=[])

                        return gr.update(choices=[], value=[])


                    masks_a_args = dict(
                        _js="reset_masks",
                        fn=update_labels,
                        inputs=[masks_a, dummy_label_a, dummy_true if is_img2img else dummy_false],
                        outputs=[labels_a],
                        show_progress=False,
                    )
                    masks_a.change(**masks_a_args);
                    masks_a_args["_js"] = "gallery_get_masks"
                    masks_a_change.click(**masks_a_args);

                    masks_b_args = dict(
                        _js="reset_masks",
                        fn=update_labels,
                        inputs=[masks_b, dummy_label_b, dummy_true if is_img2img else dummy_false],
                        outputs=[labels_b],
                        show_progress=False,
                    )
                    masks_b.change(**masks_b_args);
                    masks_b_args["_js"] = "gallery_get_masks"
                    masks_b_change.click(**masks_b_args);

                    labels_args = dict(
                        _js="overlay_masks",
                        fn=select_masks,
                        inputs=[labels_a, dd_select_masks_a, select_options_a, dummy_label_a, dummy_true if is_img2img else dummy_false],
                        outputs=[dd_select_masks_a],
                        show_progress=False,
                    )
                    labels_a.select(**labels_args)
                    labels_a.change(**labels_args)
                    labels_args.pop("_js")
                    select_options_a.change(**labels_args)

                    labels_args = dict(
                        _js="overlay_masks",
                        fn=select_masks,
                        inputs=[labels_b, dd_select_masks_b, select_options_b, dummy_label_b, dummy_true if is_img2img else dummy_false],
                        outputs=[dd_select_masks_b],
                        show_progress=False,
                    )
                    labels_b.select(**labels_args)
                    labels_b.change(**labels_args)
                    labels_args.pop("_js")
                    select_options_b.change(**labels_args)

                    dummy_component = gr.Label(visible=False)

            dd_model_a.change(
                lambda modelname: {
                    dd_model_b:gr_show( modelname != "None" ),
                    model_a_options:gr_show( modelname != "None" ),
                    options:gr_show( modelname != "None" ),
                    use_prompt_edit:gr_enable( modelname != "None" )
                },
                inputs= [dd_model_a],
                outputs=[dd_model_b, model_a_options, options, use_prompt_edit],
                show_progress=False,
            )

            self.infotext_fields = (
                (use_prompt_edit, "MuDDetailer use prompt edit"),
                (dd_prompt, "MuDDetailer prompt"),
                (dd_neg_prompt, "MuDDetailer neg prompt"),
                (dd_model_a, "MuDDetailer model a"),
                (dd_classes_a, "MuDDetailer classes a"),
                (dd_conf_a, "MuDDetailer conf a"),
                (dd_max_per_img_a, "MuDDetailer max detection a"),
                (dd_detect_order_a, "MuDDetailer detect order a"),
                (dd_select_masks_a, "MuDDetailer select masks a"),
                (dd_dilation_factor_a, "MuDDetailer dilation a"),
                (dd_offset_x_a, "MuDDetailer offset x a"),
                (dd_offset_y_a, "MuDDetailer offset y a"),
                (dd_preprocess_b, "MuDDetailer preprocess b"),
                (dd_bitwise_op, "MuDDetailer bitwise"),
                (dd_model_b, "MuDDetailer model b"),
                (dd_classes_b, "MuDDetailer classes b"),
                (dd_conf_b, "MuDDetailer conf b"),
                (dd_max_per_img_b, "MuDDetailer max detection b"),
                (dd_detect_order_b, "MuDDetailer detect order b"),
                (dd_select_masks_b, "MuDDetailer select masks b"),
                (dd_dilation_factor_b, "MuDDetailer dilation b"),
                (dd_offset_x_b, "MuDDetailer offset x b"),
                (dd_offset_y_b, "MuDDetailer offset y b"),
                (dd_mask_blur, "MuDDetailer mask blur"),
                (dd_denoising_strength, "MuDDetailer denoising"),
                (dd_inpaint_full_res, "MuDDetailer inpaint full"),
                (dd_inpaint_full_res_padding, "MuDDetailer inpaint padding"),
                (dd_cfg_scale, "MuDDetailer CFG scale"),
                (dd_steps, "MuDDetailer steps"),
                (dd_noise_multiplier, "MuDDetailer noise multiplier"),
                (dd_clipskip, "MuDDetailer CLIP skip"),
                (dd_sampler, "MuDDetailer sampler"),
                (dd_checkpoint, "MuDDetailer checkpoint"),
                (dd_vae, "MuDDetailer VAE"),
                (masks_a, "MuDDetailer detection a"),
                (masks_b, "MuDDetailer detection b"),
            )

            dd_model_b.change(
                lambda modelname: {
                    model_b_options:gr_show( modelname != "None" ),
                    model_b_options_2:gr_show( modelname != "None" ),
                    operation:gr_show( modelname != "None" ),
                    use_prompt_edit_2:gr_enable( modelname != "None" )
                },
                inputs= [dd_model_b],
                outputs=[model_b_options, model_b_options_2, operation, use_prompt_edit_2],
                show_progress=False,
            )

            use_prompt_edit.change(
                lambda enable: {
                    prompt_1:gr_show(enable),
                },
                inputs=[use_prompt_edit],
                outputs=[prompt_1]
            )

            use_prompt_edit_2.change(
                lambda enable: {
                    prompt_2:gr_show(enable),
                },
                inputs=[use_prompt_edit_2],
                outputs=[prompt_2]
            )

            dd_cfg_scale.change(
                lambda value: {
                    advanced:gr_open(True) if advanced.open is False and value > 0 else gr.update()
                },
                inputs=[dd_cfg_scale],
                outputs=[advanced],
                show_progress=False,
            )

            dd_steps.change(
                lambda value: {
                    advanced:gr_open(True) if advanced.open is False and value > 0 else gr.update()
                },
                inputs=[dd_steps],
                outputs=[advanced],
                show_progress=False,
            )

            dd_noise_multiplier.change(
                lambda value: {
                    advanced:gr_open(True) if advanced.open is False and value > 0 else gr.update()
                },
                inputs=[dd_noise_multiplier],
                outputs=[advanced],
                show_progress=False,
            )

            dd_checkpoint.change(
                lambda value: {
                    advanced:gr_open(True) if advanced.open is False and value not in [ "Use same checkpoint", "Default", "None" ] else gr.update()
                },
                inputs=[dd_checkpoint],
                outputs=[advanced],
                show_progress=False,
            )

            dd_vae.change(
                lambda value: {
                    advanced:gr_open(True) if advanced.open is False and value not in [ "Use same VAE", "Default", "None" ] else gr.update()
                },
                inputs=[dd_vae],
                outputs=[advanced],
                show_progress=False,
            )

            dd_sampler.change(
                lambda value: {
                    advanced:gr_open(True) if advanced.open is False and value not in [ "Use same sampler", "Default", "None" ] else gr.update()
                },
                inputs=[dd_sampler],
                outputs=[advanced],
                show_progress=False,
            )

            dd_clipskip.change(
                lambda value: {
                    advanced:gr_open(True) if advanced.open is False and value > 0 else gr.update()
                },
                inputs=[dd_clipskip],
                outputs=[advanced],
                show_progress=False,
            )

        all_args = [
                    use_prompt_edit,
                    use_prompt_edit_2,
                    dd_model_a, dd_classes_a,
                    dd_conf_a, dd_max_per_img_a,
                    dd_detect_order_a, dd_select_masks_a,
                    dd_dilation_factor_a,
                    dd_offset_x_a, dd_offset_y_a,
                    dd_prompt, dd_neg_prompt,
                    dd_preprocess_b, dd_bitwise_op,
                    dd_model_b, dd_classes_b,
                    dd_conf_b, dd_max_per_img_b,
                    dd_detect_order_b, dd_select_masks_a,
                    dd_dilation_factor_b,
                    dd_offset_x_b, dd_offset_y_b,
                    dd_prompt_2, dd_neg_prompt_2,
                    dd_mask_blur, dd_denoising_strength,
                    dd_inpaint_full_res, dd_inpaint_full_res_padding,
                    dd_cfg_scale, dd_steps, dd_noise_multiplier,
                    dd_sampler, dd_checkpoint, dd_vae, dd_clipskip,
        ]
        # 31 arguments

        def get_txt2img_components():
            DD = MuDetectionDetailerScript
            ret = []
            for elem_id in DD.txt2img_ids:
                ret.append(DD.txt2img_components[elem_id])
            return ret
        def get_img2img_components():
            DD = MuDetectionDetailerScript
            ret = []
            for elem_id in DD.img2img_ids:
                ret.append(DD.img2img_components[elem_id])
            return ret

        def run_inpaint(task, tab, gallery_idx, input, gallery, generation_info, prompt, negative_prompt, styles, steps, sampler_name, batch_count, batch_size,
                cfg_scale, width, height, seed, denoising_strength, *_args):

            if gallery_idx < 0:
                gallery_idx = 0

            # image from gr.Image() or gr.Gallery()
            image = input if input is not None else import_image_from_gallery(gallery, gallery_idx)
            if image is None:
                return gr.update(), gr.update(), generation_info, "No input image found"

            # convert to RGB
            image = image.convert("RGB")

            # try to read info from image
            info, _ = images.read_info_from_image(image)

            params = {}
            if info is not None:
                params = parse_prompt(info)
                if "Seed" in params:
                    seed = int(params["Seed"])

            outpath = opts.outdir_samples or opts.outdir_txt2img_samples if not is_img2img else opts.outdir_samples or opts.outdir_img2img_samples

            # fix compatible
            if type(sampler_name) is int and sampler_name < len(sd_samplers.all_samplers):
                sampler_name = sd_samplers.all_samplers[sampler_name].name

            p = processing.StableDiffusionProcessingTxt2Img(
                sd_model=shared.sd_model,
                outpath_samples=outpath,
                prompt=prompt,
                negative_prompt=negative_prompt,
                seed=seed,
                styles=styles,
                sampler_name=sampler_name,
                batch_size=batch_size,
                n_iter=1,
                steps=steps,
                cfg_scale=cfg_scale,
                width=width,
                height=height,
            )
            # set scripts and args
            p.scripts = scripts.scripts_txt2img
            p.script_args = _args[len(all_args):]

            p.all_seeds = [ seed ]

            # misc prepare
            if getattr(p, "setup_prompt", None) is not None:
                p.setup_prompts()
            else:
                p.all_prompts = p.all_prompts or [p.prompt]
                p.all_negative_prompts = p.all_negative_prompts or [p.negative_prompt]
                p.all_seeds = p.all_seeds or [p.seed]
                p.all_subseeds = p.all_subseeds or [p.subseed]

            p._inpainting = True

            # clear tqdm
            shared.total_tqdm.clear()

            # run inpainting
            pp = scripts.PostprocessImageArgs(image)
            processed = self._postprocess_image(p, pp, *_args[:len(all_args)])
            outimage = pp.image
            # update info
            info = outimage.info["parameters"]
            nparams = parse_prompt(info)
            if len(params) > 0:
                for k, v in nparams.items():
                    if "MuDDetailer" in k:
                        params[k] = v
            else:
                params = nparams

            prompt = params.pop("Prompt")
            neg_prompt = params.pop("Negative prompt")
            generation_params = ", ".join([k if k == v else f'{k}: {quote(v)}' for k, v in params.items() if v is not None])

            info = prompt + "\nNegative prompt:" + neg_prompt + "\n" + generation_params

            images.save_image(outimage, outpath, "", seed, p.prompt, opts.samples_format, info=info, p=p)

            shared.total_tqdm.clear()

            try:
                with open(os.path.join(data_path, "params.txt"), "w", encoding="utf8") as file:
                    file.write(info)
            except:
                pass

            # update generation info if
            geninfo = ""
            if generation_info.strip() != "":
                try:
                    generation_info = json.loads(generation_info)
                    generation_info["all_prompts"].append(processed.prompt)
                    generation_info["all_negative_prompts"].append(processed.negative_prompt)
                    generation_info["all_seeds"].append(processed.seed)
                    generation_info["all_subseeds"].append(processed.subseed)
                    generation_info["infotexts"].append(processed.infotexts[0])

                    geninfo = json.dumps(generation_info, ensure_ascii=False)
                except Exception:
                    geninfo = processed.js()
                    pass
            else:
                geninfo = processed.js()

            # prepare gallery dict to acceptable tuple
            gal = []
            for g in gallery:
                if type(g) is list:
                    gal.append((g[0]["name"], g[1]))
                else:
                    gal.append(g["name"])
            gal.append(outimage)

            # prepare outputs
            masks_a = processed.masks_a
            labs_a = []
            if type(masks_a) is dict and masks_a.get("bboxes", None):
                scores = masks_a["scores"]
                labs_a = [f"{lab}{scores[i]>0 and f' {round(scores[i],2)}' or ''}:{i+1}" for i, lab in enumerate(masks_a["labels"])]
            elif type(masks_a) is str:
                tmp = masks_a.split(",")
                if len(tmp) == 5:
                    # only one detection case.
                    labs_a = [f"{tmp[0]}:1"]

            masks_b = processed.masks_b
            labs_b = []
            if type(masks_b) is dict and masks_b.get("bboxes", None):
                scores = masks_b["scores"]
                labs_b = [f"{lab}{scores[i]>0 and f' {round(scores[i],2)}' or ''}:{i+1}" for i, lab in enumerate(masks_b["labels"])]
            elif type(masks_b) is str:
                tmp = masks_b.split(",")
                if len(tmp) == 5:
                    # only one detection case.
                    labs_b = [f"{tmp[0]}:1"]

            return (image if input is None else gr.update(), gal, geninfo,
                gr.update(visible=True if len(labs_a) > 0 else False),
                gr.update(visible=True if len(labs_b) > 0 else False),
                json.dumps(masks_a, separators=(",",":")) if type(masks_a) is dict else masks_a,
                json.dumps(masks_b, separators=(",",":")) if type(masks_b) is dict else masks_b,
                gr.update(choices=labs_a, visible=True if len(labs_a) > 0 else False),
                gr.update(choices=labs_b, visible=True if len(labs_b) > 0 else False),
                plaintext_to_html(info))

        def import_image_from_gallery(gallery, idx):
            if len(gallery) == 0:
                return None
            if idx > len(gallery):
                idx = len(gallery) - 1
            if isinstance(gallery[idx], dict) and gallery[idx].get("name", None) is not None:
                name = gallery[idx]["name"]
                if name.find("?") > 0:
                    name = name[:name.rfind("?")]
                print("Import ", name)
                image = Image.open(name)
                return image
            elif isinstance(gallery[idx], np.ndarray):
                return gallery[idx]
            else:
                print("Invalid gallery image {type(gallery[0]}")
            return None

        def on_after_components(component, **kwargs):
            elem_id = getattr(component, "elem_id", None)
            if elem_id is None:
                return

            self.init_on_after_callback = True

        # from supermerger GenParamGetter.py
        def compare_components_with_ids(components: list[gr.Blocks], ids: list[int]):
            return len(components) == len(ids) and all(component._id == _id for component, _id in zip(components, ids))

        def on_app_started(demo, app):
            DD = MuDetectionDetailerScript

            for _id, is_txt2img in zip([DD.components["txt2img_generate"]._id, DD.components["img2img_generate"]._id], [True, False]):
                dependencies = [x for x in demo.dependencies if x["trigger"] == "click" and _id in x["targets"]]
                dependency = None

                for d in dependencies:
                    if "js" in d and d["js"] in [ "submit", "submit_img2img" ]:
                        dependency = d

                params = [params for params in demo.fns if compare_components_with_ids(params.inputs, dependency["inputs"])]

                if is_txt2img:
                    DD.components["txt2img_elem_ids"] = [x.elem_id if hasattr(x,"elem_id") else "None" for x in params[0].inputs]
                else:
                    DD.components["img2img_elem_ids"] = [x.elem_id if hasattr(x,"elem_id") else "None" for x in params[0].inputs]

                if is_txt2img:
                    DD.components["txt2img_params"] = params[0].inputs
                else:
                    DD.components["img2img_params"] = params[0].inputs


            if not self.init_on_app_started:
                if not is_img2img:
                    script_args = DD.components["txt2img_params"][DD.components["txt2img_elem_ids"].index("txt2img_override_settings")+1:]
                else:
                    script_args = DD.components["img2img_params"][DD.components["img2img_elem_ids"].index("img2img_override_settings")+1:]

                with demo:
                    if not is_img2img:
                        dd_run_inpaint.click(
                            fn=wrap_gradio_gpu_call(run_inpaint, extra_outputs=[None, '', '']),
                            _js="mudd_inpainting",
                            inputs=[ dummy_component, dummy_component, dummy_component, dd_image, DD.components["txt2img_gallery"], DD.components["generation_info_txt2img"], *get_txt2img_components(), *all_args, *script_args],
                            outputs=[
                                dd_image,
                                DD.components["txt2img_gallery"],
                                DD.components["generation_info_txt2img"],
                                select_group_a, select_group_b,
                                masks_a, masks_b, labels_a, labels_b,
                                DD.components["html_info_txt2img"]],
                            show_progress=False,
                        )
                    else:
                        dd_run_inpaint.click(
                            fn=wrap_gradio_gpu_call(run_inpaint, extra_outputs=[None, '', '']),
                            _js="mudd_inpainting_img2img",
                            inputs=[ dummy_component, dummy_component, dummy_component, DD.components["img2img_image"], DD.components["img2img_gallery"], DD.components["generation_info_txt2img"], *get_img2img_components(), *all_args, *script_args],
                            outputs=[
                                DD.components["img2img_image"],
                                DD.components["img2img_gallery"],
                                DD.components["generation_info_img2img"],
                                select_group_a, select_group_b,
                                masks_a, masks_b, labels_a, labels_b,
                                DD.components["html_info_img2img"]],
                            show_progress=False,
                        )

            self.init_on_app_started = True

        # set callback only once
        if self.init_on_after_callback is False:
            script_callbacks.on_after_component(on_after_components)

        if self.init_on_app_started is False:
            script_callbacks.on_app_started(on_app_started)

        return [enabled, *all_args]

    def get_seed(self, p) -> tuple[int, int]:
        i = p._idx

        if not p.all_seeds:
            seed = p.seed
        elif i < len(p.all_seeds):
            seed = p.all_seeds[i]
        else:
            j = i % len(p.all_seeds)
            seed = p.all_seeds[j]

        if not p.all_subseeds:
            subseed = p.subseed
        elif i < len(p.all_subseeds):
            subseed = p.all_subseeds[i]
        else:
            j = i % len(p.all_subseeds)
            subseed = p.all_subseeds[j]

        return seed, subseed

    def script_filter(self, p):
        if p.scripts is None:
            return None
        script_runner = copy(p.scripts)

        default = "dynamic_prompting,dynamic_thresholding,wildcards,wildcard_recursive"
        script_names = default
        script_names_set = {
            name
            for script_name in script_names.split(",")
            for name in (script_name, script_name.strip())
        }

        filtered_alwayson = []
        for script_object in script_runner.alwayson_scripts:
            filepath = script_object.filename
            filename = Path(filepath).stem
            if filename in script_names_set:
                filtered_alwayson.append(script_object)

        script_runner.alwayson_scripts = filtered_alwayson
        return script_runner

    def process(self, p, *args):
        if getattr(p, "_disable_muddetailer", False):
            return

        self._image_masks = []


    def postprocess(self, p, processed, *args):
        if getattr(p, "_disable_muddetailer", False):
            return

        final_count = len(processed.images)
        # fix grid infotext
        if (opts.return_grid or opts.grid_save) and not p.do_not_save_grid and (p.n_iter > 1 or p.batch_size > 1) and final_count > 1:
            p_txt = copy(p)
            # remove detection infos from the grid image
            p_txt.extra_generation_params.pop("MuDDetailer detection a", None)
            p_txt.extra_generation_params.pop("MuDDetailer detection b", None)
            info = processing.create_infotext(p_txt, p_txt.all_prompts, p_txt.all_seeds, p_txt.all_subseeds, None, 0, 0)
            # replace infotext
            processed.infotexts[0] = info
            processed.images[0].info["parameters"] = info

        if len(self._image_masks) == 0:
            return

        grid_image = None
        if (opts.return_grid or opts.grid_save) and not p.do_not_save_grid and (p.n_iter > 1 or p.batch_size > 1) and final_count > 1:
            # recheck size of the grid image
            if processed.images[0].size[0] > processed.images[1].size[0]:
                # grid image always bigger then other images
                grid_image = processed.images[0]
                processed.images = processed.images[1:]
                grid_texts = processed.infotexts[0]
                processed.infotexts = processed.infotexts[1:]

        # insert any masks into results if available
        images = [[*masks, image] for masks, image in zip(self._image_masks, processed.images)]
        processed.images = [image for sub in images for image in sub]
        # copy infotext to mask images
        infos = [[info] * (len(masks) + 1) for masks, info in zip(self._image_masks, processed.infotexts)]
        processed.infotexts = [info for sub in infos for info in sub]

        if grid_image is not None:
            processed.images = [grid_image] + processed.images
            processed.infotexts = [grid_texts] + processed.infotexts


    def _postprocess_image(self, p, pp, use_prompt_edit, use_prompt_edit_2,
                     dd_model_a, dd_classes_a,
                     dd_conf_a, dd_max_per_img_a,
                     dd_detect_order_a, dd_select_masks_a,
                     dd_dilation_factor_a,
                     dd_offset_x_a, dd_offset_y_a,
                     dd_prompt, dd_neg_prompt,
                     dd_preprocess_b, dd_bitwise_op,
                     dd_model_b, dd_classes_b,
                     dd_conf_b, dd_max_per_img_b,
                     dd_detect_order_b, dd_select_masks_b,
                     dd_dilation_factor_b,
                     dd_offset_x_b, dd_offset_y_b,
                     dd_prompt_2, dd_neg_prompt_2,
                     dd_mask_blur, dd_denoising_strength,
                     dd_inpaint_full_res, dd_inpaint_full_res_padding,
                     dd_cfg_scale, dd_steps, dd_noise_multiplier,
                     dd_sampler, dd_checkpoint, dd_vae, dd_clipskip):

        p._idx = getattr(p, "_idx", -1) + 1
        p._inpainting = getattr(p, "_inpainting", False)

        seed, subseed = self.get_seed(p)
        p.seed = seed
        p.subseed = subseed

        info = ""
        ddetail_count = 1

        # get some global settings
        use_max_per_img = shared.opts.data.get("mudd_max_per_img", 20)
        # set max_per_img
        dd_max_per_img_a = dd_max_per_img_a if dd_max_per_img_a > 0 else use_max_per_img
        dd_max_per_img_b = dd_max_per_img_b if dd_max_per_img_b > 0 else use_max_per_img

        sampler_name = dd_sampler if dd_sampler not in [ "Use same sampler", "Default", "None" ] else p.sampler_name
        if sampler_name in ["PLMS", "UniPC"]:
            sampler_name = "Euler"

        # setup override settings
        checkpoint = dd_checkpoint if dd_checkpoint not in [ "Use same checkpoint", "Default", "None" ] else None
        clipskip = dd_clipskip if dd_clipskip > 0 else None
        vae = dd_vae if dd_vae not in [ "Use same VAE", "Default", "None" ] else None
        override_settings = {}
        if checkpoint is not None:
            override_settings["sd_model_checkpoint"] = checkpoint
        if vae is not None:
            override_settings["sd_vae"] = vae
        if clipskip is not None:
            override_settings["CLIP_stop_at_last_layers"] = clipskip

        p_txt = copy(p)

        prompt = dd_prompt if use_prompt_edit and dd_prompt else p_txt.prompt
        neg_prompt = dd_neg_prompt if use_prompt_edit and dd_neg_prompt else p_txt.negative_prompt

        # prepare dd_select_masks_*
        selected_a = parse_select_masks(dd_select_masks_a, "A")
        selected_b = parse_select_masks(dd_select_masks_b, "B")
        select_masks_a = selected_a["A"]
        select_masks_b = selected_b["B"]
        #dd_select_masks_a = ",".join(zip_ranges(select_masks_a))
        #dd_select_masks_b = ",".join(zip_ranges(select_masks_b))

        # 0 could be acceptable.
        select_masks_a = [x-1 for x in select_masks_a if x >= 0]
        select_masks_b = [x-1 for x in select_masks_b if x >= 0]
        select_masks_a = None if len(select_masks_a) == 0 else select_masks_a
        select_masks_b = None if len(select_masks_b) == 0 else select_masks_b

        # ddetailer info
        extra_params = ddetailer_extra_params(
            use_prompt_edit,
            use_prompt_edit_2,
            dd_model_a, dd_classes_a,
            dd_conf_a, dd_max_per_img_a,
            dd_detect_order_a, dd_select_masks_a,
            dd_dilation_factor_a,
            dd_offset_x_a, dd_offset_y_a,
            dd_prompt, dd_neg_prompt,
            dd_preprocess_b, dd_bitwise_op,
            dd_model_b, dd_classes_b,
            dd_conf_b, dd_max_per_img_b,
            dd_detect_order_b, dd_select_masks_b,
            dd_dilation_factor_b,
            dd_offset_x_b, dd_offset_y_b,
            dd_prompt_2, dd_neg_prompt_2,
            dd_mask_blur, dd_denoising_strength,
            dd_inpaint_full_res, dd_inpaint_full_res_padding,
            dd_cfg_scale, dd_steps, dd_noise_multiplier,
            dd_sampler, dd_checkpoint, dd_vae, dd_clipskip,
        )
        p_txt.extra_generation_params.update(extra_params)

        cfg_scale = dd_cfg_scale if dd_cfg_scale > 0 else p_txt.cfg_scale
        steps = dd_steps if dd_steps > 0 else p_txt.steps
        initial_noise_multiplier = dd_noise_multiplier if dd_noise_multiplier > 0 else None

        p = StableDiffusionProcessingImg2Img(
                init_images = [pp.image],
                resize_mode = 0,
                denoising_strength = dd_denoising_strength,
                mask = None,
                mask_blur= dd_mask_blur,
                inpainting_fill = 1,
                inpaint_full_res = dd_inpaint_full_res,
                inpaint_full_res_padding= dd_inpaint_full_res_padding,
                inpainting_mask_invert= 0,
                initial_noise_multiplier=initial_noise_multiplier,
                sd_model=p_txt.sd_model,
                outpath_samples=p_txt.outpath_samples,
                outpath_grids=p_txt.outpath_grids,
                prompt=prompt,
                negative_prompt=neg_prompt,
                styles=p_txt.styles,
                seed=p_txt.seed,
                subseed=p_txt.subseed,
                subseed_strength=p_txt.subseed_strength,
                seed_resize_from_h=p_txt.seed_resize_from_h,
                seed_resize_from_w=p_txt.seed_resize_from_w,
                sampler_name=sampler_name,
                batch_size=1,
                n_iter=1,
                steps=steps,
                cfg_scale=cfg_scale,
                width=p_txt.width,
                height=p_txt.height,
                tiling=p_txt.tiling,
                extra_generation_params=p_txt.extra_generation_params,
                override_settings=override_settings,
            )
        p.scripts = self.script_filter(p_txt)
        p.script_args = deepcopy(p_txt.script_args) if p_txt.script_args is not None else {}

        p.do_not_save_grid = True
        p.do_not_save_samples = True

        p._disable_muddetailer = True

        # reset tqdm for inpainting helper mode
        if p_txt._inpainting:
            shared.total_tqdm.updateTotal(0)

        # init random
        rand_seed = p_txt.seed
        while rand_seed > 2**32 - 1:
            rand_seed >>= 1
        np.random.seed(rand_seed)

        detected_a = {}
        detected_b = {}
        output_images = []
        segmask_preview_a = None
        segmask_preview_b = None
        save_jobcount = state.job_count

        info = processing.create_infotext(p_txt, p_txt.all_prompts, p_txt.all_seeds, p_txt.all_subseeds, None, 0, 0)
        processed = Processed(
            p_txt,
            images_list=output_images,
            seed=p_txt.all_seeds[0],
            info=info,
            subseed=p_txt.all_subseeds[0],
            infotexts=[info],
        )

        # prepare gray_image
        npimg = np.array(pp.image)
        npimg = npimg[:, :, ::-1].copy()
        gray_image = cv2.cvtColor(npimg, cv2.COLOR_BGR2GRAY)

        def info_results(results):
            bboxes = [bbox.astype(np.intp).tolist() for bbox in results[1]]
            scores = [round(score.item(), 4) for score in results[3]]
            detected = {"bboxes": bboxes, "labels": results[0], "scores": scores}
            if len(results[2]) > 0:
                polylines = create_polyline_from_segms(results[2])
                detected["segms"] = polylines

            # only one detection with bbox case -> A-face 0.92,100,120,190,200
            if len(results[2]) == 0 and len(bboxes) == 1:
                detected = results[0][0] + f" {scores[0]}" + "," + ",".join([str(x) for x in bboxes[0]])
            return detected

        for n in range(ddetail_count):
            devices.torch_gc()
            start_seed = seed + n
            init_image = copy(pp.image)

            output_images.append(init_image)
            masks_a = []
            masks_b_pre = []

            # Optional secondary pre-processing run
            if (dd_model_b != "None" and dd_preprocess_b):
                label_b_pre = "B"
                results_b_pre = inference(init_image, dd_model_b, dd_conf_b/100.0, label_b_pre, dd_classes_b, dd_max_per_img_b)
                results_b_pre = sort_results(results_b_pre, dd_detect_order_b)

                detected_b = info_results(results_b_pre)

                detected = len(results_b_pre[1])
                print(f"Total {detected} {'was' if detected == 1 else 'were'} detected with model {label_b_pre}...")

                masks_b_pre = create_segmasks(gray_image, results_b_pre)
                masks_b_pre = dilate_masks(masks_b_pre, dd_dilation_factor_b, 1)
                masks_b_pre = offset_masks(masks_b_pre,dd_offset_x_b, dd_offset_y_b)
                if (len(masks_b_pre) > 0):
                    results_b_pre = update_result_masks(results_b_pre, masks_b_pre)
                    segmask_preview_b = create_segmask_preview(results_b_pre, init_image, select_masks_b)
                    shared.state.assign_current_image(segmask_preview_b)
                    if ( opts.mudd_save_previews):
                        images.save_image(segmask_preview_b, p_txt.outpath_samples, "", start_seed, p.prompt, opts.samples_format, info=info, p=p)

                    if select_masks_b:
                        gen_selected = [i for i in select_masks_b if i < len(masks_b_pre) and i >= 0]
                    else:
                        gen_selected = range(len(masks_b_pre))
                    state.job_count += len(gen_selected)

                    selected = len(gen_selected)
                    print(f"Processing {selected} detection{'s' if selected > 1 else ''} of model {label_b_pre} for output generation {p_txt._idx + 1}.")

                    p2 = copy(p)
                    p2.seed = start_seed
                    p2.init_images = [init_image]

                    # prompt/negative_prompt for pre-processing
                    p2.prompt = dd_prompt_2 if use_prompt_edit_2 and dd_prompt_2 else p_txt.prompt
                    p2.negative_prompt = dd_neg_prompt_2 if use_prompt_edit_2 and dd_neg_prompt_2 else p_txt.negative_prompt

                    # get img2img sampler steps and update total tqdm
                    _, sampler_steps = sd_samplers_common.setup_img2img_steps(p)
                    if len(gen_selected) > 0 and shared.total_tqdm._tqdm is not None:
                        shared.total_tqdm.updateTotal(shared.total_tqdm._tqdm.total + (sampler_steps + 1) * len(gen_selected))

                    for i in gen_selected:
                        p2.image_mask = masks_b_pre[i]
                        if ( opts.mudd_save_masks):
                            images.save_image(masks_b_pre[i], p_txt.outpath_samples, "", start_seed, p2.prompt, opts.samples_format, info=info, p=p2)
                        processed = processing.process_images(p2)

                        p2.seed = processed.seed + 1
                        p2.subseed = processed.subseed + 1
                        p2.init_images = processed.images

                    if (len(gen_selected) > 0):
                        output_images[n] = processed.images[0]
                        init_image = processed.images[0]

                else:
                    print(f"No model B detection for output generation {p_txt._idx + 1} with current settings.")

            # Primary run
            if (dd_model_a != "None"):
                label_a = "A"
                if (dd_model_b != "None" and dd_bitwise_op != "None"):
                    label_a = dd_bitwise_op
                results_a = inference(init_image, dd_model_a, dd_conf_a/100.0, label_a, dd_classes_a, dd_max_per_img_a)
                results_a = sort_results(results_a, dd_detect_order_a)

                detected_a = info_results(results_a)

                detected = len(results_a[1])
                print(f"Total {detected} {'was' if detected == 1 else 'were'} detected by model {label_a}...")

                masks_a = create_segmasks(gray_image, results_a)
                masks_a = dilate_masks(masks_a, dd_dilation_factor_a, 1)
                masks_a = offset_masks(masks_a,dd_offset_x_a, dd_offset_y_a)
                if (dd_model_b != "None" and dd_bitwise_op != "None"):
                    label_b = "B"
                    results_b = inference(init_image, dd_model_b, dd_conf_b/100.0, label_b, dd_classes_b, dd_max_per_img_b)
                    results_b = sort_results(results_b, dd_detect_order_b)

                    detected_b = info_results(results_b)

                    detected = len(results_b[1])
                    print(f"Total {detected} {'was' if detected == 1 else 'were'} detected by model {label_b}...")

                    masks_b = create_segmasks(gray_image, results_b)
                    masks_b = dilate_masks(masks_b, dd_dilation_factor_b, 1)
                    masks_b = offset_masks(masks_b,dd_offset_x_b, dd_offset_y_b)
                    if (len(masks_b) > 0):
                        combined_mask_b = combine_masks(masks_b)
                        for i in reversed(range(len(masks_a))):
                            if (dd_bitwise_op == "A&B"):
                                masks_a[i] = bitwise_and_masks(masks_a[i], combined_mask_b)
                            elif (dd_bitwise_op == "A-B"):
                                masks_a[i] = subtract_masks(masks_a[i], combined_mask_b)
                            if (is_allblack(masks_a[i])):
                                del masks_a[i]
                                for result in results_a:
                                    del result[i]

                    else:
                        print("No model B detection to overlap with model A masks")
                        results_a = []
                        masks_a = []

                if (len(masks_a) > 0):
                    results_a = update_result_masks(results_a, masks_a)
                    segmask_preview_a = create_segmask_preview(results_a, init_image, select_masks_a)
                    shared.state.assign_current_image(segmask_preview_a)
                    if ( opts.mudd_save_previews):
                        images.save_image(segmask_preview_a, p_txt.outpath_samples, "", start_seed, p.prompt, opts.samples_format, info=info, p=p)

                    if select_masks_a:
                        gen_selected = [i for i in select_masks_a if i < len(masks_a) and i >= 0]
                    else:
                        gen_selected = range(len(masks_a))

                    state.job_count += len(gen_selected)

                    selected = len(gen_selected)
                    print(f"Processing {selected} detection{'s' if selected > 1 else ''} of model {label_a} for output generation {p_txt._idx + 1}.")

                    p.seed = start_seed
                    p.init_images = [init_image]

                    # get img2img sampler steps and update total tqdm
                    _, sampler_steps = sd_samplers_common.setup_img2img_steps(p)
                    if len(gen_selected) > 0 and shared.total_tqdm._tqdm is not None:
                        shared.total_tqdm.updateTotal(shared.total_tqdm._tqdm.total + (sampler_steps + 1) * len(gen_selected))

                    for i in gen_selected:
                        p.image_mask = masks_a[i]
                        if ( opts.mudd_save_masks):
                            images.save_image(masks_a[i], p_txt.outpath_samples, "", start_seed, p.prompt, opts.samples_format, info=info, p=p)

                        processed = processing.process_images(p)
                        p.seed = processed.seed + 1
                        p.subseed = processed.subseed + 1
                        p.init_images = processed.images

                    if len(gen_selected) > 0 and len(processed.images) > 0:
                        output_images[n] = processed.images[0]

                else:
                    print(f"No model {label_a} detections for output generation {p_txt._idx + 1} with current settings.")
            state.job = f"Generation {p_txt._idx + 1} out of {state.job_count}"

        masks_params = {}
        if len(detected_a) > 0:
            masks_params["MuDDetailer detection a"] = json.dumps(detected_a, separators=(",", ":")) if type(detected_a) is dict else detected_a
        if len(detected_b) > 0:
            masks_params["MuDDetailer detection b"] = json.dumps(detected_b, separators=(",", ":")) if type(detected_b) is dict else detected_b

        if len(masks_params) > 0:
            p_txt.extra_generation_params.update(masks_params)
            info = processing.create_infotext(p_txt, p_txt.all_prompts, p_txt.all_seeds, p_txt.all_subseeds, None, 0, 0)

        processed.masks_a = detected_a
        processed.masks_b = detected_b
        processed.infotexts[0] = info

        # append masks if needed case
        if getattr(self, "_image_masks", None) is not None:
            self._image_masks.append([])

        if len(output_images) > 0:
            pp.image = output_images[0]
            pp.image.info["parameters"] = info

            if p.extra_generation_params.get("Noise multiplier") is not None:
                p.extra_generation_params.pop("Noise multiplier")
        if state.job_count == save_jobcount and getattr(self, "_image_masks", None) is not None:
            if segmask_preview_a is not None:
                segmask_preview_a.info["parameters"] = info
                self._image_masks[-1].append(segmask_preview_a)
            if segmask_preview_b is not None:
                segmask_preview_b.info["parameters"] = info
                self._image_masks[-1].append(segmask_preview_b)

        return processed

    def postprocess_image(self, p, pp, *_args):
        if getattr(p, "_disable_muddetailer", False):
            return

        if type(_args[0]) is bool:
            (enabled, use_prompt_edit, use_prompt_edit_2,
                     dd_model_a, dd_classes_a,
                     dd_conf_a, dd_max_per_img_a,
                     dd_detect_order_a, dd_select_masks_a,
                     dd_dilation_factor_a,
                     dd_offset_x_a, dd_offset_y_a,
                     dd_prompt, dd_neg_prompt,
                     dd_preprocess_b, dd_bitwise_op,
                     dd_model_b, dd_classes_b,
                     dd_conf_b, dd_max_per_img_b,
                     dd_detect_order_b, dd_select_masks_b,
                     dd_dilation_factor_b,
                     dd_offset_x_b, dd_offset_y_b,
                     dd_prompt_2, dd_neg_prompt_2,
                     dd_mask_blur, dd_denoising_strength,
                     dd_inpaint_full_res, dd_inpaint_full_res_padding,
                     dd_cfg_scale, dd_steps, dd_noise_multiplier,
                     dd_sampler, dd_checkpoint, dd_vae, dd_clipskip) = (*_args,)
        else:
            # for API
            args = _args[0]

            enabled = args.get("enabled", False)
            use_prompt_edit = args.get("use prompt edit", False)
            use_prompt_edit_2 = args.get("use prompt edit b", False)
            dd_model_a = args.get("model a", "None")
            dd_classes_a = args.get("classes a", [])
            dd_conf_a = args.get("conf a", 30)
            dd_max_per_img_a = args.get("max detection a", 0)
            dd_detect_order_a = args.get("detect order a", [])
            dd_select_masks_a = args.get("select masks a", '')
            dd_dilation_factor_a = args.get("dilation a", 4)
            dd_offset_x_a = args.get("offset x a", 0)
            dd_offset_y_a = args.get("offset y a", 0)
            dd_prompt = args.get("prompt", "")
            dd_neg_prompt = args.get("negative prompt", "")

            dd_preprocess_b = args.get("preprocess b", False)
            dd_bitwise_op = args.get("bitwise", "None")

            dd_model_b = args.get("model b", "None")
            dd_classes_b = args.get("classes b", [])
            dd_conf_b = args.get("conf b", 30)
            dd_max_per_img_b = args.get("max detection b", 0)
            dd_detect_order_b = args.get("detect order b", [])
            dd_select_masks_b = args.get("select masks b", '')
            dd_dilation_factor_b = args.get("dilation b", 4)
            dd_offset_x_b = args.get("offset x b", 0)
            dd_offset_y_b = args.get("offset y b", 0)
            dd_prompt_2 = args.get("prompt b", "")
            dd_neg_prompt_2 = args.get("negative prompt b", "")

            dd_mask_blur = args.get("mask blur", 4)
            dd_denoising_strength = args.get("denoising strength", 0.4)
            dd_inpaint_full_res = args.get("inpaint full", True)
            dd_inpaint_full_res_padding = args.get("inpaint full padding", 32)
            dd_cfg_scale = args.get("CFG scale", 0)
            dd_steps = args.get("steps", 0)
            dd_noise_multiplier = args.get("noise multiplier", 0)
            dd_sampler = args.get("sampler", "None")
            dd_checkpoint = args.get("checkpoint", "None")
            dd_vae = args.get("VAE", "None")
            dd_clipskip = args.get("CLIP skip", 0)

        # some check for API
        if dd_classes_a is str:
            if dd_classes_a.find(",") != -1:
                dd_classes_a = [x.strip() for x in dd_classes_a.split(",")]
        if dd_classes_a == "None":
            dd_classes_a = None

        if dd_classes_b is str:
            if dd_classes_b.find(",") != -1:
                dd_classes_b = [x.strip() for x in dd_classes_b.split(",")]
        if dd_classes_b == "None":
            dd_classes_b = None

        if dd_detect_order_a is str:
            if dd_detect_order_a.find(",") != -1:
                dd_detect_order_a = [x.strip() for x in dd_detect_order_a.split(",")]
        if dd_detect_order_a == "None":
            dd_detect_order_a = None

        if dd_detect_order_b is str:
            if dd_detect_order_b.find(",") != -1:
                dd_detect_order_b = [x.strip() for x in dd_detect_order_b.split(",")]
        if dd_detect_order_b == "None":
            dd_detect_order_b = None

        valid_orders = ["area", "position"]
        dd_detect_order_a = list(set(valid_orders) & set(dd_detect_order_a))
        dd_detect_order_b = list(set(valid_orders) & set(dd_detect_order_b))

        if not enabled:
            return

        self._postprocess_image(p, pp, use_prompt_edit, use_prompt_edit_2,
                     dd_model_a, dd_classes_a,
                     dd_conf_a, dd_max_per_img_a,
                     dd_detect_order_a, dd_select_masks_a,
                     dd_dilation_factor_a,
                     dd_offset_x_a, dd_offset_y_a,
                     dd_prompt, dd_neg_prompt,
                     dd_preprocess_b, dd_bitwise_op,
                     dd_model_b, dd_classes_b,
                     dd_conf_b, dd_max_per_img_b,
                     dd_detect_order_b, dd_select_masks_b,
                     dd_dilation_factor_b,
                     dd_offset_x_b, dd_offset_y_b,
                     dd_prompt_2, dd_neg_prompt_2,
                     dd_mask_blur, dd_denoising_strength,
                     dd_inpaint_full_res, dd_inpaint_full_res_padding,
                     dd_cfg_scale, dd_steps, dd_noise_multiplier,
                     dd_sampler, dd_checkpoint, dd_vae, dd_clipskip)

        p.close()

def quote(text):
    if ',' not in str(text) and '\n' not in str(text) and ':' not in str(text):
        return text

    return json.dumps(text, ensure_ascii=False)

def unquote(text):
    if len(text) == 0 or text[0] != '"' or text[-1] != '"':
        return text

    try:
        return json.loads(text)
    except Exception:
        return text

# from modules/generation_parameters_copypaste.py
re_param_code = r'\s*(\w[\w \-/]+):\s*("(?:\\"[^,]|\\"|\\|[^\"])+"|[^,]*)(?:,|$)'
re_param = re.compile(re_param_code)

def parse_prompt(x: str):
    """from parse_generation_parameters(x: str)"""
    res = {}

    prompt = ""
    negative_prompt = ""

    done_with_prompt = False

    *lines, lastline = x.strip().split("\n")
    if len(re_param.findall(lastline)) < 3:
        lines.append(lastline)
        lastline = ''

    for line in lines:
        line = line.strip()
        if line.startswith("Negative prompt:"):
            done_with_prompt = True
            line = line[16:].strip()
        if done_with_prompt:
            negative_prompt += ("" if negative_prompt == "" else "\n") + line
        else:
            prompt += ("" if prompt == "" else "\n") + line

    for k, v in re_param.findall(lastline):
        try:
            if v[0] == '"' and v[-1] == '"':
                v = unquote(v)

            res[k] = v
        except Exception:
            print(f"Error parsing \"{k}: {v}\"")

    res["Prompt"] = prompt
    res["Negative prompt"] = negative_prompt

    return res


def parse_select_masks(line, default_lab="A"):
    """
    Parse selected masks line

    A:1,B:2-4,A:3-7 => A:[1,3,4,5,6,7] B:[2,3,4]
    """
    tmp = [x.strip() for x in line.strip().split(",")]

    parts = {"A": [], "B": []}
    for x in tmp:
        if ":" in x:
            lab, num = x.rsplit(":", 1)
        else:
            lab = default_lab
            num = x

        if '-' in num:
            nums = [
                int(x.strip()) for x in num.split("-") if x.strip().isdigit()
            ]
            if len(nums) == 2:
                nums = list(range(nums[0], nums[1] + 1))
            else:
                continue
        elif num != '':
            nums = [int(num)]
        else:
            continue

        if lab.startswith("B"):
            parts["B"] = parts["B"] + nums
        elif lab.startswith("A"):
            parts["A"] = parts["A"] + nums
        else:
            continue

    parts["A"] = list(set(parts["A"]))
    parts["B"] = list(set(parts["B"]))
    return parts


def zip_ranges(inp):
    """
    Zip ranges

    2,3,4,7,8,9=>2-4,7-9
    """
    if len(inp) == 0:
        return inp

    inp = list(set(inp))

    start = inp[0]
    end = inp[0]

    ranges = []
    for x in inp[1:]:
        if x == end + 1:
            end += 1
            continue
        else:
            if start == end:
                ranges.append(str(start))
            elif start + 1 == end:
                ranges.append(str(start))
                ranges.append(str(end))
            else:
                ranges.append(f"{start}-{end}")

            start = end = x

    if start == end:
        ranges.append(str(start))
    elif start + 1 == end:
        ranges.append(str(start))
        ranges.append(str(end))
    else:
        ranges.append(f"{start}-{end}")

    return ranges


def modeldataset(model_shortname):
    path = modelpath(model_shortname)
    if "mmdet" in path and ("segm" in path or "coco" in model_shortname):
        dataset = 'coco'
    else:
        dataset = 'bbox'
    return dataset

def match_modelname(modelname):
    model_list = list_models()

    if modelname.find("[") == -1:
        for model in model_list:
            if modelname in model:
                tmp = model.split(" ")[0]
                if modelname == tmp.split("/")[-1]:
                    modelname = model
                    break

    if modelname in models_alias:
        return modelname
    return None


def modelpath(modelname):
    model = match_modelname(modelname)

    if model in models_alias:
        path = models_alias[model]
        model_h = model.split("[")[-1].split("]")[0]
        if model_hash(path) == model_h:
            return path

    raise gr.Error("No matched model found.")


def sort_results(results, orders):
    if len(results[1]) <= 1 or orders is None or len(orders) == 0:
        return results

    bboxes = results[1]
    items = len(bboxes)
    order = range(items)

    # get max size bbox
    sortkey = lambda bbox: -(bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
    tmpord = sorted(order, key=lambda i: sortkey(bboxes[i]))
    # setup marginal variables ~0.2
    bbox = bboxes[tmpord[0]]
    area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
    marginarea = int(area * 0.2)
    marginwidth = int(math.sqrt(area) * 0.2)

    # sort by position (left to light)
    if "position" in orders:
        sortkey = lambda bbox: int(int((bbox[0] + (bbox[2] - bbox[0]) * 0.5)/marginwidth)*marginwidth)
        order = sorted(order, key=lambda i: sortkey(bboxes[i]))

    # sort by area
    if "area" in orders:
        sortkey = lambda bbox: -int(int((bbox[2] - bbox[0]) * (bbox[3] - bbox[1])/marginarea)*marginarea)
        order = sorted(order, key=lambda i: sortkey(bboxes[i]))

    # sort all results
    results[1] = [bboxes[i] for i in order]
    results[0] = [results[0][i] for i in order]
    if len(results[2]) > 0:
        results[2] = [results[2][i] for i in order]
    results[3] = [results[3][i] for i in order]
    return results


def update_result_masks(results, masks):
    boolmasks = []
    for i in range(len(masks)):
        boolmasks.append(np.array(masks[i], dtype=bool))
    results[2] = boolmasks
    return results

def create_segmask_preview(results, image, selected=None):
    use_mediapipe_preview = shared.opts.data.get("mudd_use_mediapipe_preview", False)
    if use_mediapipe_preview and len(results) > 4:
        image = results[4]

    labels = results[0]
    bboxes = results[1]
    segms = results[2]
    scores = results[3]

    cv2_image = np.array(image)
    cv2_image = cv2_image[:, :, ::-1].copy()
    gray = cv2.cvtColor(cv2_image, cv2.COLOR_BGR2GRAY)

    # no segms? simply generate from bboxes
    if len(segms) == 0:
        segms = _create_segms(gray, bboxes)

    if selected is None:
        selected = []

    for i in range(len(bboxes)):
        color = np.full_like(cv2_image, np.random.randint(100, 256, (1, 3), dtype=np.uint8))
        alpha = 0.2
        if i in selected:
            # selected masks change color to white and draw bbox rectangle on it
            #color = np.full_like(cv2_image, np.array([[255, 255, 255]], dtype=np.uint8))
            bbox = bboxes[i]
            cv2.rectangle(cv2_image, (int(bbox[0]), int(bbox[1])), (int(bbox[2]), int(bbox[3])), (0, 255, 0), 3, cv2.LINE_AA)
            alpha = 0.3
        color_image = cv2.addWeighted(cv2_image, alpha, color, 1-alpha, 0)
        cv2_mask = segms[i].astype(np.uint8) * 255
        #cv2_mask_bool = np.array(segms[i], dtype=bool)
        #centroid = np.mean(np.argwhere(cv2_mask_bool),axis=0)
        centroid = np.mean(np.argwhere(segms[i]),axis=0)
        centroid_x, centroid_y = int(centroid[1]), int(centroid[0])

        cv2_mask_rgb = cv2.merge((cv2_mask, cv2_mask, cv2_mask))
        cv2_image = np.where(cv2_mask_rgb == 255, color_image, cv2_image)
        text_color = tuple([int(x) for x in ( color[0][0] - 100 )])
        name = labels[i]
        score = scores[i]

        if score > 0.0:
            score = str(score)[:4]
            text = name + ' ' + score + f":{i+1}"
        else:
            text = name + f":{i+1}"
        (w, h), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_DUPLEX, 0.4, 1)
        cv2.putText(cv2_image, text, (centroid_x - int(w/2), centroid_y), cv2.FONT_HERSHEY_DUPLEX, 0.4, text_color, 1, cv2.LINE_AA)

    if ( len(segms) > 0):
        preview_image = Image.fromarray(cv2.cvtColor(cv2_image, cv2.COLOR_BGR2RGB))
    else:
        preview_image = image

    return preview_image

def is_allblack(mask):
    cv2_mask = np.array(mask)
    return cv2.countNonZero(cv2_mask) == 0

def bitwise_and_masks(mask1, mask2):
    cv2_mask1 = np.array(mask1)
    cv2_mask2 = np.array(mask2)
    cv2_mask = cv2.bitwise_and(cv2_mask1, cv2_mask2)
    mask = Image.fromarray(cv2_mask)
    return mask

def subtract_masks(mask1, mask2):
    cv2_mask1 = np.array(mask1)
    cv2_mask2 = np.array(mask2)
    cv2_mask = cv2.subtract(cv2_mask1, cv2_mask2)
    mask = Image.fromarray(cv2_mask)
    return mask

def dilate_masks(masks, dilation_factor, iter=1):
    if dilation_factor == 0:
        return masks
    dilated_masks = []
    kernel = np.ones((dilation_factor,dilation_factor), np.uint8)
    for i in range(len(masks)):
        cv2_mask = np.array(masks[i])
        dilated_mask = cv2.dilate(cv2_mask, kernel, iter)
        dilated_masks.append(Image.fromarray(dilated_mask))
    return dilated_masks

def offset_masks(masks, offset_x, offset_y):
    if (offset_x == 0 and offset_y == 0):
        return masks
    offset_masks = []
    for i in range(len(masks)):
        cv2_mask = np.array(masks[i])
        offset_mask = cv2_mask.copy()
        offset_mask = np.roll(offset_mask, -offset_y, axis=0)
        offset_mask = np.roll(offset_mask, offset_x, axis=1)

        offset_masks.append(Image.fromarray(offset_mask))
    return offset_masks

def combine_masks(masks):
    initial_cv2_mask = np.array(masks[0])
    combined_cv2_mask = initial_cv2_mask
    for i in range(1, len(masks)):
        cv2_mask = np.array(masks[i])
        combined_cv2_mask = cv2.bitwise_or(combined_cv2_mask, cv2_mask)

    combined_mask = Image.fromarray(combined_cv2_mask)
    return combined_mask

def on_ui_settings():
    section = ("muddetailer", "μ DDetailer")
    shared.opts.add_option(
        "mudd_max_per_img",
        shared.OptionInfo(
            default=20,
            label="Maximum Detection number",
            component=gr.Slider,
            component_args={"minimum": 1, "maximum": 100, "step": 1},
            section=section,
        ),
    )
    shared.opts.add_option("mudd_save_previews", shared.OptionInfo(False, "Save mask previews", section=section))
    shared.opts.add_option("mudd_save_masks", shared.OptionInfo(False, "Save masks", section=section))
    shared.opts.add_option("mudd_import_adetailer", shared.OptionInfo(False, "Import ADetailer options", section=section))
    shared.opts.add_option("mudd_check_validity", shared.OptionInfo(True, "Check validity of model configs on startup", section=section))
    shared.opts.add_option("mudd_check_model_validity", shared.OptionInfo(False, "Check validity of models on startup", section=section))
    shared.opts.add_option("mudd_use_mediapipe_preview", shared.OptionInfo(False, "Use mediapipe preview if available", section=section))


def _create_segms(gray, bboxes):
    segms = []
    for x0, y0, x1, y1 in bboxes:
        # make black (blank) image
        mask = np.zeros((gray.shape), np.uint8)
        # draw white rectangle
        cv2.rectangle(mask, (int(x0), int(y0)), (int(x1), int(y1)), 255, -1)
        mask_bool = mask.astype(bool)
        segms.append(mask_bool)

    return segms


def create_segmasks(gray_image, results):
    bboxes = results[1]
    segms = results[2]

    if len(segms) == 0:
        segms = _create_segms(gray_image, bboxes)

    segmasks = []
    for i in range(len(bboxes)):
        mask = segms[i].astype(np.uint8) * 255
        mask = Image.fromarray(mask)
        segmasks.append(mask)

    return segmasks


def create_polyline_from_segms(segms):
    polys = []
    for i in range(len(segms)):
        mask = segms[i].astype(np.uint8) * 255
        #contours, _ = cv2.findContours(mask, cv2.RETR_TREE, cv2.CHAIN_APPROX_NONE)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        polygons = [np.array(polygon).squeeze().reshape(-1).tolist() for polygon in contours]
        #polygons = [np.array(polygon).squeeze() for polygon in contours]
        polys.append(polygons)
    return polys


import mmcv

try:
    from mmdet.core import get_classes
    from mmdet.apis import inference_detector, init_detector
    from mmcv import Config
    mmcv_legacy = True
except ImportError:
    from mmdet.evaluation import get_classes
    from mmdet.apis import inference_detector, init_detector
    from mmengine.config import Config
    mmcv_legacy = False


def check_validity():
    """check validity of model + config settings"""
    model_list = list_models()
    print(f" Total \033[92m{len(model_list)}\033[0m mmdet models and \033[92m{3}\033[0m mediapipe models.")
    check = shared.opts.data.get("mudd_check_validity", True)
    if not check:
        print(" You can enable validity tester in the Settings-> μ DDetailer.")
        return

    modelcheck = shared.opts.data.get("mudd_check_model_validity", False)

    model_device = get_device()
    valid = 0
    valid_config = 0
    for j, title in enumerate(model_list):
        checkpoint = models_alias[title]
        config = os.path.splitext(checkpoint)[0] + ".py"
        if not os.path.exists(config):
            continue

        try:
            conf = Config.fromfile(config)
            print(f"\033[92mSUCCESS\033[0m - success to load config for {checkpoint}!")
        except Exception as e:
            continue
            print(f"\033[91mFAIL\033[0m - failed to load config for {checkpoint}, please check validity of the config - {e}")
        valid_config += 1

        if not modelcheck:
            if j == 0:
                print(" You can enable model validity tester in the Settings-> μ DDetailer.")
            continue
        # check default scope
        if "yolov8" in config:
            conf["default_scope"] = "mmyolo"
        try:
            if mmcv_legacy:
                model = init_detector(conf, checkpoint, device=model_device)
            else:
                model = init_detector(conf, checkpoint, palette="random", device=model_device)

            print(f"\033[92mSUCCESS\033[0m - success to load {checkpoint}!")
            del model
            valid += 1
        except Exception as e:
            print(f"\033[91mFAIL\033[0m - failed to load {checkpoint}, please check validity of the model - {e}")

        devices.torch_gc()

    if modelcheck:
        print(f" Total \033[92m{valid_config}\033[0m valid mmdet configs, \033[92m{valid}\033[0m models are found.")
    else:
        print(f" Total \033[92m{valid_config}\033[0m valid mmdet configs are found.")
    print(" You can disable validity tester in the Settings-> μ DDetailer.")

def get_device():
    device = devices.get_optimal_device_name()
    if device == "mps":
        return device
    if any(getattr(cmd_opts, vram, False) for vram in ["lowvram", "medvram"]):
        return "cpu"

    return device

# check validity of models
check_validity()

def inference(image, modelname, conf_thres, label, classes=None, max_per_img=100):
    if modelname in ["mediapipe_face_short", "mediapipe_face_full"]:
        results = mp_detector_face(image, modelname, conf_thres, label, classes, max_per_img)
        return results
    elif modelname in ["mediapipe_face_mesh"]:
        results = mp_detector_facemesh(image, modelname, conf_thres, label, classes, max_per_img)
        return results

    path = modelpath(modelname)
    if ( "mmdet" in path and "bbox" in path ):
        results = inference_mmdet_bbox(image, modelname, conf_thres, label, classes, max_per_img)
    elif ( "mmdet" in path and "segm" in path):
        results = inference_mmdet_segm(image, modelname, conf_thres, label, classes, max_per_img)
    elif "yolo/" in path or "yolo\\" in path:
        results = ultra_inference(image, path, conf_thres, label, classes, max_per_img, device=get_device())
    else:
        return [[], [], [], []]
    return results

def inference_mmdet_segm(image, modelname, conf_thres, label, sel_classes, max_per_img):
    model_checkpoint = modelpath(modelname)
    model_config = os.path.splitext(model_checkpoint)[0] + ".py"
    model_device = get_device()

    conf = Config.fromfile(model_config)
    # check default scope
    if "yolov8" in model_config:
        conf["default_scope"] = "mmyolo"

    # setup default values
    conf.merge_from_dict(dict(model=dict(test_cfg=dict(score_thr=conf_thres, max_per_img=max_per_img))))

    segms = []
    bboxes = []
    if mmcv_legacy:
        model = init_detector(conf, model_checkpoint, device=model_device)
        results = inference_detector(model, np.array(image))

        if type(results) is dict:
            print("dict type result")
            results = results["ins_results"]
        else:
            print("tuple type result")

        bboxes = np.vstack(results[0])
        labels = [
            np.full(bbox.shape[0], i, dtype=np.int32)
            for i, bbox in enumerate(results[0])
        ]
        labels = np.concatenate(labels)

        if len(results) > 1:
            segms = results[1]
            segms = mmcv.concat_list(segms)

        scores = bboxes[:, 4]
        bboxes = bboxes[:, :4]
    else:
        model = init_detector(conf, model_checkpoint, palette="random", device=model_device)
        results = inference_detector(model, np.array(image)).pred_instances
        bboxes = results.bboxes.cpu().numpy()
        labels = results.labels
        if "masks" in results:
            segms = results.masks.cpu().numpy()
        scores = results.scores.cpu().numpy()

    if len(segms) == 0:
        # without segms case.
        segms = []
        cv2_image = np.array(image)
        cv2_image = cv2_image[:, :, ::-1].copy()
        cv2_gray = cv2.cvtColor(cv2_image, cv2.COLOR_BGR2GRAY)

        for x0, y0, x1, y1 in bboxes:
            cv2_mask = np.zeros((cv2_gray.shape), np.uint8)
            cv2.rectangle(cv2_mask, (int(x0), int(y0)), (int(x1), int(y1)), 255, -1)
            cv2_mask_bool = cv2_mask.astype(bool)
            segms.append(cv2_mask_bool)

    n, m = bboxes.shape
    results = [[], [], [], []]
    if (n == 0):
        return results

    # get classes info from metadata
    meta = getattr(model, "dataset_meta", None)
    classes = None
    if meta is not None:
        classes = meta.get("classes", None)
        classes = list(classes) if classes is not None else None
    if classes is None:
        dataset = modeldataset(modelname)
        if dataset == "coco":
            classes = get_classes(dataset)
        else:
            classes = None

    filter_inds = np.where(scores > conf_thres)[0]

    # check selected classes
    if type(sel_classes) is str:
        sel_classes = [sel_classes]
    if sel_classes is not None:
        if len(sel_classes) == 0 or (len(sel_classes) == 1 and sel_classes[0] == "None"):
            # "None" selected. in this case, get all dectected classes
            sel_classes = None

    for i in filter_inds:
        lab = label
        if sel_classes is not None and labels is not None and classes is not None:
            cls = classes[labels[i]]
            if cls not in sel_classes:
                continue
            lab += "-" + cls
        elif labels is not None and classes is not None:
            lab += "-" + classes[labels[i]]

        results[0].append(lab)
        results[1].append(bboxes[i])
        results[2].append(segms[i])
        results[3].append(scores[i])

    return results

def inference_mmdet_bbox(image, modelname, conf_thres, label, sel_classes, max_per_img):
    model_checkpoint = modelpath(modelname)
    model_config = os.path.splitext(model_checkpoint)[0] + ".py"
    model_device = get_device()

    conf = Config.fromfile(model_config)
    # check default scope
    if "yolov8" in model_config:
        conf["default_scope"] = "mmyolo"

    # setup default values
    conf.merge_from_dict(dict(model=dict(test_cfg=dict(score_thr=conf_thres, max_per_img=max_per_img))))

    if mmcv_legacy:
        model = init_detector(conf, model_checkpoint, device=model_device)
        results = inference_detector(model, np.array(image))
    else:
        model = init_detector(conf, model_checkpoint, device=model_device, palette="random")
        results = inference_detector(model, np.array(image)).pred_instances

    bboxes = []
    scores = []
    if mmcv_legacy:
        bboxes = np.vstack(results[0])
        scores = bboxes[:,4]
        bboxes = bboxes[:,:4]
        labels = [
            np.full(bbox.shape[0], i, dtype=np.int32)
            for i, bbox in enumerate(results[0])
        ]
        labels = np.concatenate(labels)
    else:
        bboxes = results.bboxes.cpu().numpy()
        scores = results.scores.cpu().numpy()
        labels = results.labels

    n, m = bboxes.shape
    results = [[], [], [], []]
    if (n == 0):
        return results

    # get classes info from metadata
    meta = getattr(model, "dataset_meta", None)
    classes = None
    if meta is not None:
        classes = meta.get("classes", None)
        classes = list(classes) if classes is not None else None
    if classes is None:
        dataset = modeldataset(modelname)
        if dataset == "coco":
            classes = get_classes(dataset)
        else:
            classes = None

    filter_inds = np.where(scores > conf_thres)[0]

    # check selected classes
    if type(sel_classes) is str:
        sel_classes = [sel_classes]
    if sel_classes is not None:
        if len(sel_classes) == 0 or (len(sel_classes) == 1 and sel_classes[0] == "None"):
            # "None" selected. in this case, get all dectected classes
            sel_classes = None

    for i in filter_inds:
        lab = label
        if sel_classes is not None and labels is not None and classes is not None:
            cls = classes[labels[i]]
            if cls not in sel_classes:
                continue
            lab += "-" + cls
        elif labels is not None and classes is not None:
            lab += "-" + classes[labels[i]]

        results[0].append(lab)
        results[1].append(bboxes[i])
        results[3].append(scores[i])

    return results

def on_infotext_pasted(infotext, results):
    updates = {}
    import_adetailer = shared.opts.data.get("mudd_import_adetailer", False)
    adetailer_args = [
        "model", "prompt", "negative prompt", "dilate/erode", "steps",
        "CFG scale", "noise multiplifer", "x offset", "y offset", "CLIP skip",
        "VAE", "checkpoint", "confidence", "mask blur", "denoising strength",
        "inpaint only masked", "inpaint padding"
    ]
    adetailer_models = {
        "face_yolov8n.pt": "face_yolov8n.pth",
        "face_yolov8s.pt": "face_yolov8s.pth",
        "hand_yolov8n.pt": "hand_yolov8n.pth",
        "hand_yolov8s.pt": "hand_yolov8s.pth",
        "mediapipe_face_full": "face_yolov8n.pth",
        "mediapipe_face_short": "face_yolov8n.pth",
        "mediapipe_face_mesh": "face_yolov8n.pth",
        "person_yolov8n-seg.pt": "mmdet_dd-person_mask2former.pth",
        "person_yolov8s-seg.pt": "mmdet_dd-person_mask2former.pth",
    }
    list_model = list_models()
    for k, v in results.items():
        if import_adetailer and k.startswith("ADetailer"):
            key = k
            # import ADetailer params
            if any(x in k for x in adetailer_args):
                # check suffix
                if any(x in k for x in [" 3rd", " 4th", " 5th", " 6th", " 7th"]):
                    # do not support above "3rd" parameters
                    continue
                if "2nd" in k:
                    suffix = " b"
                else:
                    suffix = " a"

                if "confidence" in k:
                    v = int(float(v) * 100)
                elif "dilate" in k:
                    if int(v) < 0: continue
                if all(x not in k for x in ["confidence", "offset", "dilate", "model"]) and suffix != " a":
                    continue
                if "model" in k and v in adetailer_models:
                    m = adetailer_models[v]
                    found = None
                    for model in list_model:
                        if m in model:
                            tmp = model.split(" ")[0]
                            if m == tmp.split("/")[-1]:
                                found = model
                                break
                    if found is not None:
                        v = found
                    else:
                        continue
                elif "model" in k:
                    continue

                k = k.replace("ADetailer ", "MuDDetailer ").replace("x offset", "offset x").replace("y offset", "offset y")
                k = k.replace("dilate/erode", "dilation").replace("negative prompt", "neg prompt").replace("confidence", "conf")
                k = k.replace("denoising strength", "denoising").replace("inpaint only masked", "inpaint full")
                k = k.replace(" 2nd", "").strip()

                if "prompt" in k and suffix != " a":
                    k += suffix
                elif any(x in k for x in
                       ["model", "conf", "offset", "dilation"]):
                    k += suffix

                print(f"import ADetailer param: {key}->{k}: {v}")
                updates[k] = v
                continue

        if not k.startswith("DDetailer") and not k.startswith("MuDDetailer"):
            continue

        # fix old params
        k = k.replace("prompt 2", "prompt b")

        # copy DDetailer options
        if k.startswith("DDetailer"):
            k = k.replace("DDetailer", "MuDDetailer")
            updates[k] = v

        # fix path separator e.g) "bbox\model_name.pth"
        if "model" in k:
            updates[k] = v.replace("\\", "/")

        if k.find(" classes ") > 0 or k.find(" detect order") > 0:
            if v[0] == '"' and v[-1] == '"':
                v = v[1:-1]
            arr = v.split(",")
            updates[k] = arr

    results.update(updates)

def api_version():
    return "1.0.0"

def muddetailer_api(_: gr.Blocks, app: FastAPI):
    @app.get("/muddetailer/version")
    async def version():
        return {"version": api_version()}

    @app.get("/muddetailer/model_list")
    async def model_list(update: bool = True):
        list_model = list_models()
        return {"model_list": list_model}

script_callbacks.on_ui_settings(on_ui_settings)
script_callbacks.on_infotext_pasted(on_infotext_pasted)
script_callbacks.on_app_started(muddetailer_api)
