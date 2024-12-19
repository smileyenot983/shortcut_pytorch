import math
import torch
import os
import pytorch_lightning as pl
from pytorch_lightning import Callback
from pytorch_lightning.callbacks import ModelCheckpoint
from pytorch_lightning.utilities import rank_zero_warn, rank_zero_info
from pytorch_lightning.utilities.exceptions import MisconfigurationException
from pytorch_lightning.utilities.types import STEP_OUTPUT

from typing import Any, Dict, List, Optional


DEVICE = 'cuda' if torch.cuda.is_available() else 'cpu'
BOOTSTRAP_EVERY = 8
DENOISE_TIMESTEPS = 128
CLASS_DROPOUT_PROB = 1.0
NUM_CLASSES = 1
BATCH_SIZE = 64

# create batch, consisting of different timesteps and different dts(depending on total step sizes)
def create_targets(images, labels, model):

    model.eval()

    current_batch_size = images.shape[0]

    FORCE_T = -1
    FORCE_DT = -1

    # 1. create step sizes dt
    bootstrap_batch_size = current_batch_size // BOOTSTRAP_EVERY #=8
    log2_sections = int(math.log2(DENOISE_TIMESTEPS))
    # print(f"log2_sections: {log2_sections}")
    # print(f"bootstrap_batch_size: {bootstrap_batch_size}")

    dt_base = torch.repeat_interleave(log2_sections - 1 - torch.arange(log2_sections), bootstrap_batch_size // log2_sections)
    # print(f"dt_base: {dt_base}")

    dt_base = torch.cat([dt_base, torch.zeros(bootstrap_batch_size-dt_base.shape[0],)])
    # print(f"dt_base: {dt_base}")


    
    force_dt_vec = torch.ones(bootstrap_batch_size) * FORCE_DT
    dt_base = torch.where(force_dt_vec != -1, force_dt_vec, dt_base).to(model.device)
    dt = 1 / (2 ** (dt_base)) # [1, 1/2, 1/8, 1/16, 1/32]
    # print(f"dt: {dt}")

    dt_base_bootstrap = dt_base + 1
    dt_bootstrap = dt / 2 # [0.0078125 0.015625 0.03125 0.0625 0.125 0.25 0.5 0.5]
    # print(f"dt_bootstrap: {dt_bootstrap}")

    # 2. sample timesteps t
    dt_sections = 2**dt_base

    # print(f"dt_sections: {dt_sections}")

    t = torch.cat([
        torch.randint(low=0, high=int(val.item()), size=(1,)).float()
        for val in dt_sections
        ]).to(model.device)
    
    # print(f"t[randint]: {t}")
    t = t / dt_sections
    # print(f"t[normalized]: {t}")
    
    force_t_vec = torch.ones(bootstrap_batch_size, dtype=torch.float32).to(model.device) * FORCE_T
    t = torch.where(force_t_vec != -1, force_t_vec, t).to(model.device)
    t_full = t[:, None, None, None]

    # print(f"t_full: {t_full}")

    # 3. generate bootstrap targets:
    x_1 = images[:bootstrap_batch_size]
    x_0 = torch.randn_like(x_1)

    # get dx at timestep t
    x_t = (1 - (1-1e-5) * t_full)*x_0 + t_full*x_1

    bst_labels = labels[:bootstrap_batch_size]


    with torch.no_grad():
        v_b1 = model(x_t, t, dt_base_bootstrap, bst_labels)

    t2 = t + dt_bootstrap
    x_t2 = x_t + dt_bootstrap[:, None, None, None] * v_b1
    x_t2 = torch.clip(x_t2, -4, 4)
    
    with torch.no_grad():
        v_b2 = model(x_t2, t2, dt_base_bootstrap, bst_labels)

    v_target = (v_b1 + v_b2) / 2

    v_target = torch.clip(v_target, -4, 4)
    
    bst_v = v_target
    bst_dt = dt_base
    bst_t = t
    bst_xt = x_t
    bst_l = bst_labels

    # 4. generate flow-matching targets

    labels_dropout = torch.bernoulli(torch.full(labels.shape, CLASS_DROPOUT_PROB)).to(model.device)
    labels_dropped = torch.where(labels_dropout.bool(), NUM_CLASSES, labels)

    # sample t(normalized)
    t = torch.randint(low=0, high=DENOISE_TIMESTEPS, size=(images.shape[0],), dtype=torch.float32)
    # print(f"t: {t}")
    t /= DENOISE_TIMESTEPS
    # print(f"t: {t}")
    force_t_vec = torch.ones(images.shape[0]) * FORCE_T
    # force_t_vec = torch.full((images.shape[0],), FORCE_T, dtype=torch.float32)
    t = torch.where(force_t_vec != -1, force_t_vec, t).to(model.device)
    # t_full = t.view(-1, 1, 1, 1)
    t_full = t[:, None, None, None]

    # print(f"t_full: {t_full}")

    # sample flow pairs x_t, v_t
    x_0 = torch.randn_like(images).to(model.device)
    x_1 = images
    x_t = (1 - (1 - 1e-5) * t_full) * x_0 + t_full * x_1
    v_t = x_1 - (1 - 1e-5) * x_0

    dt_flow = int(math.log2(DENOISE_TIMESTEPS))
    dt_base = (torch.ones(images.shape[0], dtype=torch.int32) * dt_flow).to(model.device)

    # 5. merge flow and bootstrap
    bst_size = current_batch_size // BOOTSTRAP_EVERY
    bst_size_data = current_batch_size - bst_size

    # print(f"bst_size: {bst_size}")
    # print(f"bst_size_data: {bst_size_data}")

    x_t = torch.cat([bst_xt, x_t[:bst_size_data]], dim=0)
    t = torch.cat([bst_t, t[:bst_size_data]], dim=0)

    dt_base = torch.cat([bst_dt, dt_base[:bst_size_data]], dim=0)
    v_t = torch.cat([bst_v, v_t[:bst_size_data]], dim=0)
    labels_dropped = torch.cat([bst_l, labels_dropped[:bst_size_data]], dim=0)

    return x_t, v_t, t, dt_base, labels_dropped

def create_targets_naive(images, labels, model):

    model.eval()

    current_batch_size = images.shape[0]

    FORCE_T = -1
    FORCE_DT = -1

    labels_dropout = torch.bernoulli(torch.full(labels.shape, CLASS_DROPOUT_PROB)).to(model.device)
    labels_dropped = torch.where(labels_dropout.bool(), NUM_CLASSES, labels)

    # sample t(normalized)
    t = torch.randint(low=0, high=DENOISE_TIMESTEPS, size=(images.shape[0],), dtype=torch.float32)
    # print(f"t: {t}")
    t /= DENOISE_TIMESTEPS
    # print(f"t: {t}")
    force_t_vec = torch.ones(images.shape[0]) * FORCE_T
    # force_t_vec = torch.full((images.shape[0],), FORCE_T, dtype=torch.float32)
    t = torch.where(force_t_vec != -1, force_t_vec, t).to(model.device)
    # t_full = t.view(-1, 1, 1, 1)
    t_full = t[:, None, None, None]


    x_0 = torch.randn_like(images).to(model.device)
    x_1 = images
    x_t = (1 - (1 - 1e-5) * t_full) * x_0 + t_full * x_1
    v_t = x_1 - (1 - 1e-5) * x_0

    dt_flow = int(math.log2(DENOISE_TIMESTEPS))
    dt_base = (torch.ones(images.shape[0], dtype=torch.int32) * dt_flow).to(model.device)

    return x_t, v_t, t, dt_base, labels_dropped



    

