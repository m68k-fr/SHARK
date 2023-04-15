import torch
import time
from PIL import Image
import transformers
from apps.stable_diffusion.src import (
    args,
    UpscalerPipeline,
    get_schedulers,
    set_init_device_flags,
    utils,
    clear_all,
    save_output_img,
)


# set initial values of iree_vulkan_target_triple, use_tuned and import_mlir.
init_iree_vulkan_target_triple = args.iree_vulkan_target_triple
init_use_tuned = args.use_tuned
init_import_mlir = args.import_mlir


# Exposed to UI.
def upscaler_inf(
    prompt: str,
    negative_prompt: str,
    init_image,
    height: int,
    width: int,
    steps: int,
    noise_level: int,
    guidance_scale: float,
    seed: int,
    batch_count: int,
    batch_size: int,
    scheduler: str,
    custom_model: str,
    hf_model_id: str,
    precision: str,
    device: str,
    max_length: int,
    save_metadata_to_json: bool,
    save_metadata_to_png: bool,
    lora_weights: str,
    lora_hf_id: str,
    ondemand: bool,
):
    from apps.stable_diffusion.web.ui.utils import (
        get_custom_model_pathfile,
        get_custom_vae_or_lora_weights,
        Config,
    )
    import apps.stable_diffusion.web.utils.global_obj as global_obj
    import apps.stable_diffusion.src.utils.state_manager as state_manager

    if not state_manager.app.is_ready():
        return

    args.prompts = [prompt]
    args.negative_prompts = [negative_prompt]
    args.guidance_scale = guidance_scale
    args.seed = seed
    args.steps = steps
    args.scheduler = scheduler
    args.ondemand = ondemand
    if ondemand and batch_count > 1:
        print("Low VRAM mode currently only supports 1 batch count.")
        batch_count = 1

    if init_image is None:
        return None, "An Initial Image is required"
    image = init_image.convert("RGB").resize((height, width))

    # set ckpt_loc and hf_model_id.
    args.ckpt_loc = ""
    args.hf_model_id = ""
    if custom_model == "None":
        if not hf_model_id:
            return (
                None,
                "Please provide either custom model or huggingface model ID, both must not be empty",
            )
        args.hf_model_id = hf_model_id
    elif ".ckpt" in custom_model or ".safetensors" in custom_model:
        args.ckpt_loc = get_custom_model_pathfile(custom_model)
    else:
        args.hf_model_id = custom_model

    # TODO: StateManager Try starts here
    # try:
    state_manager.app.set_job(f"Initializing model {custom_model}", False)
    args.save_metadata_to_json = save_metadata_to_json
    args.write_metadata_to_png = save_metadata_to_png

    args.use_lora = get_custom_vae_or_lora_weights(
        lora_weights, lora_hf_id, "lora"
    )

    dtype = torch.float32 if precision == "fp32" else torch.half
    cpu_scheduling = not scheduler.startswith("Shark")
    args.height = 128
    args.width = 128
    new_config_obj = Config(
        "upscaler",
        args.hf_model_id,
        args.ckpt_loc,
        precision,
        batch_size,
        max_length,
        args.height,
        args.width,
        device,
        use_lora=args.use_lora,
        use_stencil=None,
        ondemand=ondemand,
    )
    if (
        args.ondemand
        or not global_obj.get_sd_obj()
        or global_obj.get_cfg_obj() != new_config_obj
    ):
        global_obj.clear_cache()
        global_obj.set_cfg_obj(new_config_obj)
        args.batch_size = batch_size
        args.max_length = max_length
        args.device = device.split("=>", 1)[1].strip()
        args.iree_vulkan_target_triple = init_iree_vulkan_target_triple
        args.use_tuned = init_use_tuned
        args.import_mlir = init_import_mlir
        set_init_device_flags()
        model_id = (
            args.hf_model_id
            if args.hf_model_id
            else "stabilityai/stable-diffusion-2-1-base"
        )
        global_obj.set_schedulers(get_schedulers(model_id))
        scheduler_obj = global_obj.get_scheduler(scheduler)
        global_obj.set_sd_obj(
            UpscalerPipeline.from_pretrained(
                scheduler_obj,
                args.import_mlir,
                args.hf_model_id,
                args.ckpt_loc,
                args.custom_vae,
                args.precision,
                args.max_length,
                args.batch_size,
                args.height,
                args.width,
                args.use_base_vae,
                args.use_tuned,
                low_cpu_mem_usage=args.low_cpu_mem_usage,
                use_lora=args.use_lora,
                ondemand=args.ondemand,
            )
        )

    global_obj.set_sd_scheduler(scheduler)
    global_obj.get_sd_obj().low_res_scheduler = global_obj.get_scheduler(
        "DDPM"
    )

    start_time = time.time()
    global_obj.get_sd_obj().log = ""
    generated_imgs = []
    seeds = []
    img_seed = utils.sanitize_seed(seed)
    extra_info = {"NOISE LEVEL": noise_level}
    for current_batch in range(batch_count):
        state_manager.app.set_job(
            "Running upscaler job",
            False,
            current_batch,
            batch_count,
            steps,
        )
        if current_batch > 0:
            img_seed = utils.sanitize_seed(-1)
        low_res_img = image
        high_res_img = Image.new("RGB", (height * 4, width * 4))

        for i in range(0, width, 128):
            for j in range(0, height, 128):
                box = (j, i, j + 128, i + 128)
                upscaled_image = global_obj.get_sd_obj().generate_images(
                    prompt,
                    negative_prompt,
                    low_res_img.crop(box),
                    batch_size,
                    args.height,
                    args.width,
                    steps,
                    noise_level,
                    guidance_scale,
                    img_seed,
                    args.max_length,
                    dtype,
                    args.use_base_vae,
                    cpu_scheduling,
                )
                high_res_img.paste(upscaled_image[0], (j * 4, i * 4))

        if state_manager.app.is_canceling():
            break
        save_output_img(high_res_img, img_seed, extra_info)
        generated_imgs.append(high_res_img)
        seeds.append(img_seed)
        global_obj.get_sd_obj().log += "\n"
        yield generated_imgs, global_obj.get_sd_obj().log

    total_time = time.time() - start_time
    text_output = f"prompt={args.prompts}"
    text_output += f"\nnegative prompt={args.negative_prompts}"
    text_output += f"\nmodel_id={args.hf_model_id}, ckpt_loc={args.ckpt_loc}"
    text_output += f"\nscheduler={args.scheduler}, device={device}"
    text_output += f"\nsteps={steps}, noise_level={noise_level}, guidance_scale={guidance_scale}, seed={seeds}"
    text_output += f"\nsize={height}x{width}, batch_count={batch_count}, batch_size={batch_size}, max_length={args.max_length}"
    text_output += global_obj.get_sd_obj().log
    text_output += f"\nTotal image generation time: {total_time:.4f}sec"

    # TODO: StateManager Try ends here
    # except Exception:
    #     state_manager.app.set_ready()
    #     raise

    state_manager.app.set_ready()
    return generated_imgs, text_output


