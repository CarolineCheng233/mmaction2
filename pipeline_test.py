import os.path as osp
import time
import warnings

import mmcv
import torch
from mmcv import Config
from mmcv.runner import init_dist, set_random_seed
from mmcv.utils import get_git_hash

from mmaction import __version__
from mmaction.apis import train_model
from mmaction.datasets import build_dataset
from mmaction.models import build_model
from mmaction.utils import collect_env, get_root_logger

img_norm_cfg = dict(
    mean=[123.675, 116.28, 103.53], std=[58.395, 57.12, 57.375], to_bgr=False)

mc_cfg = dict(
    server_list_cfg='/mnt/lustre/share/memcached_client/server_list.conf',
    client_cfg='/mnt/lustre/share/memcached_client/client.conf',
    sys_path='/mnt/lustre/share/pymc/py3')

cfg = dict(
    type = 'Mp4TextDataset',
    ann_file = '/mnt/lustre/jinliwei/annotation/bili_video_title_train',
    data_prefix = '',
    pipeline=[
        dict(type='OpenCVInit',
             io_backend='memcached',
             **mc_cfg),
        dict(type='SampleFrames', clip_len=1, frame_interval=1, num_clips=8),
        dict(type='OpenCVDecode'),
        dict(type='Resize', scale=(-1, 256), lazy=True),
        dict(
            type='MultiScaleCrop',
            input_size=224,
            scales=(1, 0.875, 0.75, 0.66),
            random_crop=False,
            max_wh_scale_gap=1,
            lazy=True),
        dict(type='Resize', scale=(224, 224), keep_ratio=False, lazy=True),
        dict(type='Flip', flip_ratio=0.5, lazy=True),
        dict(type='Fuse'),
        dict(type='Normalize', **img_norm_cfg),
        dict(type='FormatShape', input_format='NCHW'),
        dict(type='LoadTexts', sample_mode='number', sample_number=1),
        dict(type='TextTokenize', tokenizer_dir='/mnt/lustre/jinliwei/bert_model'),
        dict(type='Collect', keys=['imgs', 'texts_item'], meta_keys=[]),
        dict(type='ToTensor', keys=['imgs'])
    ]
)

mp4_text_dataset = build_dataset(cfg)


for i in range(len(mp4_text_dataset)):
    print(mp4_text_dataset[i])
