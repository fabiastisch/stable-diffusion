import argparse, os, sys, glob
import cv2
import torch
import numpy as np
from omegaconf import OmegaConf
from PIL import Image
from tqdm import tqdm, trange
from imwatermark import WatermarkEncoder
from itertools import islice
from einops import rearrange
from torchvision.utils import make_grid
import time
from pytorch_lightning import seed_everything
from torch import autocast
from contextlib import contextmanager, nullcontext

from ldm.util import instantiate_from_config
from ldm.models.diffusion.ddim import DDIMSampler
from ldm.models.diffusion.plms import PLMSSampler
from ldm.models.diffusion.dpm_solver import DPMSolverSampler

from diffusers.pipelines.stable_diffusion.safety_checker import StableDiffusionSafetyChecker
from transformers import AutoFeatureExtractor

# load safety model
safety_model_id = "CompVis/stable-diffusion-safety-checker"
safety_feature_extractor = AutoFeatureExtractor.from_pretrained(safety_model_id)
safety_checker = StableDiffusionSafetyChecker.from_pretrained(safety_model_id)


def chunk(it, size):
    it = iter(it)
    return iter(lambda: tuple(islice(it, size)), ())


def numpy_to_pil(images):
    """
    Convert a numpy image or a batch of images to a PIL image.
    """
    if images.ndim == 3:
        images = images[None, ...]
    images = (images * 255).round().astype("uint8")
    pil_images = [Image.fromarray(image) for image in images]

    return pil_images


def load_model_from_config(config, ckpt, verbose=False):
    print(f"Loading model from {ckpt}")
    pl_sd = torch.load(ckpt, map_location="cpu")
    if "global_step" in pl_sd:
        print(f"Global Step: {pl_sd['global_step']}")
    sd = pl_sd["state_dict"]
    model = instantiate_from_config(config.model)
    m, u = model.load_state_dict(sd, strict=False)
    if len(m) > 0 and verbose:
        print("missing keys:")
        print(m)
    if len(u) > 0 and verbose:
        print("unexpected keys:")
        print(u)

    model.cuda()
    model.eval()
    return model


def put_watermark(img, wm_encoder=None):
    if wm_encoder is not None:
        img = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
        img = wm_encoder.encode(img, 'dwtDct')
        img = Image.fromarray(img[:, :, ::-1])
    return img


def load_replacement(x):
    try:
        hwc = x.shape
        y = Image.open("assets/rick.jpeg").convert("RGB").resize((hwc[1], hwc[0]))
        y = (np.array(y) / 255.0).astype(x.dtype)
        assert y.shape == x.shape
        return y
    except Exception:
        return x


def check_safety(x_image):
    safety_checker_input = safety_feature_extractor(numpy_to_pil(x_image), return_tensors="pt")
    x_checked_image, has_nsfw_concept = safety_checker(images=x_image, clip_input=safety_checker_input.pixel_values)
    assert x_checked_image.shape[0] == len(has_nsfw_concept)
    for i in range(len(has_nsfw_concept)):
        if has_nsfw_concept[i]:
            x_checked_image[i] = load_replacement(x_checked_image[i])
    return x_checked_image, has_nsfw_concept