if __name__ == "__main__":
    if args.clear_all:
        clear_all()

    if args.img_path is None:
        print("Flag --img_path is required.")
        exit()

    # When the models get uploaded, it should be default to False.
    args.import_mlir = True

    cpu_scheduling = not args.scheduler.startswith("Shark")
    dtype = torch.float32 if args.precision == "fp32" else torch.half
    set_init_device_flags()
    schedulers = get_schedulers(args.hf_model_id)

    scheduler_obj = schedulers[args.scheduler]
    image = (
        Image.open(args.img_path)
        .convert("RGB")
        .resize((args.height, args.width))
    )
    seed = utils.sanitize_seed(args.seed)
    # Adjust for height and width based on model

    upscaler_obj = UpscalerPipeline.from_pretrained(
        scheduler_obj,
        args.import_mlir,
        args.hf_model_id,
        args.ckpt_loc,
        args.custom_vae,
        args.precision,
        args.max_length,
        args.batch_size,
        args.height,
        args.width,
        args.use_base_vae,
        args.use_tuned,
        low_cpu_mem_usage=args.low_cpu_mem_usage,
        use_lora=args.use_lora,
        ddpm_scheduler=schedulers["DDPM"],
        ondemand=args.ondemand,
    )

    start_time = time.time()
    generated_imgs = upscaler_obj.generate_images(
        args.prompts,
        args.negative_prompts,
        image,
        args.batch_size,
        args.height,
        args.width,
        args.steps,
        args.noise_level,
        args.guidance_scale,
        seed,
        args.max_length,
        dtype,
        args.use_base_vae,
        cpu_scheduling,
    )
    total_time = time.time() - start_time
    text_output = f"prompt={args.prompts}"
    text_output += f"\nnegative prompt={args.negative_prompts}"
    text_output += f"\nmodel_id={args.hf_model_id}, ckpt_loc={args.ckpt_loc}"
    text_output += f"\nscheduler={args.scheduler}, device={args.device}"
    text_output += f"\nsteps={args.steps}, noise_level={args.noise_level}, guidance_scale={args.guidance_scale}, seed={seed}, size={args.height}x{args.width}"
    text_output += (
        f", batch size={args.batch_size}, max_length={args.max_length}"
    )
    text_output += upscaler_obj.log
    text_output += f"\nTotal image generation time: {total_time:.4f}sec"

    extra_info = {"NOISE LEVEL": args.noise_level}
    save_output_img(generated_imgs[0], seed, extra_info)
    print(text_output)