def main():
    # region Args
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--prompt",
        type=str,
        nargs="?",
        default="a painting of a virus monster playing guitar",
        help="the prompt to render"
    )
    parser.add_argument(
        "--outdir",
        type=str,
        nargs="?",
        help="dir to write results to",
        default="outputs/txt2img-samples"
    )
    parser.add_argument(
        "--skip_grid",
        action='store_true',
        help="do not save a grid, only individual samples. Helpful when evaluating lots of samples",
    )
    parser.add_argument(
        "--skip_save",
        action='store_true',
        help="do not save individual samples. For speed measurements.",
    )
    parser.add_argument(
        "--ddim_steps",
        type=int,
        default=50,
        help="number of ddim sampling steps",
    )
    parser.add_argument(
        "--plms",
        action='store_true',
        help="use plms sampling",
    )
    parser.add_argument(
        "--dpm_solver",
        action='store_true',
        help="use dpm_solver sampling",
    )
    parser.add_argument(
        "--laion400m",
        action='store_true',
        help="uses the LAION400M model",
    )
    parser.add_argument(
        "--fixed_code",
        action='store_true',
        help="if enabled, uses the same starting code across samples ",
    )
    parser.add_argument(
        "--ddim_eta",
        type=float,
        default=0.0,
        help="ddim eta (eta=0.0 corresponds to deterministic sampling",
    )
    parser.add_argument(
        "--n_iter",
        type=int,
        default=2,
        help="sample this often",
    )
    parser.add_argument(
        "--H",
        type=int,
        default=512,
        help="image height, in pixel space",
    )
    parser.add_argument(
        "--W",
        type=int,
        default=512,
        help="image width, in pixel space",
    )
    parser.add_argument(
        "--C",
        type=int,
        default=4,
        help="latent channels",
    )
    parser.add_argument(
        "--f",
        type=int,
        default=8,
        help="downsampling factor",
    )
    parser.add_argument(
        "--n_samples",
        type=int,
        default=3,
        help="how many samples to produce for each given prompt. A.k.a. batch size",
    )
    parser.add_argument(
        "--n_rows",
        type=int,
        default=0,
        help="rows in the grid (default: n_samples)",
    )
    parser.add_argument(
        "--scale",
        type=float,
        default=7.5,
        help="unconditional guidance scale: eps = eps(x, empty) + scale * (eps(x, cond) - eps(x, empty))",
    )
    parser.add_argument(
        "--from-file",
        type=str,
        help="if specified, load prompts from this file",
    )
    parser.add_argument(
        "--config",
        type=str,
        default="configs/stable-diffusion/v1-inference.yaml",
        help="path to config which constructs model",
    )
    parser.add_argument(
        "--ckpt",
        type=str,
        default="models/ldm/stable-diffusion-v1/model.ckpt",
        help="path to checkpoint of model",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="the seed (for reproducible sampling)",
    )
    parser.add_argument(
        "--precision",
        type=str,
        help="evaluate at this precision",
        choices=["full", "autocast"],
        default="autocast"
    )
    opt = parser.parse_args()
    # endregion Args

    # option laion400m - uses the LAION400M model
    if opt.laion400m:
        print("Falling back to LAION 400M model...")
        opt.config = "configs/latent-diffusion/txt2img-1p4B-eval.yaml"
        opt.ckpt = "models/ldm/text2img-large/model.ckpt"
        opt.outdir = "outputs/txt2img-samples-laion400m"

    seed_everything(opt.seed)

    config = OmegaConf.load(f"{opt.config}")
    # Model from config
    model = load_model_from_config(config, f"{opt.ckpt}")

    # Using Cuda (GPU) if available, else cpu
    device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
    model = model.to(device)

    # Define Sampler
    if opt.dpm_solver:
        sampler = DPMSolverSampler(model)
    elif opt.plms:
        sampler = PLMSSampler(model)
    else:
        sampler = DDIMSampler(model)

    os.makedirs(opt.outdir, exist_ok=True)
    outpath = opt.outdir

    print("Creating invisible watermark encoder (see https://github.com/ShieldMnt/invisible-watermark)...")
    wm = "StableDiffusionV1"
    wm_encoder = WatermarkEncoder()
    wm_encoder.set_watermark('bytes', wm.encode('utf-8'))

    batch_size = opt.n_samples
    # Number of Rows in the Grid
    n_rows = opt.n_rows if opt.n_rows > 0 else batch_size
    if not opt.from_file:
        prompt = opt.prompt
        assert prompt is not None
        data = [batch_size * [prompt]]

    else:
        print(f"reading prompts from {opt.from_file}")
        with open(opt.from_file, "r") as f:
            data = f.read().splitlines()
            data = list(chunk(data, batch_size))

    sample_path = os.path.join(outpath, "samples")
    os.makedirs(sample_path, exist_ok=True)
    process_path = os.path.join(outpath, "process")
    os.makedirs(process_path, exist_ok=True)
    process2_path = os.path.join(outpath, "process2")
    os.makedirs(process2_path, exist_ok=True)
    processList = []
    process2List = []

    base_count = len(os.listdir(sample_path))
    grid_count = len(os.listdir(outpath)) - 1

    start_code = None
    if opt.fixed_code:
        start_code = torch.randn([opt.n_samples, opt.C, opt.H // opt.f, opt.W // opt.f], device=device)

    precision_scope = autocast if opt.precision == "autocast" else nullcontext
    # Disabling gradient calculation
    with torch.no_grad():
        with precision_scope("cuda"):
            # Set Some weigts to ddpm model
            # (maybe Denoising Diffusion Probabilistic Model -https://arxiv.org/abs/2006.11239)
            with model.ema_scope():
                tic = time.time()
                all_samples = list()
                # for n_iter (sample this often) times TODO: was macht n_iter?
                for n in trange(opt.n_iter, desc="Sampling"):
                    # for every xyz in the prompt-data (data = [batch_size * [prompt]])
                    # tqdm is just a progessbar see- (https://github.com/tqdm/tqdm)
                    for prompts in tqdm(data, desc="data"):
                        uc = None
                        # "unconditional guidance scale", default: 7.5   - TODO: what is that?
                        if opt.scale != 1.0:
                            # TODO: model without prompts
                            # Latent Diffusion
                            uc = model.get_learned_conditioning(batch_size * [""])
                            # print(uc.shape) # torch.Size([3, 77, 768])

                        if isinstance(prompts, tuple):
                            prompts = list(prompts)

                        # TODO: model just with prompts
                        # Latent Diffusion
                        c = model.get_learned_conditioning(prompts)
                        print(c.shape)  # Torch.Size([3, 77, 768])

                        # C = latent channels
                        # H = image height, in pixel space
                        # W = image width, in pixel space
                        # f = downsampling factor
                        # so Channels, Height/factor, Width/factor
                        shape = [opt.C, opt.H // opt.f, opt.W // opt.f]

                        # Running sampling (default PLMS-Sampling)
                        # PLMS added here
                        # https://github.com/CompVis/latent-diffusion/pull/51
                        # TODO figure out what PLMS is
                        # ddim_eta == random noise added during sampling
                        # 1 = full; 0 = smooth changes (https://twitter.com/RiversHaveWings/status/1481389818315558912)

                        def save_step(prev, imgx0, i):
                            processList.append(prev)
                            process2List.append(imgx0)

                        samples_ddim, _ = sampler.sample(S=opt.ddim_steps,
                                                         conditioning=c,
                                                         batch_size=opt.n_samples,
                                                         shape=shape,
                                                         verbose=False,
                                                         unconditional_guidance_scale=opt.scale,
                                                         unconditional_conditioning=uc,
                                                         img_callback=save_step,
                                                         eta=opt.ddim_eta,
                                                         x_T=start_code)

                        def saveImages():
                            counter = 0
                            for imgstep in process2List:
                                # decode, clamp and permute sample?
                                samples = model.decode_first_stage(imgstep)
                                samples = torch.clamp((samples + 1.0) / 2.0, min=0.0, max=1.0)
                                samples = samples.cpu().permute(0, 2, 3, 1).numpy()
                                # checked_image, has_nsfw_concept = check_safety(samples)
                                checked_image_torch = torch.from_numpy(samples).permute(0, 3, 1, 2)
                                for i, sample in enumerate(checked_image_torch):
                                    sample = 255. * rearrange(sample.cpu().numpy(), 'c h w -> h w c')
                                    img = Image.fromarray(sample.astype(np.uint8))
                                    # img = put_watermark(img, wm_encoder)
                                    img.save(os.path.join(process2_path, f"{counter:05}.png"))
                                    counter += 1
                            counter = 0
                            for imgstep in processList:
                                # decode, clamp and permute sample?
                                samples = model.decode_first_stage(imgstep)
                                samples = torch.clamp((samples + 1.0) / 2.0, min=0.0, max=1.0)
                                samples = samples.cpu().permute(0, 2, 3, 1).numpy()
                                # checked_image, has_nsfw_concept = check_safety(samples)
                                checked_image_torch = torch.from_numpy(samples).permute(0, 3, 1, 2)
                                for i, sample in enumerate(checked_image_torch):
                                    sample = 255. * rearrange(sample.cpu().numpy(), 'c h w -> h w c')
                                    img = Image.fromarray(sample.astype(np.uint8))
                                    # img = put_watermark(img, wm_encoder)
                                    img.save(os.path.join(process_path, f"{counter:05}.png"))
                                    counter += 1

                            processList.clear()
                            process2List.clear()
                        saveImages()

                        # decode, clamp and permute sample?
                        # TODO: what is this doing
                        # print(samples_ddim.shape) # torch.Size([3, 4, 32, 32])
                        x_samples_ddim = model.decode_first_stage(samples_ddim)
                        x_samples_ddim = torch.clamp((x_samples_ddim + 1.0) / 2.0, min=0.0, max=1.0)
                        # print(x_samples_ddim.shape)# torch.Size([3, 3, 256, 256]) // H 256 | W 256 as parm
                        x_samples_ddim = x_samples_ddim.cpu().permute(0, 2, 3, 1).numpy()

                        # Check sample for nsfw content
                        x_checked_image, has_nsfw_concept = check_safety(x_samples_ddim)

                        # permute again
                        x_checked_image_torch = torch.from_numpy(x_checked_image).permute(0, 3, 1, 2)
                        # print(x_checked_image_torch.shape) # torch.Size([3, 3, 256, 256])

                        if not opt.skip_save:
                            # Save the individual samples
                            for x_sample in x_checked_image_torch:
                                x_sample = 255. * rearrange(x_sample.cpu().numpy(), 'c h w -> h w c')
                                img = Image.fromarray(x_sample.astype(np.uint8))
                                img = put_watermark(img, wm_encoder)
                                img.save(os.path.join(sample_path, f"{base_count:05}.png"))
                                base_count += 1

                        if not opt.skip_grid:
                            all_samples.append(x_checked_image_torch)

                if not opt.skip_grid:
                    # Save the Grid image

                    # additionally, save as grid
                    grid = torch.stack(all_samples, 0)
                    grid = rearrange(grid, 'n b c h w -> (n b) c h w')
                    grid = make_grid(grid, nrow=n_rows)

                    # to image
                    grid = 255. * rearrange(grid, 'c h w -> h w c').cpu().numpy()
                    img = Image.fromarray(grid.astype(np.uint8))
                    img = put_watermark(img, wm_encoder)
                    img.save(os.path.join(outpath, f'grid-{grid_count:04}.png'))
                    grid_count += 1

                toc = time.time()

    print(f"Your samples are ready and waiting for you here: \n{outpath} \n"
          f" \nEnjoy.")


if __name__ == "__main__":
    main()
